"""Run creation, query, and result retrieval."""

from __future__ import annotations

import logging

from bracket_team.config import AppConfig, get_config
from bracket_team.db.connection import get_connection
from bracket_team.db.models import LLMCost, Prediction, Run
from bracket_team.db.repositories.cost_repo import CostRepository
from bracket_team.db.repositories.prediction_repo import PredictionRepository
from bracket_team.db.repositories.run_repo import RunRepository

logger = logging.getLogger(__name__)


async def create_run(
    bracket_id: int,
    name: str,
    weights: dict[str, float] | None = None,
    risk_appetite: str = "neutral",
    user_preferences: str | None = None,
    config: AppConfig | None = None,
) -> Run:
    cfg = config or get_config()
    analyst_weights = weights or cfg.default_analyst_weights
    async with get_connection() as conn:
        return await RunRepository(conn).create(
            bracket_id=bracket_id,
            name=name,
            analyst_weights=analyst_weights,
            risk_appetite=risk_appetite,
            user_preferences=user_preferences,
        )


async def get_run(run_id: int) -> Run | None:
    async with get_connection() as conn:
        return await RunRepository(conn).get(run_id)


async def list_runs(bracket_id: int) -> list[Run]:
    async with get_connection() as conn:
        return await RunRepository(conn).list_by_bracket(bracket_id)


async def get_run_predictions(run_id: int) -> list[Prediction]:
    async with get_connection() as conn:
        return await PredictionRepository(conn).list_by_run(run_id)


async def get_run_costs(run_id: int) -> list[LLMCost]:
    async with get_connection() as conn:
        return await CostRepository(conn).list_by_run(run_id)


async def get_total_cost(run_id: int) -> float:
    async with get_connection() as conn:
        return await CostRepository(conn).total_cost_for_run(run_id)
