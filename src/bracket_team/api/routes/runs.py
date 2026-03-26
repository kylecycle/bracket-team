"""Run routes: create runs, trigger analysis, poll status, view results."""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from bracket_team.api.app import _require_api_key
from bracket_team.config import get_config
from bracket_team.db.connection import get_connection
from bracket_team.db.models import LLMCost, Prediction, Run
from bracket_team.db.repositories.run_repo import RunRepository
from bracket_team.service.run_service import (
    create_run,
    get_run,
    get_run_costs,
    get_run_predictions,
    get_total_cost,
    list_runs,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/runs", dependencies=[Depends(_require_api_key)])


class CreateRunRequest(BaseModel):
    bracket_id: int
    name: str = "API Run"
    user_preferences: str | None = None


class RunDetail(BaseModel):
    run: Run
    predictions: list[Prediction]
    total_cost_usd: float


class AnalyzeResponse(BaseModel):
    run_id: int
    status: str
    message: str


@router.post("", response_model=Run, status_code=201)
async def api_create_run(body: CreateRunRequest) -> Run:
    return await create_run(
        bracket_id=body.bracket_id,
        name=body.name,
        user_preferences=body.user_preferences,
    )


@router.get("", response_model=list[Run])
async def api_list_runs(bracket_id: int) -> list[Run]:
    return await list_runs(bracket_id)


@router.get("/{run_id}", response_model=RunDetail)
async def api_get_run(run_id: int) -> RunDetail:
    run = await get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    predictions = await get_run_predictions(run_id)
    total_cost = await get_total_cost(run_id)
    return RunDetail(run=run, predictions=predictions, total_cost_usd=total_cost)


@router.post("/{run_id}/analyze", response_model=AnalyzeResponse, status_code=202)
async def api_analyze(run_id: int, background_tasks: BackgroundTasks) -> AnalyzeResponse:
    run = await get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    if run.status == "running":
        raise HTTPException(status_code=409, detail="Run is already in progress")
    if run.status == "completed":
        raise HTTPException(status_code=409, detail="Run already completed")

    resuming = run.status in ("error", "paused")
    async with get_connection() as conn:
        await RunRepository(conn).update_status(run_id, "running")
    background_tasks.add_task(_run_pipeline, run_id)
    return AnalyzeResponse(
        run_id=run_id,
        status="running",
        message=(
            f"Resuming run from last checkpoint. Poll GET /api/runs/{run_id} for status."
            if resuming else
            f"Analysis started. Poll GET /api/runs/{run_id} for status."
        ),
    )


@router.post("/{run_id}/pause", status_code=200)
async def api_pause_run(run_id: int) -> dict:
    """Pause a stuck 'running' run so it can be resumed later."""
    run = await get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    if run.status == "completed":
        raise HTTPException(status_code=409, detail="Run already completed")

    async with get_connection() as conn:
        await RunRepository(conn).update_status(run_id, "paused")
    return {"run_id": run_id, "status": "paused"}


@router.delete("/{run_id}", status_code=204)
async def api_delete_run(run_id: int) -> Response:
    run = await get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    if run.status == "running":
        raise HTTPException(status_code=409, detail="Cannot delete a run that is in progress")
    async with get_connection() as conn:
        await RunRepository(conn).delete(run_id)
    return Response(status_code=204)


@router.get("/{run_id}/costs", response_model=list[LLMCost])
async def api_get_costs(run_id: int) -> list[LLMCost]:
    run = await get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    return await get_run_costs(run_id)


async def _run_pipeline(run_id: int) -> None:
    """Background task: mark run running, execute pipeline, mark completed/error."""
    from bracket_team.agents.llm import create_backend
    from bracket_team.exceptions import FatalLLMError
    from bracket_team.service.pipeline import analyze_bracket

    cfg = get_config()

    try:
        llm = create_backend(cfg)
        await analyze_bracket(run_id, llm, cfg)
        # Only mark completed if not paused mid-run
        async with get_connection() as conn:
            current = await RunRepository(conn).get(run_id)
        if current and current.status == "running":
            async with get_connection() as conn:
                await RunRepository(conn).update_status(run_id, "completed")
    except FatalLLMError as exc:
        raw = str(exc)
        msg_lower = raw.lower()
        if "credit" in msg_lower or "balance" in msg_lower or "billing" in msg_lower:
            error_message = (
                "Anthropic account has insufficient credits. "
                "Add credits at console.anthropic.com → Plans & Billing."
            )
        elif "401" in raw or "authentication" in msg_lower or "api key" in msg_lower:
            error_message = "Invalid Anthropic API key. Check your ANTHROPIC_API_KEY configuration."
        elif "403" in raw or "permission" in msg_lower or "forbidden" in msg_lower:
            error_message = "Anthropic API access denied. Check your account permissions."
        else:
            error_message = f"Anthropic API error: {raw}"
        logger.error("Fatal LLM error for run_id=%d: %s", run_id, exc)
        async with get_connection() as conn:
            await RunRepository(conn).update_status(run_id, "error", error_message=error_message)
    except Exception as exc:
        # Unwrap ExceptionGroup (from asyncio.TaskGroup) to get the real error
        real_exc: BaseException = exc
        if isinstance(exc, BaseExceptionGroup) and exc.exceptions:
            real_exc = exc.exceptions[0]
        logger.exception("Pipeline failed for run_id=%d", run_id)
        async with get_connection() as conn:
            await RunRepository(conn).update_status(run_id, "error", error_message=str(real_exc))
