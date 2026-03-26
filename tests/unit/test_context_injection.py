"""Unit tests for analyst context injection: DB queries, formatters, graceful degradation."""

from __future__ import annotations

import pytest

from bracket_team.agents.analysts import (
    _build_context_block,
    _fetch_context,
    _format_odds,
    _format_roster,
    _format_seed_history,
    _format_sports_stats,
    _matchup_message,
)
from bracket_team.db.models import Matchup
from bracket_team.scraper.seed_history import write_seed_history
from bracket_team.scraper.sports_scraper import upsert_team_stats


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def bracket_in_db(in_memory_db):
    """Ensure bracket id=1 exists so FK constraints on team_stats pass."""
    from bracket_team.db.connection import get_connection

    async with get_connection() as conn:
        await conn.execute(
            "INSERT INTO brackets (id, year, tournament_name) VALUES (1, 2025, 'Test')",
        )
        await conn.commit()


@pytest.fixture
def matchup() -> Matchup:
    return Matchup(
        id=1,
        bracket_id=1,
        round_num=1,
        region="East",
        favorite_name="Duke",
        favorite_seed=2,
        underdog_name="UNC",
        underdog_seed=15,
        created_at="2025-03-14T00:00:00",
    )


@pytest.fixture
def duke_stats() -> dict:
    return {
        "bracket_id": 1,
        "team_name": "Duke",
        "season_wins": 30, "season_losses": 5,
        "conf_wins": 15, "conf_losses": 3,
        "conference": "ACC",
        "sos_rank": 12, "net_rank": None,
        "srs": 24.3, "adj_off_eff": 118.5, "adj_def_eff": 91.2,
        "pace": 70.1, "fg_pct": 0.478, "three_pt_pct": 0.362,
        "conf_tourney_wins": 2, "conf_tourney_losses": 1,
        "head_coach": "Jon Scheyer", "coach_tourney_appearances": None,
        "freshmen_count": 3, "senior_count": 4, "transfer_count": 2,
    }


# ---------------------------------------------------------------------------
# Formatter unit tests (pure functions — no DB needed)
# ---------------------------------------------------------------------------

def test_format_sports_stats_full():
    stats = {
        "season_wins": 30, "season_losses": 5,
        "conf_wins": 15, "conf_losses": 3,
        "conference": "ACC",
        "srs": 24.3, "sos_rank": 12,
        "adj_off_eff": 118.5, "adj_def_eff": 91.2,
        "pace": 70.1, "fg_pct": 0.478, "three_pt_pct": 0.362,
        "head_coach": "Jon Scheyer",
        "conf_tourney_wins": 2, "conf_tourney_losses": 1,
    }
    result = _format_sports_stats("Duke", stats)
    assert "Duke" in result
    assert "30-5" in result
    assert "ACC" in result
    assert "24.3" in result
    assert "Jon Scheyer" in result
    assert "2-1" in result  # conf tourney


def test_format_sports_stats_minimal():
    result = _format_sports_stats("Duke", {})
    assert "Duke" in result


def test_format_seed_history():
    h = {
        "favorite_seed": 2, "underdog_seed": 15,
        "total_games": 160, "favorite_wins": 151, "underdog_wins": 9,
        "favorite_win_pct": 94.4, "upset_rate_pct": 5.6,
        "notable_pattern": "2-seeds occasionally fall",
    }
    result = _format_seed_history(h)
    assert "2-seed" in result
    assert "15-seed" in result
    assert "94.4%" in result
    assert "5.6%" in result
    assert "2-seeds occasionally fall" in result


def test_format_odds_full():
    odds = {
        "favorite_name": "Duke", "underdog_name": "UNC",
        "spread": -6.5, "favorite_ml": -270, "underdog_ml": 220,
        "over_under": 145.5,
        "implied_fav_win_pct": 0.73, "implied_dog_win_pct": 0.27,
    }
    result = _format_odds(odds)
    assert "-6.5" in result
    assert "Duke" in result
    assert "145.5" in result
    assert "73.0%" in result


def test_format_roster():
    stats = {"freshmen_count": 3, "senior_count": 4, "transfer_count": 2}
    result = _format_roster("Duke", stats)
    assert "FR: 3" in result
    assert "SR: 4" in result
    assert "transfers: 2" in result


def test_format_roster_empty():
    result = _format_roster("Duke", {})
    assert result == ""


# ---------------------------------------------------------------------------
# _matchup_message
# ---------------------------------------------------------------------------

def test_matchup_message_no_context(matchup):
    msg = _matchup_message(matchup)
    assert "Duke" in msg
    assert "UNC" in msg
    assert "Produce your structured AnalystReport now." in msg
    assert "===" not in msg


def test_matchup_message_with_context(matchup):
    msg = _matchup_message(matchup, context_block="=== DATA ===\nsome stats")
    assert "=== DATA ===" in msg
    assert "Produce your structured AnalystReport now." in msg


# ---------------------------------------------------------------------------
# _build_context_block — DB integration tests
# ---------------------------------------------------------------------------

async def test_context_block_sports_analyst_with_data(bracket_in_db, matchup, duke_stats):
    from bracket_team.db.connection import get_connection

    async with get_connection() as conn:
        await upsert_team_stats(conn, duke_stats)
        result = await _build_context_block(conn, matchup, "sports_analyst")

    assert "PRE-SCRAPED SPORTS DATA" in result
    assert "Duke" in result
    assert "30-5" in result
    assert "24.3" in result


async def test_context_block_sports_analyst_no_data(in_memory_db, matchup):
    from bracket_team.db.connection import get_connection

    async with get_connection() as conn:
        result = await _build_context_block(conn, matchup, "sports_analyst")

    assert result == ""


async def test_context_block_historical_analyst_with_seed_history(in_memory_db, matchup):
    from bracket_team.db.connection import get_connection

    async with get_connection() as conn:
        await write_seed_history(conn)
        result = await _build_context_block(conn, matchup, "historical_analyst")

    assert "PRE-SCRAPED HISTORICAL DATA" in result
    assert "2-seed" in result
    assert "15-seed" in result


async def test_context_block_odds_analyst_no_data(in_memory_db, matchup):
    from bracket_team.db.connection import get_connection

    async with get_connection() as conn:
        result = await _build_context_block(conn, matchup, "odds_analyst")

    assert result == ""


async def test_context_block_injury_analyst_with_data(bracket_in_db, matchup, duke_stats):
    from bracket_team.db.connection import get_connection

    async with get_connection() as conn:
        await upsert_team_stats(conn, duke_stats)
        result = await _build_context_block(conn, matchup, "injury_analyst")

    assert "ROSTER & INJURY STATUS" in result
    assert "Duke" in result


# ---------------------------------------------------------------------------
# _fetch_context — graceful degradation
# ---------------------------------------------------------------------------

async def test_fetch_context_returns_empty_string_on_exception(matchup):
    """_fetch_context should swallow DB errors and return empty string."""
    with pytest.raises(Exception):
        raise RuntimeError("should not propagate")

    # Verify the graceful path: patch get_connection to raise
    from unittest.mock import patch, AsyncMock
    import bracket_team.agents.analysts as analysts_module

    class _FailingCtx:
        async def __aenter__(self):
            raise RuntimeError("DB unavailable")
        async def __aexit__(self, *a):
            pass

    with patch.object(analysts_module, "get_connection", return_value=_FailingCtx()):
        result = await _fetch_context(matchup, "sports_analyst")

    assert result == ""
