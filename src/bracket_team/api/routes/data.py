"""Data routes: view and manage scraped cache tables."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from bracket_team.api.app import _require_api_key
from bracket_team.config import get_config
from bracket_team.db.connection import get_connection
from bracket_team.db.repositories.bracket_repo import BracketRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/data", dependencies=[Depends(_require_api_key)])

# In-memory gather status per bracket_id
_gather_status: dict[int, dict[str, Any]] = {}

# Last known Odds API remaining quota (updated after every odds gather)
_odds_api_remaining: int | None = None


class GatherStatus(BaseModel):
    bracket_id: int
    status: str  # idle | running | completed | error
    summary: dict[str, Any] | None = None
    errors: list[str] = []


class TableSummary(BaseModel):
    team_stats: int
    team_player_stats: int
    team_odds: int
    seed_matchup_history: int
    bart_stats: int  # teams with BartTorvik data (barthag IS NOT NULL)


@router.get("/summary", response_model=TableSummary)
async def api_data_summary(bracket_id: int) -> TableSummary:
    async with get_connection() as conn:
        async with conn.execute(
            "SELECT COUNT(*) FROM team_stats WHERE bracket_id = ?", (bracket_id,)
        ) as c:
            ts = (await c.fetchone())[0]
        async with conn.execute(
            "SELECT COUNT(*) FROM team_player_stats WHERE bracket_id = ?", (bracket_id,)
        ) as c:
            tps = (await c.fetchone())[0]
        async with conn.execute(
            "SELECT COUNT(*) FROM team_odds WHERE bracket_id = ?", (bracket_id,)
        ) as c:
            to_ = (await c.fetchone())[0]
        async with conn.execute("SELECT COUNT(*) FROM seed_matchup_history") as c:
            smh = (await c.fetchone())[0]
        async with conn.execute(
            "SELECT COUNT(*) FROM team_stats WHERE bracket_id = ? AND barthag IS NOT NULL",
            (bracket_id,),
        ) as c:
            bt = (await c.fetchone())[0]
    return TableSummary(
        team_stats=ts,
        team_player_stats=tps,
        team_odds=to_,
        seed_matchup_history=smh,
        bart_stats=bt,
    )


@router.get("/team_stats")
async def api_team_stats(bracket_id: int) -> list[dict]:
    async with get_connection() as conn:
        async with conn.execute(
            "SELECT * FROM team_stats WHERE bracket_id = ? ORDER BY team_name", (bracket_id,)
        ) as c:
            rows = await c.fetchall()
            return [dict(r) for r in rows]


@router.get("/team_player_stats")
async def api_team_player_stats(bracket_id: int) -> list[dict]:
    async with get_connection() as conn:
        async with conn.execute(
            "SELECT * FROM team_player_stats WHERE bracket_id = ? ORDER BY team_name, mpg DESC",
            (bracket_id,),
        ) as c:
            rows = await c.fetchall()
            return [dict(r) for r in rows]


@router.get("/team_odds")
async def api_team_odds(bracket_id: int) -> list[dict]:
    async with get_connection() as conn:
        async with conn.execute(
            "SELECT * FROM team_odds WHERE bracket_id = ? ORDER BY favorite_name", (bracket_id,)
        ) as c:
            rows = await c.fetchall()
            return [dict(r) for r in rows]


@router.get("/seed_history")
async def api_seed_history() -> list[dict]:
    async with get_connection() as conn:
        async with conn.execute(
            "SELECT * FROM seed_matchup_history ORDER BY favorite_seed, underdog_seed"
        ) as c:
            rows = await c.fetchall()
            return [dict(r) for r in rows]


@router.get("/gather-status", response_model=GatherStatus)
async def api_gather_status(bracket_id: int) -> GatherStatus:
    info = _gather_status.get(bracket_id, {"status": "idle", "summary": None, "errors": []})
    return GatherStatus(bracket_id=bracket_id, **info)


@router.get("/odds-quota")
async def api_odds_quota() -> dict:
    """Return the last known Odds API remaining request count for this month."""
    return {"remaining": _odds_api_remaining}


@router.post("/gather", response_model=GatherStatus, status_code=202)
async def api_gather(
    bracket_id: int,
    background_tasks: BackgroundTasks,
    force: bool = False,
    source: str | None = None,
) -> GatherStatus:
    """Trigger a background gather.
    Pass force=true to re-scrape already-cached data.
    Pass source=sports|barttorvik|odds|seed_history to run one scraper only.
    """
    current = _gather_status.get(bracket_id, {})
    if current.get("status") == "running":
        raise HTTPException(status_code=409, detail="Gather already in progress for this bracket")
    async with get_connection() as conn:
        bracket = await BracketRepository(conn).get(bracket_id)
    if bracket is None:
        raise HTTPException(status_code=404, detail=f"Bracket {bracket_id} not found")
    _gather_status[bracket_id] = {"status": "running", "summary": None, "errors": [], "force": force}
    background_tasks.add_task(_run_gather, bracket_id, bracket.year, force, source)
    return GatherStatus(bracket_id=bracket_id, status="running")


class PlayerInjuryUpdate(BaseModel):
    player_id: int
    injured: bool
    injury_note: str | None = None


@router.patch("/player_injury")
async def api_update_player_injury(body: PlayerInjuryUpdate) -> dict:
    """Manually flag a player as injured/available."""
    async with get_connection() as conn:
        async with conn.execute(
            "SELECT id FROM team_player_stats WHERE id = ?", (body.player_id,)
        ) as c:
            if not await c.fetchone():
                raise HTTPException(status_code=404, detail=f"Player {body.player_id} not found")
        await conn.execute(
            "UPDATE team_player_stats SET injured = ?, injury_note = ? WHERE id = ?",
            (body.injured, body.injury_note if body.injured else None, body.player_id),
        )
        await conn.commit()
    return {"ok": True}


@router.delete("/clear", status_code=204)
async def api_clear(bracket_id: int) -> Response:
    async with get_connection() as conn:
        await conn.execute("DELETE FROM team_stats WHERE bracket_id = ?", (bracket_id,))
        await conn.execute("DELETE FROM team_player_stats WHERE bracket_id = ?", (bracket_id,))
        await conn.execute("DELETE FROM team_odds WHERE bracket_id = ?", (bracket_id,))
        await conn.commit()
    _gather_status.pop(bracket_id, None)
    return Response(status_code=204)


async def _run_gather(bracket_id: int, year: int, force: bool = False, source: str | None = None) -> None:
    """Background task: run GatherCoordinator for a bracket."""
    global _odds_api_remaining

    from bracket_team.scraper.coordinator import VALID_SOURCES, GatherCoordinator

    cfg = get_config()
    odds_key = cfg.odds_api_key.get_secret_value() if cfg.odds_api_key else None
    sources = frozenset([source]) if source and source in VALID_SOURCES else None
    try:
        coord = GatherCoordinator(
            bracket_id=bracket_id, year=year, odds_api_key=odds_key, force=force, sources=sources
        )
        summary = await coord.run()
        if summary.get("odds_api_remaining") is not None:
            _odds_api_remaining = summary["odds_api_remaining"]
        _gather_status[bracket_id] = {
            "status": "completed",
            "summary": summary,
            "errors": summary.get("errors", []),
        }
    except Exception as exc:
        logger.exception("Gather failed for bracket_id=%d", bracket_id)
        _gather_status[bracket_id] = {
            "status": "error",
            "summary": None,
            "errors": [str(exc)],
        }
