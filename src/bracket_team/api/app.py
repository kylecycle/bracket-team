"""FastAPI application factory."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Security
from fastapi.responses import FileResponse
from fastapi.security.api_key import APIKeyHeader
from fastapi.staticfiles import StaticFiles

from bracket_team.agents.prompt_loader import set_prompt_overrides
from bracket_team.config import get_config, set_config_overrides
from bracket_team.db.connection import get_connection, init_db

_STATIC_DIR = Path(__file__).parents[3] / "static"

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def _require_api_key(api_key: str | None = Security(_api_key_header)) -> None:
    """No-op if BT_API_KEY is unset. Enforces key match if set."""
    cfg = get_config()
    if cfg.api_key is None:
        return
    expected = cfg.api_key.get_secret_value()
    if api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def create_app() -> FastAPI:
    from bracket_team.api.routes import brackets, data, matchups, results, runs
    from bracket_team.api.routes import config as config_routes

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        cfg = get_config()
        await init_db(cfg.database_url)

        # Load persisted overrides and apply them before handling requests
        async with get_connection() as conn:
            async with conn.execute(
                "SELECT key, value FROM config_overrides"
            ) as cur:
                rows = await cur.fetchall()
        config_dict = {
            k[len("config."):]: json.loads(v)
            for k, v in rows
            if k.startswith("config.")
        }
        prompt_dict = {
            k[len("prompt."):]: v
            for k, v in rows
            if k.startswith("prompt.")
        }
        set_config_overrides(config_dict)
        set_prompt_overrides(prompt_dict)

        yield

    app = FastAPI(
        title="Bracket Team",
        description="Multi-agent NCAA bracket prediction API",
        version="0.1.0",
        lifespan=_lifespan,
    )

    app.include_router(brackets.router)
    app.include_router(runs.router)
    app.include_router(matchups.router)
    app.include_router(results.router)
    app.include_router(data.router)
    app.include_router(config_routes.router)

    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def _index() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html")

    return app
