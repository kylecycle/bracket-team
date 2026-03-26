"""Repository for llm_costs table."""

from __future__ import annotations

import aiosqlite

from bracket_team.db.models import LLMCost


class CostRepository:
    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn

    async def create(
        self,
        run_id: int,
        agent_role: str,
        model: str,
        phase: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        matchup_id: int | None = None,
    ) -> LLMCost:
        cursor = await self._conn.execute(
            """INSERT INTO llm_costs
               (run_id, matchup_id, agent_role, model, phase,
                input_tokens, output_tokens, cost_usd)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING *""",
            (run_id, matchup_id, agent_role, model, phase,
             input_tokens, output_tokens, cost_usd),
        )
        row = await cursor.fetchone()
        await self._conn.commit()
        return LLMCost(**dict(row))

    async def list_by_run(self, run_id: int) -> list[LLMCost]:
        cursor = await self._conn.execute(
            "SELECT * FROM llm_costs WHERE run_id = ? ORDER BY id",
            (run_id,),
        )
        rows = await cursor.fetchall()
        return [LLMCost(**dict(r)) for r in rows]

    async def total_cost_for_run(self, run_id: int) -> float:
        cursor = await self._conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) FROM llm_costs WHERE run_id = ?",
            (run_id,),
        )
        row = await cursor.fetchone()
        return float(row[0])
