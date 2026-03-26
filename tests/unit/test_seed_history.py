"""Unit tests for seed_history: DB write, idempotency, and lookup."""

from __future__ import annotations

import pytest

from bracket_team.scraper.seed_history import SEED_HISTORY, get_seed_history, write_seed_history


async def test_write_seed_history_inserts_all_rows(in_memory_db):
    from bracket_team.db.connection import get_connection

    async with get_connection() as conn:
        inserted = await write_seed_history(conn)

    assert inserted == len(SEED_HISTORY)


async def test_write_seed_history_is_idempotent(in_memory_db):
    """Running write twice should not insert duplicates (INSERT OR IGNORE)."""
    from bracket_team.db.connection import get_connection

    async with get_connection() as conn:
        await write_seed_history(conn)
        second_insert = await write_seed_history(conn)

    assert second_insert == 0


async def test_get_seed_history_known_matchup(in_memory_db):
    from bracket_team.db.connection import get_connection

    async with get_connection() as conn:
        await write_seed_history(conn)
        row = await get_seed_history(conn, favorite_seed=1, underdog_seed=16)

    assert row is not None
    assert row["favorite_seed"] == 1
    assert row["underdog_seed"] == 16
    assert row["upset_rate_pct"] < 2.0  # 1-seeds almost never lose
    assert row["notable_pattern"] is not None


async def test_get_seed_history_unknown_matchup(in_memory_db):
    from bracket_team.db.connection import get_connection

    async with get_connection() as conn:
        await write_seed_history(conn)
        row = await get_seed_history(conn, favorite_seed=99, underdog_seed=99)

    assert row is None


async def test_seed_history_five_twelve_upset_rate(in_memory_db):
    """5-12 games are famous for upsets (~36%)."""
    from bracket_team.db.connection import get_connection

    async with get_connection() as conn:
        await write_seed_history(conn)
        row = await get_seed_history(conn, favorite_seed=5, underdog_seed=12)

    assert row is not None
    assert row["upset_rate_pct"] > 30.0
