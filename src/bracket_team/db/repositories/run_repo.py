"""Repository for runs table."""

from __future__ import annotations

import json
from typing import Literal

import aiosqlite

from bracket_team.db.models import Run


class RunRepository:
    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn

    async def create(
        self,
        bracket_id: int,
        name: str,
        analyst_weights: dict[str, float],
        risk_appetite: str = "neutral",
        user_preferences: str | None = None,
    ) -> Run:
        cursor = await self._conn.execute(
            """INSERT INTO runs (bracket_id, name, risk_appetite, analyst_weights, user_preferences)
               VALUES (?, ?, ?, ?, ?) RETURNING *""",
            (bracket_id, name, risk_appetite, json.dumps(analyst_weights), user_preferences),
        )
        row = await cursor.fetchone()
        await self._conn.commit()
        return Run(**dict(row))

    async def get(self, run_id: int) -> Run | None:
        cursor = await self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,))
        row = await cursor.fetchone()
        return Run(**dict(row)) if row else None

    async def list_by_bracket(self, bracket_id: int) -> list[Run]:
        cursor = await self._conn.execute(
            "SELECT * FROM runs WHERE bracket_id = ? ORDER BY created_at DESC",
            (bracket_id,),
        )
        rows = await cursor.fetchall()
        return [Run(**dict(r)) for r in rows]

    async def delete(self, run_id: int) -> bool:
        """Delete a run and all related rows. Returns True if run existed."""
        cursor = await self._conn.execute(
            "SELECT id FROM runs WHERE id = ?", (run_id,)
        )
        if not await cursor.fetchone():
            return False
        await self._conn.execute("DELETE FROM llm_costs WHERE run_id = ?", (run_id,))
        await self._conn.execute("DELETE FROM predictions WHERE run_id = ?", (run_id,))
        await self._conn.execute("DELETE FROM discussion_messages WHERE run_id = ?", (run_id,))
        await self._conn.execute("DELETE FROM analyst_reports WHERE run_id = ?", (run_id,))
        await self._conn.execute("DELETE FROM matchups WHERE run_id = ?", (run_id,))
        await self._conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        await self._conn.commit()
        return True

    async def update_status(
        self, run_id: int, status: str, error_message: str | None = None
    ) -> None:
        completed_at = "datetime('now')" if status in ("completed", "error") else "NULL"
        clear_progress = status != "running"
        await self._conn.execute(
            f"UPDATE runs SET status = ?, completed_at = {completed_at}, error_message = ?"
            f"{', progress_info = NULL' if clear_progress else ''} WHERE id = ?",
            (status, error_message, run_id),
        )
        await self._conn.commit()

    async def update_progress(
        self,
        run_id: int,
        teams: str,
        phase: Literal["research", "discussion", "decision"],
    ) -> None:
        info = json.dumps({"teams": teams, "phase": phase})
        await self._conn.execute(
            "UPDATE runs SET progress_info = ? WHERE id = ?",
            (info, run_id),
        )
        await self._conn.commit()
