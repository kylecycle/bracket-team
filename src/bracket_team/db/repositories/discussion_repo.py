"""Repository for discussion_messages table."""

from __future__ import annotations

import aiosqlite

from bracket_team.db.models import DiscussionMessage


class DiscussionRepository:
    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn

    async def create(
        self,
        run_id: int,
        matchup_id: int,
        phase: str,
        author_role: str,
        content: str,
        target_role: str | None = None,
        steelman: str | None = None,
    ) -> DiscussionMessage:
        cursor = await self._conn.execute(
            """INSERT INTO discussion_messages
               (run_id, matchup_id, phase, author_role, target_role, steelman, content)
               VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING *""",
            (run_id, matchup_id, phase, author_role, target_role, steelman, content),
        )
        row = await cursor.fetchone()
        await self._conn.commit()
        return DiscussionMessage(**dict(row))

    async def list_by_matchup(
        self, run_id: int, matchup_id: int
    ) -> list[DiscussionMessage]:
        cursor = await self._conn.execute(
            """SELECT * FROM discussion_messages
               WHERE run_id = ? AND matchup_id = ? ORDER BY id""",
            (run_id, matchup_id),
        )
        rows = await cursor.fetchall()
        return [DiscussionMessage(**dict(r)) for r in rows]
