"""Shared test fixtures: in-memory SQLite, mock AgentLLM, sample Matchup."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from bracket_team.agents.llm import AgentConfig, LLMResponse
from bracket_team.agents.schemas import (
    AnalystReport,
    DiscussionChallenge,
    DiscussionRebuttal,
    ManagerPrediction,
)
from bracket_team.db.connection import init_db
from bracket_team.db.models import Matchup


@pytest.fixture(autouse=True)
async def in_memory_db(tmp_path):
    """Use a fresh file-based SQLite DB per test (aiosqlite doesn't support :memory: well)."""
    db_file = str(tmp_path / "test.db")
    await init_db(db_file)
    yield
    # cleanup handled by tmp_path


@pytest.fixture
def sample_matchup() -> Matchup:
    return Matchup(
        id=1,
        bracket_id=1,
        round_num=1,
        region="East",
        favorite_name="Duke",
        favorite_seed=2,
        underdog_name="UNC",
        underdog_seed=7,
        created_at="2025-03-14T00:00:00",
    )


@pytest.fixture
def sample_reports() -> list[tuple[str, AnalystReport]]:
    return [
        ("sports_analyst", AnalystReport(
            pick="favorite", score=3, relevance="high", thesis="Duke plays better defense."
        )),
        ("odds_analyst", AnalystReport(
            pick="favorite", score=2, relevance="medium", thesis="Markets lean Duke -6."
        )),
        ("historical_analyst", AnalystReport(
            pick="underdog", score=-1, relevance="medium", thesis="7-seeds win 35% of the time."
        )),
        ("injury_analyst", AnalystReport(
            pick="favorite", score=1, relevance="low", thesis="Both rosters healthy."
        )),
    ]


def _make_llm_response(content: str, model: str = "claude-sonnet-4-5") -> LLMResponse:
    return LLMResponse(
        content=content,
        input_tokens=100,
        output_tokens=50,
        model=model,
        cost_usd=0.001,
    )


@pytest.fixture
def mock_llm(sample_reports):
    """Mock AgentLLM that returns canned structured responses."""
    llm = AsyncMock()

    def _side_effect(config: AgentConfig, user_message: str, response_schema=None):
        if response_schema is AnalystReport:
            role = config.role
            # pick the matching report or default to first
            report = next(
                (r for role_name, r in sample_reports if role_name == role),
                sample_reports[0][1],
            )
            return _make_llm_response(report.model_dump_json())

        if response_schema is DiscussionChallenge:
            ch = DiscussionChallenge(
                steelman_against_own_pick="Could be an upset.",
                target_analyst="odds_analyst",
                challenge="The line may not reflect late injury news.",
            )
            return _make_llm_response(ch.model_dump_json())

        if response_schema is DiscussionRebuttal:
            rb = DiscussionRebuttal(rebuttal="The market is efficient and I stand by my pick.")
            return _make_llm_response(rb.model_dump_json())

        if response_schema is ManagerPrediction:
            mp = ManagerPrediction(
                predicted_winner="Duke",
                outcome_type="expected",
                weighted_score=1.8,
                synthesis="Duke wins based on strong analyst consensus.",
            )
            return _make_llm_response(mp.model_dump_json())

        return _make_llm_response("generic response")

    llm.generate = AsyncMock(side_effect=_side_effect)
    return llm
