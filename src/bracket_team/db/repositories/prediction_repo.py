"""Repository for predictions table."""

from __future__ import annotations

import aiosqlite

from bracket_team.db.models import Prediction


class PredictionRepository:
    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn

    async def create(
        self,
        run_id: int,
        matchup_id: int,
        predicted_winner: str,
        outcome_type: str,
        weighted_score: float,
        confidence: str,
        synthesis: str,
        manager_model: str,
        status: str = "completed",
    ) -> Prediction:
        cursor = await self._conn.execute(
            """INSERT INTO predictions
               (run_id, matchup_id, predicted_winner, outcome_type, weighted_score,
                confidence, synthesis, manager_model, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *""",
            (run_id, matchup_id, predicted_winner, outcome_type, weighted_score,
             confidence, synthesis, manager_model, status),
        )
        row = await cursor.fetchone()
        await self._conn.commit()
        return Prediction(**dict(row))

    async def get_by_matchup(self, run_id: int, matchup_id: int) -> Prediction | None:
        cursor = await self._conn.execute(
            "SELECT * FROM predictions WHERE run_id = ? AND matchup_id = ?",
            (run_id, matchup_id),
        )
        row = await cursor.fetchone()
        return Prediction(**dict(row)) if row else None

    async def list_by_run(self, run_id: int) -> list[Prediction]:
        cursor = await self._conn.execute(
            "SELECT * FROM predictions WHERE run_id = ? ORDER BY id",
            (run_id,),
        )
        rows = await cursor.fetchall()
        return [Prediction(**dict(r)) for r in rows]
