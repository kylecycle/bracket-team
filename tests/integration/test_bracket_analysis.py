"""
Integration tests for full bracket analysis (round-by-round propagation).
Uses StubBackend — no API calls.
"""

from __future__ import annotations

import pytest

from bracket_team.agents.llm import StubBackend
from bracket_team.config import AppConfig
from bracket_team.db.connection import get_connection
from bracket_team.db.repositories.matchup_repo import MatchupRepository
from bracket_team.db.repositories.prediction_repo import PredictionRepository
from bracket_team.service.bracket_service import MatchupInput, import_bracket
from bracket_team.service.pipeline import analyze_bracket
from bracket_team.service.run_service import create_run


@pytest.fixture
def config() -> AppConfig:
    return AppConfig(anthropic_api_key="stub")


@pytest.fixture
def stub_llm() -> StubBackend:
    return StubBackend()


def _make_matchup(round_num, region, fav, fav_seed, dog, dog_seed) -> MatchupInput:
    return MatchupInput(
        round_num=round_num, region=region,
        favorite_name=fav, favorite_seed=fav_seed,
        underdog_name=dog, underdog_seed=dog_seed,
    )


# ---------------------------------------------------------------------------
# Two-team bracket (1 matchup → 1 round → done)
# ---------------------------------------------------------------------------

async def test_single_matchup_bracket(stub_llm, config, in_memory_db):
    bracket, _ = await import_bracket(
        2025, "Mini Bracket",
        [_make_matchup(1, "East", "Duke", 2, "UNC", 7)],
    )
    run = await create_run(bracket_id=bracket.id, name="test")
    predictions = await analyze_bracket(run.id, stub_llm, config)

    assert len(predictions) == 1
    assert predictions[0].matchup_id is not None


# ---------------------------------------------------------------------------
# Four-team bracket: 2 round-1 matchups → 1 round-2 final
# ---------------------------------------------------------------------------

async def test_four_team_bracket_creates_second_round(stub_llm, config, in_memory_db):
    bracket, _ = await import_bracket(
        2025, "Four Team",
        [
            _make_matchup(1, "East", "Kansas", 1, "Howard", 16),
            _make_matchup(1, "East", "Duke", 2, "UNC", 7),
        ],
    )
    run = await create_run(bracket_id=bracket.id, name="test")
    predictions = await analyze_bracket(run.id, stub_llm, config)

    # Round 1: 2 matchups + Round 2: 1 matchup = 3 total
    assert len(predictions) == 3


async def test_four_team_bracket_round2_matchup_created(stub_llm, config, in_memory_db):
    bracket, _ = await import_bracket(
        2025, "Four Team",
        [
            _make_matchup(1, "East", "Kansas", 1, "Howard", 16),
            _make_matchup(1, "East", "Duke", 2, "UNC", 7),
        ],
    )
    run = await create_run(bracket_id=bracket.id, name="test")
    await analyze_bracket(run.id, stub_llm, config)

    async with get_connection() as conn:
        all_matchups = await MatchupRepository(conn).list_by_bracket(bracket.id)
        round2 = [m for m in all_matchups if m.round_num == 2]

    assert len(round2) == 1, "Round 2 matchup should be auto-created"


async def test_four_team_bracket_winner_propagated(stub_llm, config, in_memory_db):
    """Round-2 matchup teams must come from round-1 original rosters."""
    bracket, round1_matchups = await import_bracket(
        2025, "Four Team",
        [
            _make_matchup(1, "East", "Kansas", 1, "Howard", 16),
            _make_matchup(1, "East", "Duke", 2, "UNC", 7),
        ],
    )
    run = await create_run(bracket_id=bracket.id, name="test")
    await analyze_bracket(run.id, stub_llm, config)

    all_round1_teams = {
        name
        for m in round1_matchups
        for name in (m.favorite_name, m.underdog_name)
    }

    async with get_connection() as conn:
        all_matchups = await MatchupRepository(conn).list_by_bracket(bracket.id)
        round2 = [m for m in all_matchups if m.round_num == 2][0]

    assert round2.favorite_name in all_round1_teams
    assert round2.underdog_name in all_round1_teams


# ---------------------------------------------------------------------------
# Eight-team bracket: 4 → 2 → 1 (three rounds)
# ---------------------------------------------------------------------------

async def test_eight_team_three_rounds(stub_llm, config, in_memory_db):
    bracket, _ = await import_bracket(
        2025, "Eight Team",
        [
            _make_matchup(1, "East", "Kansas", 1, "Howard", 16),
            _make_matchup(1, "East", "Villanova", 4, "Oregon", 13),
            _make_matchup(1, "East", "Duke", 2, "UNC", 7),
            _make_matchup(1, "East", "Purdue", 3, "Vermont", 14),
        ],
    )
    run = await create_run(bracket_id=bracket.id, name="test")
    predictions = await analyze_bracket(run.id, stub_llm, config)

    # Round 1: 4 + Round 2: 2 + Round 3: 1 = 7
    assert len(predictions) == 7


async def test_eight_team_all_rounds_have_predictions(stub_llm, config, in_memory_db):
    bracket, _ = await import_bracket(
        2025, "Eight Team",
        [
            _make_matchup(1, "East", "Kansas", 1, "Howard", 16),
            _make_matchup(1, "East", "Villanova", 4, "Oregon", 13),
            _make_matchup(1, "East", "Duke", 2, "UNC", 7),
            _make_matchup(1, "East", "Purdue", 3, "Vermont", 14),
        ],
    )
    run = await create_run(bracket_id=bracket.id, name="test")
    await analyze_bracket(run.id, stub_llm, config)

    async with get_connection() as conn:
        all_matchups = await MatchupRepository(conn).list_by_bracket(bracket.id)
        all_preds = await PredictionRepository(conn).list_by_run(run.id)

    # Every matchup should have exactly one prediction
    assert len(all_preds) == len(all_matchups)


# ---------------------------------------------------------------------------
# Progress callback
# ---------------------------------------------------------------------------

async def test_progress_callback_called(stub_llm, config, in_memory_db):
    bracket, _ = await import_bracket(
        2025, "Callback Test",
        [
            _make_matchup(1, "East", "Kansas", 1, "Howard", 16),
            _make_matchup(1, "East", "Duke", 2, "UNC", 7),
        ],
    )
    run = await create_run(bracket_id=bracket.id, name="test")

    calls = []
    def _cb(round_num, total_rounds, done, total):
        calls.append((round_num, done, total))

    await analyze_bracket(run.id, stub_llm, config, progress_callback=_cb)

    # Round 1 has 2 matchups, round 2 has 1 — expect 3 callbacks
    assert len(calls) == 3
    round_nums = [c[0] for c in calls]
    assert 1 in round_nums
    assert 2 in round_nums
