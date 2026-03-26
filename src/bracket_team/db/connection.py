"""Database connection management using aiosqlite."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

_db_path: str = "bracket_team.db"
_lock = asyncio.Lock()


def configure(database_url: str) -> None:
    """Set the database path. Call before first connection."""
    global _db_path
    _db_path = database_url


@asynccontextmanager
async def get_connection() -> AsyncIterator[aiosqlite.Connection]:
    """Yield a connection with row_factory set to aiosqlite.Row."""
    conn = await aiosqlite.connect(_db_path)
    conn.row_factory = aiosqlite.Row
    try:
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        yield conn
    finally:
        await conn.close()


async def init_db(database_url: str | None = None) -> None:
    """Create all tables from schema.sql if they don't exist."""
    if database_url:
        configure(database_url)

    schema_path = Path(__file__).parent / "schema.sql"
    schema_sql = schema_path.read_text()

    async with get_connection() as conn:
        await conn.executescript(schema_sql)
        await conn.commit()

    # Run column migrations for existing databases.
    # Each ALTER TABLE is executed individually so "duplicate column name" errors
    # (which occur when the column already exists) are silently ignored.
    migrations = [
        "ALTER TABLE team_stats ADD COLUMN ft_rate REAL",
        "ALTER TABLE team_stats ADD COLUMN tov_pct REAL",
        "ALTER TABLE team_stats ADD COLUMN neutral_wins INTEGER DEFAULT 0",
        "ALTER TABLE team_stats ADD COLUMN neutral_losses INTEGER DEFAULT 0",
        "ALTER TABLE team_stats ADD COLUMN last10_wins INTEGER",
        "ALTER TABLE team_stats ADD COLUMN last10_losses INTEGER",
        "ALTER TABLE team_stats ADD COLUMN bart_adj_oe REAL",
        "ALTER TABLE team_stats ADD COLUMN bart_adj_de REAL",
        "ALTER TABLE team_stats ADD COLUMN barthag REAL",
        "ALTER TABLE team_stats ADD COLUMN bart_tempo REAL",
        "ALTER TABLE team_stats ADD COLUMN bart_luck REAL",
        "ALTER TABLE team_stats ADD COLUMN bart_wab REAL",
        "ALTER TABLE team_stats ADD COLUMN quad1_wins INTEGER DEFAULT 0",
        "ALTER TABLE team_stats ADD COLUMN quad1_losses INTEGER DEFAULT 0",
        "ALTER TABLE team_stats ADD COLUMN quad2_wins INTEGER DEFAULT 0",
        "ALTER TABLE team_stats ADD COLUMN quad2_losses INTEGER DEFAULT 0",
        "ALTER TABLE team_stats ADD COLUMN coach_tourney_record TEXT",
        "ALTER TABLE team_stats ADD COLUMN ft_pct REAL",
        "ALTER TABLE team_stats ADD COLUMN ppg REAL",
        "ALTER TABLE team_stats ADD COLUMN opp_ppg REAL",
        "ALTER TABLE team_stats ADD COLUMN orb_per_g REAL",
        "ALTER TABLE team_stats ADD COLUMN drb_per_g REAL",
        "ALTER TABLE team_stats ADD COLUMN ast_per_g REAL",
        "ALTER TABLE team_stats ADD COLUMN stl_per_g REAL",
        "ALTER TABLE team_stats ADD COLUMN blk_per_g REAL",
        "ALTER TABLE team_stats ADD COLUMN opp_fg_pct REAL",
        "ALTER TABLE team_stats ADD COLUMN opp_three_pt_pct REAL",
        "ALTER TABLE team_stats ADD COLUMN opp_tov_per_g REAL",
        "ALTER TABLE team_stats ADD COLUMN ap_rank INTEGER",
        "ALTER TABLE team_stats ADD COLUMN quad3_wins INTEGER DEFAULT 0",
        "ALTER TABLE team_stats ADD COLUMN quad3_losses INTEGER DEFAULT 0",
        "ALTER TABLE team_stats ADD COLUMN quad4_wins INTEGER DEFAULT 0",
        "ALTER TABLE team_stats ADD COLUMN quad4_losses INTEGER DEFAULT 0",
        "ALTER TABLE team_player_stats ADD COLUMN ft_pct REAL",
        "ALTER TABLE team_player_stats ADD COLUMN three_pt_pct REAL",
        "ALTER TABLE team_player_stats ADD COLUMN usage_rate REAL",
        "ALTER TABLE team_player_stats ADD COLUMN injured BOOLEAN DEFAULT FALSE",
        "ALTER TABLE team_player_stats ADD COLUMN injury_note TEXT",
        "ALTER TABLE runs ADD COLUMN user_preferences TEXT",
        "ALTER TABLE runs ADD COLUMN error_message TEXT",
        "ALTER TABLE runs ADD COLUMN progress_info TEXT",
        "ALTER TABLE matchups ADD COLUMN run_id INTEGER REFERENCES runs(id)",
        (
            "CREATE TABLE IF NOT EXISTS config_overrides "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL, "
            "updated_at TEXT DEFAULT (datetime('now')))"
        ),
    ]
    async with get_connection() as conn:
        for stmt in migrations:
            try:
                await conn.execute(stmt)
                await conn.commit()
            except Exception as exc:
                if "duplicate column name" not in str(exc).lower():
                    logger.warning("Migration failed for %r: %s", stmt, exc)
