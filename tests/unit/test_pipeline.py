"""Unit tests for pipeline orchestration using mock LLM."""

import pytest

from bracket_team.config import AppConfig
from bracket_team.db.connection import get_connection
from bracket_team.db.repositories.prediction_repo import PredictionRepository
from bracket_team.db.repositories.report_repo import ReportRepository
from bracket_team.service.pipeline import MatchupPipeline


@pytest.fixture
def config() -> AppConfig:
    return AppConfig(anthropic_api_key="dummy")


async def test_pipeline_writes_reports(mock_llm, sample_matchup, config, in_memory_db):
    """Pipeline should write 4 analyst reports to DB."""
    # Create a bracket and run first
    from bracket_team.db.connection import get_connection
    from bracket_team.db.repositories.bracket_repo import BracketRepository
    from bracket_team.db.repositories.run_repo import RunRepository

    async with get_connection() as conn:
        bracket = await BracketRepository(conn).create(2025, "Test Tournament")
        run = await RunRepository(conn).create(
            bracket_id=bracket.id,
            name="test run",
            analyst_weights=config.default_analyst_weights,
        )

    # Patch matchup to use the created bracket/run IDs
    from bracket_team.db.models import Matchup
    matchup = Matchup(
        id=1,
        bracket_id=bracket.id,
        round_num=1,
        region="East",
        favorite_name="Duke",
        favorite_seed=2,
        underdog_name="UNC",
        underdog_seed=7,
        created_at="2025-03-14T00:00:00",
    )

    # Create the matchup in DB
    from bracket_team.db.repositories.matchup_repo import MatchupRepository
    async with get_connection() as conn:
        matchup = await MatchupRepository(conn).create(
            bracket_id=bracket.id,
            round_num=1,
            region="East",
            favorite_name="Duke",
            favorite_seed=2,
            underdog_name="UNC",
            underdog_seed=7,
        )

    pipeline = MatchupPipeline(mock_llm, config)
    prediction = await pipeline.run(run.id, matchup)

    assert prediction is not None
    assert prediction.predicted_winner == "Duke"

    async with get_connection() as conn:
        reports = await ReportRepository(conn).list_by_matchup(run.id, matchup.id)

    assert len(reports) == 4


async def test_pipeline_writes_prediction(mock_llm, sample_matchup, config, in_memory_db):
    """Pipeline should write exactly one prediction to DB."""
    from bracket_team.db.repositories.bracket_repo import BracketRepository
    from bracket_team.db.repositories.matchup_repo import MatchupRepository
    from bracket_team.db.repositories.run_repo import RunRepository

    async with get_connection() as conn:
        bracket = await BracketRepository(conn).create(2025, "Test")
        run = await RunRepository(conn).create(
            bracket_id=bracket.id,
            name="test",
            analyst_weights=config.default_analyst_weights,
        )
        matchup = await MatchupRepository(conn).create(
            bracket_id=bracket.id,
            round_num=1,
            region="East",
            favorite_name="Duke",
            favorite_seed=2,
            underdog_name="UNC",
            underdog_seed=7,
        )

    pipeline = MatchupPipeline(mock_llm, config)
    await pipeline.run(run.id, matchup)

    async with get_connection() as conn:
        predictions = await PredictionRepository(conn).list_by_run(run.id)

    assert len(predictions) == 1
    assert predictions[0].predicted_winner == "Duke"
