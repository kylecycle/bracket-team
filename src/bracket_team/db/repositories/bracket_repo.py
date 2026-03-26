"""Repository for brackets table."""

from __future__ import annotations

import aiosqlite

from bracket_team.db.models import Bracket


class BracketRepository:
    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn

    async def create(self, year: int, tournament_name: str) -> Bracket:
        cursor = await self._conn.execute(
            "INSERT INTO brackets (year, tournament_name) VALUES (?, ?) RETURNING *",
            (year, tournament_name),
        )
        row = await cursor.fetchone()
        await self._conn.commit()
        return Bracket(**dict(row))

    async def get(self, bracket_id: int) -> Bracket | None:
        cursor = await self._conn.execute(
            "SELECT * FROM brackets WHERE id = ?", (bracket_id,)
        )
        row = await cursor.fetchone()
        return Bracket(**dict(row)) if row else None

    async def list_all(self) -> list[Bracket]:
        cursor = await self._conn.execute(
            "SELECT * FROM brackets ORDER BY created_at DESC"
        )
        rows = await cursor.fetchall()
        return [Bracket(**dict(r)) for r in rows]

    async def delete(self, bracket_id: int) -> bool:
        """Delete a bracket and all related rows. Returns True if bracket existed."""
        # Check existence first
        cursor = await self._conn.execute(
            "SELECT id FROM brackets WHERE id = ?", (bracket_id,)
        )
        if not await cursor.fetchone():
            return False
        # Delete child rows in dependency order
        await self._conn.execute(
            """DELETE FROM llm_costs WHERE run_id IN
               (SELECT id FROM runs WHERE bracket_id = ?)""", (bracket_id,)
        )
        await self._conn.execute(
            """DELETE FROM predictions WHERE run_id IN
               (SELECT id FROM runs WHERE bracket_id = ?)""", (bracket_id,)
        )
        await self._conn.execute(
            """DELETE FROM discussion_messages WHERE run_id IN
               (SELECT id FROM runs WHERE bracket_id = ?)""", (bracket_id,)
        )
        await self._conn.execute(
            """DELETE FROM analyst_reports WHERE run_id IN
               (SELECT id FROM runs WHERE bracket_id = ?)""", (bracket_id,)
        )
        await self._conn.execute(
            "DELETE FROM runs WHERE bracket_id = ?", (bracket_id,)
        )
        await self._conn.execute(
            "DELETE FROM matchups WHERE bracket_id = ?", (bracket_id,)
        )
        await self._conn.execute(
            "DELETE FROM brackets WHERE id = ?", (bracket_id,)
        )
        await self._conn.commit()
        return True
