"""
Integration tests: full pipeline end-to-end against in-memory SQLite.
Uses StubBackend — no API calls, no cost.
"""

from __future__ import annotations

import pytest

from bracket_team.agents.llm import StubBackend
from bracket_team.config import AppConfig
from bracket_team.db.connection import get_connection
from bracket_team.db.repositories.cost_repo import CostRepository
from bracket_team.db.repositories.discussion_repo import DiscussionRepository
from bracket_team.db.repositories.matchup_repo import MatchupRepository
from bracket_team.db.repositories.prediction_repo import PredictionRepository
from bracket_team.db.repositories.report_repo import ReportRepository
from bracket_team.service.bracket_service import MatchupInput, import_bracket
from bracket_team.service.pipeline import MatchupPipeline
from bracket_team.service.run_service import create_run, get_run_predictions, get_total_cost


@pytest.fixture
def config() -> AppConfig:
    return AppConfig(anthropic_api_key="stub")


@pytest.fixture
def stub_llm() -> StubBackend:
    return StubBackend()


@pytest.fixture
async def seeded_run(config):
    """Create a bracket + run + matchup in DB, return (run, matchup)."""
    bracket, matchups = await import_bracket(
        year=2025,
        tournament_name="Integration Test Bracket",
        matchups=[
            MatchupInput(
                round_num=1, region="East",
                favorite_name="Duke", favorite_seed=2,
                underdog_name="UNC", underdog_seed=7,
            )
        ],
    )
    run = await create_run(bracket_id=bracket.id, name="Integration Test Run")
    return run, matchups[0]


# ---------------------------------------------------------------------------
# Row counts after a full matchup pipeline run
# ---------------------------------------------------------------------------

async def test_analyst_reports_written(stub_llm, config, seeded_run, in_memory_db):
    run, matchup = seeded_run
    pipeline = MatchupPipeline(stub_llm, config)
    await pipeline.run(run.id, matchup)

    async with get_connection() as conn:
        reports = await ReportRepository(conn).list_by_matchup(run.id, matchup.id)

    assert len(reports) == 4, f"Expected 4 analyst reports, got {len(reports)}"


async def test_report_fields_valid(stub_llm, config, seeded_run, in_memory_db):
    run, matchup = seeded_run
    pipeline = MatchupPipeline(stub_llm, config)
    await pipeline.run(run.id, matchup)

    async with get_connection() as conn:
        reports = await ReportRepository(conn).list_by_matchup(run.id, matchup.id)

    roles = {r.analyst_role for r in reports}
    assert roles == {"sports_analyst", "odds_analyst", "historical_analyst", "injury_analyst"}

    for r in reports:
        assert r.pick in ("favorite", "underdog")
        assert -5 <= r.score <= 5
        assert r.relevance in ("low", "medium", "high")
        assert len(r.thesis) > 0


async def test_discussion_messages_written(stub_llm, config, seeded_run, in_memory_db):
    run, matchup = seeded_run
    pipeline = MatchupPipeline(stub_llm, config)
    await pipeline.run(run.id, matchup)

    async with get_connection() as conn:
        messages = await DiscussionRepository(conn).list_by_matchup(run.id, matchup.id)

    phases = [m.phase for m in messages]
    assert "challenge" in phases
    assert "rebuttal" in phases


async def test_exactly_one_prediction_written(stub_llm, config, seeded_run, in_memory_db):
    run, matchup = seeded_run
    pipeline = MatchupPipeline(stub_llm, config)
    await pipeline.run(run.id, matchup)

    async with get_connection() as conn:
        predictions = await PredictionRepository(conn).list_by_run(run.id)

    assert len(predictions) == 1
    p = predictions[0]
    assert p.outcome_type in ("expected", "upset")
    assert p.confidence in ("high", "medium", "low")
    assert p.weighted_score != 0.0
    assert len(p.synthesis) > 0


async def test_cost_rows_written(stub_llm, config, seeded_run, in_memory_db):
    run, matchup = seeded_run
    pipeline = MatchupPipeline(stub_llm, config)
    await pipeline.run(run.id, matchup)

    async with get_connection() as conn:
        costs = await CostRepository(conn).list_by_run(run.id)

    # 4 research + 4 challenges + 4 rebuttals + 1 decision = 13 max
    # (some may be absent if discussion phase had no target, but min is research + decision)
    assert len(costs) >= 5

    phases = {c.phase for c in costs}
    assert "research" in phases
    assert "decision" in phases


async def test_stub_cost_is_zero(stub_llm, config, seeded_run, in_memory_db):
    run, matchup = seeded_run
    pipeline = MatchupPipeline(stub_llm, config)
    await pipeline.run(run.id, matchup)

    total = await get_total_cost(run.id)
    assert total == 0.0


# ---------------------------------------------------------------------------
# Service layer
# ---------------------------------------------------------------------------

async def test_get_run_predictions(stub_llm, config, seeded_run, in_memory_db):
    run, matchup = seeded_run
    pipeline = MatchupPipeline(stub_llm, config)
    await pipeline.run(run.id, matchup)

    predictions = await get_run_predictions(run.id)
    assert len(predictions) == 1
    assert predictions[0].matchup_id == matchup.id


async def test_idempotent_bracket_import(in_memory_db):
    """Importing two different brackets should create independent records."""
    b1, _ = await import_bracket(
        2025, "Bracket A",
        [MatchupInput(1, "East", "Duke", 2, "UNC", 7)]
    )
    b2, _ = await import_bracket(
        2025, "Bracket B",
        [MatchupInput(1, "West", "Kansas", 1, "Howard", 16)]
    )
    assert b1.id != b2.id

    async with get_connection() as conn:
        east = await MatchupRepository(conn).list_by_bracket(b1.id)
        west = await MatchupRepository(conn).list_by_bracket(b2.id)

    assert east[0].region == "East"
    assert west[0].region == "West"
