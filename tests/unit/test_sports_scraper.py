"""Unit tests for sports_scraper: slug resolution, parsers, and DB upsert."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bracket_team.scraper.sports_scraper import (
    SportsScraper,
    _parse_record,
    _resolve_slug,
    _safe_float,
    _safe_int,
    upsert_team_stats,
)


# ---------------------------------------------------------------------------
# _resolve_slug
# ---------------------------------------------------------------------------

def test_resolve_slug_exact_match():
    slug = _resolve_slug("Duke")
    assert slug == "duke"


def test_resolve_slug_case_insensitive():
    slug = _resolve_slug("duke")
    assert slug == "duke"


def test_resolve_slug_fuzzy_match():
    # "Gonzaga" is in the slug map; a close variant should still match
    slug = _resolve_slug("Gonzaga")
    assert slug is not None


def test_resolve_slug_unknown_team():
    slug = _resolve_slug("Nonexistent University ZZZZZ")
    assert slug is None


# ---------------------------------------------------------------------------
# _parse_record
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("25-8", (25, 8)),
    ("30-5 (15-3 ACC)", (30, 5)),
    ("10-0", (10, 0)),
    ("garbage", (None, None)),
    ("", (None, None)),
])
def test_parse_record(text, expected):
    assert _parse_record(text) == expected


# ---------------------------------------------------------------------------
# _safe_float / _safe_int
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("3.14", 3.14),
    ("1,234.5", 1234.5),
    ("", None),
    ("n/a", None),
])
def test_safe_float(text, expected):
    assert _safe_float(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("42", 42),
    ("1,000", 1000),
    ("", None),
    ("abc", None),
])
def test_safe_int(text, expected):
    assert _safe_int(text) == expected


# ---------------------------------------------------------------------------
# upsert_team_stats — DB round-trip
# ---------------------------------------------------------------------------

async def _create_bracket(bracket_id: int = 1) -> None:
    from bracket_team.db.connection import get_connection

    async with get_connection() as conn:
        await conn.execute(
            "INSERT INTO brackets (id, year, tournament_name) VALUES (?, ?, ?)",
            (bracket_id, 2025, "Test"),
        )
        await conn.commit()


async def test_upsert_team_stats_inserts(in_memory_db):
    from bracket_team.db.connection import get_connection

    await _create_bracket()
    stats = {
        "bracket_id": 1,
        "team_name": "Duke",
        "season_wins": 30,
        "season_losses": 5,
        "conf_wins": 15,
        "conf_losses": 3,
        "conference": "ACC",
        "sos_rank": 12,
        "net_rank": None,
        "srs": 24.3,
        "adj_off_eff": 118.5,
        "adj_def_eff": 91.2,
        "pace": 70.1,
        "fg_pct": 0.478,
        "three_pt_pct": 0.362,
        "conf_tourney_wins": 2,
        "conf_tourney_losses": 1,
        "head_coach": "Jon Scheyer",
        "coach_tourney_appearances": None,
        "freshmen_count": 3,
        "senior_count": 4,
        "transfer_count": 2,
    }
    async with get_connection() as conn:
        await upsert_team_stats(conn, stats)
        async with conn.execute(
            "SELECT * FROM team_stats WHERE bracket_id=1 AND team_name='Duke'"
        ) as cursor:
            row = await cursor.fetchone()

    assert row is not None
    row = dict(row)
    assert row["season_wins"] == 30
    assert row["srs"] == pytest.approx(24.3)
    assert row["conference"] == "ACC"


async def test_upsert_team_stats_overwrites_on_conflict(in_memory_db):
    from bracket_team.db.connection import get_connection

    await _create_bracket()
    base = {
        "bracket_id": 1, "team_name": "Duke",
        "season_wins": 25, "season_losses": 10,
        "conf_wins": None, "conf_losses": None, "conference": None,
        "sos_rank": None, "net_rank": None, "srs": 20.0,
        "adj_off_eff": None, "adj_def_eff": None, "pace": None,
        "fg_pct": None, "three_pt_pct": None,
        "conf_tourney_wins": 0, "conf_tourney_losses": 0,
        "head_coach": None, "coach_tourney_appearances": None,
        "freshmen_count": None, "senior_count": None, "transfer_count": None,
    }
    updated = {**base, "season_wins": 30, "srs": 24.3}

    async with get_connection() as conn:
        await upsert_team_stats(conn, base)
        await upsert_team_stats(conn, updated)
        async with conn.execute(
            "SELECT season_wins, srs FROM team_stats WHERE bracket_id=1 AND team_name='Duke'"
        ) as cursor:
            row = dict(await cursor.fetchone())

    assert row["season_wins"] == 30
    assert row["srs"] == pytest.approx(24.3)


# ---------------------------------------------------------------------------
# SportsScraper._extract_roster — HTML parsing (no network)
# ---------------------------------------------------------------------------

def _make_roster_html(rows: list[dict]) -> str:
    """Build minimal Sports Reference roster table HTML."""
    trs = ""
    for r in rows:
        trs += (
            f'<tr><td data-stat="player">{r.get("player", "")}</td>'
            f'<td data-stat="class">{r.get("class", "")}</td></tr>'
        )
    return f"""
    <html><body>
    <table id="roster"><tbody>{trs}</tbody></table>
    </body></html>
    """


def test_extract_roster_counts():
    from bs4 import BeautifulSoup

    html = _make_roster_html([
        {"player": "Alice", "class": "FR"},
        {"player": "Bob", "class": "SR"},
        {"player": "Carol*", "class": "JR"},  # transfer
        {"player": "Dave", "class": "FR"},
        {"player": "Eve", "class": "SO"},
    ])
    soup = BeautifulSoup(html, "lxml")
    scraper = SportsScraper(year=2025)
    result = scraper._extract_roster(soup)

    assert result["freshmen_count"] == 2
    assert result["senior_count"] == 1
    assert result["transfer_count"] == 1


def test_extract_roster_empty_table():
    from bs4 import BeautifulSoup

    html = "<html><body><table id='roster'><tbody></tbody></table></body></html>"
    soup = BeautifulSoup(html, "lxml")
    scraper = SportsScraper(year=2025)
    result = scraper._extract_roster(soup)

    # All counts None (0 evaluated as falsy → stored as None)
    assert result.get("freshmen_count") is None
    assert result.get("senior_count") is None


def test_extract_roster_no_table():
    from bs4 import BeautifulSoup

    soup = BeautifulSoup("<html><body></body></html>", "lxml")
    scraper = SportsScraper(year=2025)
    result = scraper._extract_roster(soup)
    assert result == {}
