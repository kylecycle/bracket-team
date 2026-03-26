"""Matchup routes: per-matchup detail including reports and discussion."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from bracket_team.api.app import _require_api_key
from bracket_team.config import get_config
from bracket_team.db.connection import get_connection
from bracket_team.db.models import AnalystReport, DiscussionMessage, Matchup, Prediction
from bracket_team.db.repositories.discussion_repo import DiscussionRepository
from bracket_team.db.repositories.matchup_repo import MatchupRepository
from bracket_team.db.repositories.prediction_repo import PredictionRepository
from bracket_team.db.repositories.report_repo import ReportRepository
from bracket_team.db.repositories.run_repo import RunRepository

router = APIRouter(prefix="/api/matchups", dependencies=[Depends(_require_api_key)])


class MatchupDetail(BaseModel):
    matchup: Matchup
    prediction: Prediction | None
    reports: list[AnalystReport]
    discussion: list[DiscussionMessage]


@router.get("/{matchup_id}", response_model=MatchupDetail)
async def api_get_matchup(matchup_id: int, run_id: int) -> MatchupDetail:
    async with get_connection() as conn:
        matchup = await MatchupRepository(conn).get(matchup_id)
        if matchup is None:
            raise HTTPException(status_code=404, detail=f"Matchup {matchup_id} not found")
        prediction = await PredictionRepository(conn).get_by_matchup(run_id, matchup_id)
        reports = await ReportRepository(conn).list_by_matchup(run_id, matchup_id)
        discussion = await DiscussionRepository(conn).list_by_matchup(run_id, matchup_id)

    return MatchupDetail(
        matchup=matchup,
        prediction=prediction,
        reports=reports,
        discussion=discussion,
    )


class RecalculateResponse(BaseModel):
    recalculated: list[MatchupDetail]


@router.post("/{matchup_id}/recalculate", response_model=RecalculateResponse)
async def api_recalculate_matchup(matchup_id: int, run_id: int) -> RecalculateResponse:
    """Recalculate one matchup and cascade to downstream rounds if the winner changes."""
    from bracket_team.agents.llm import create_backend
    from bracket_team.service.pipeline import MatchupPipeline

    cfg = get_config()

    async with get_connection() as conn:
        matchup = await MatchupRepository(conn).get(matchup_id)
        if matchup is None:
            raise HTTPException(status_code=404, detail=f"Matchup {matchup_id} not found")
        run = await RunRepository(conn).get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
        if run.status == "running":
            raise HTTPException(status_code=409, detail="Cannot recalculate while run is in progress")

    llm = create_backend(cfg)
    pipeline = MatchupPipeline(llm, cfg, user_preferences=run.user_preferences)

    results: list[MatchupDetail] = []
    await _recalculate_and_cascade(run_id, run, matchup, pipeline, results)
    return RecalculateResponse(recalculated=results)


async def _recalculate_and_cascade(
    run_id: int,
    run,
    matchup: Matchup,
    pipeline,
    results: list[MatchupDetail],
) -> None:
    """Clear, re-run one matchup, then cascade to the next round if the winner changed."""
    from bracket_team.db.repositories.matchup_repo import MatchupRepository

    # Capture old winner before deleting
    async with get_connection() as conn:
        old_pred = await PredictionRepository(conn).get_by_matchup(run_id, matchup.id)
    old_winner = old_pred.predicted_winner if old_pred else None

    # Clear existing data
    async with get_connection() as conn:
        await conn.execute(
            "DELETE FROM predictions WHERE run_id = ? AND matchup_id = ?", (run_id, matchup.id)
        )
        await conn.execute(
            "DELETE FROM analyst_reports WHERE run_id = ? AND matchup_id = ?", (run_id, matchup.id)
        )
        await conn.execute(
            "DELETE FROM discussion_messages WHERE run_id = ? AND matchup_id = ?", (run_id, matchup.id)
        )
        await conn.execute(
            "DELETE FROM llm_costs WHERE run_id = ? AND matchup_id = ?", (run_id, matchup.id)
        )
        await conn.commit()

    await pipeline.run(run_id, matchup)

    async with get_connection() as conn:
        new_pred = await PredictionRepository(conn).get_by_matchup(run_id, matchup.id)
        reports = await ReportRepository(conn).list_by_matchup(run_id, matchup.id)
        discussion = await DiscussionRepository(conn).list_by_matchup(run_id, matchup.id)

    results.append(MatchupDetail(
        matchup=matchup,
        prediction=new_pred,
        reports=reports,
        discussion=discussion,
    ))

    new_winner = new_pred.predicted_winner if new_pred else None
    if not new_winner or new_winner == old_winner or matchup.round_num >= 6:
        return

    # Winner changed — update downstream matchup and cascade
    new_seed = (
        matchup.favorite_seed if new_winner == matchup.favorite_name else matchup.underdog_seed
    )
    async with get_connection() as conn:
        repo = MatchupRepository(conn)
        next_matchup = await repo.find_by_team_in_round(
            matchup.bracket_id, matchup.round_num + 1, old_winner, run_id=run_id
        )
        if next_matchup is None:
            return
        next_matchup = await repo.replace_team(next_matchup.id, old_winner, new_winner, new_seed)

    await _recalculate_and_cascade(run_id, run, next_matchup, pipeline, results)
