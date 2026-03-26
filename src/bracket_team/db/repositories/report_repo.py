"""Repository for analyst_reports table."""

from __future__ import annotations

import aiosqlite

from bracket_team.db.models import AnalystReport


class ReportRepository:
    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn

    async def create(
        self,
        run_id: int,
        matchup_id: int,
        analyst_role: str,
        pick: str,
        score: int,
        relevance: str,
        thesis: str,
    ) -> AnalystReport:
        cursor = await self._conn.execute(
            """INSERT INTO analyst_reports
               (run_id, matchup_id, analyst_role, pick, score, relevance, thesis)
               VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING *""",
            (run_id, matchup_id, analyst_role, pick, score, relevance, thesis),
        )
        row = await cursor.fetchone()
        await self._conn.commit()
        return AnalystReport(**dict(row))

    async def list_by_matchup(self, run_id: int, matchup_id: int) -> list[AnalystReport]:
        cursor = await self._conn.execute(
            "SELECT * FROM analyst_reports WHERE run_id = ? AND matchup_id = ? ORDER BY id",
            (run_id, matchup_id),
        )
        rows = await cursor.fetchall()
        return [AnalystReport(**dict(r)) for r in rows]
