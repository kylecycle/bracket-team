"""Config routes: read/write runtime config overrides and prompt overrides."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from bracket_team.agents.prompt_loader import (
    PROMPT_NAMES,
    get_prompt,
    get_prompt_default,
    set_prompt_overrides,
)
from bracket_team.api.app import _require_api_key
from bracket_team.config import AppConfig, get_config, set_config_overrides
from bracket_team.db.connection import get_connection

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/config", dependencies=[Depends(_require_api_key)])

# ---------------------------------------------------------------------------
# Field metadata — defines which AppConfig fields are exposed in the UI
# ---------------------------------------------------------------------------

FIELD_SPECS: list[dict[str, Any]] = [
    # Anthropic Models
    {"name": "analyst_model", "label": "Analyst Model", "type": "str", "group": "Models"},
    {"name": "manager_model_contested", "label": "Manager (Contested)", "type": "str", "group": "Models"},
    {"name": "manager_model_moderate", "label": "Manager (Moderate)", "type": "str", "group": "Models"},
    {"name": "manager_model_consensus", "label": "Manager (Consensus)", "type": "str", "group": "Models"},
    # Gemini Models
    {"name": "gemini_analyst_model", "label": "Gemini Analyst Model", "type": "str", "group": "Gemini Models"},
    {"name": "gemini_manager_model_contested", "label": "Gemini Manager (Contested)", "type": "str", "group": "Gemini Models"},
    {"name": "gemini_manager_model_moderate", "label": "Gemini Manager (Moderate)", "type": "str", "group": "Gemini Models"},
    {"name": "gemini_manager_model_consensus", "label": "Gemini Manager (Consensus)", "type": "str", "group": "Gemini Models"},
    {"name": "gemini_rpm", "label": "Rate Limit (req/min)", "type": "int", "group": "Gemini Models"},
    {"name": "gemini_thinking_budget", "label": "Thinking Budget (tokens)", "type": "int", "group": "Gemini Models"},
    # LLM Behavior
    {"name": "llm_temperature", "label": "Temperature", "type": "float", "group": "LLM Behavior"},
    {"name": "llm_max_tokens", "label": "Max Tokens", "type": "int", "group": "LLM Behavior"},
    {"name": "llm_max_retries", "label": "Max Retries", "type": "int", "group": "LLM Behavior"},
    {"name": "gemini_request_delay", "label": "Gemini Request Delay (s)", "type": "float", "group": "LLM Behavior"},
    {"name": "discussion_max_chars", "label": "Discussion Max Chars", "type": "int", "group": "LLM Behavior"},
    # Concurrency
    {"name": "max_concurrent_matchups", "label": "Max Concurrent Matchups", "type": "int", "group": "Concurrency"},
    {"name": "max_concurrent_api_calls", "label": "Max Concurrent API Calls", "type": "int", "group": "Concurrency"},
    # Analyst Weights (virtual fields — backed by default_analyst_weights dict)
    {"name": "w_sports_analyst", "label": "Sports Analyst", "type": "float", "group": "Analyst Weights"},
    {"name": "w_odds_analyst", "label": "Odds Analyst", "type": "float", "group": "Analyst Weights"},
    {"name": "w_historical_analyst", "label": "Historical Analyst", "type": "float", "group": "Analyst Weights"},
    {"name": "w_injury_analyst", "label": "Injury Analyst", "type": "float", "group": "Analyst Weights"},
    # Scoring Thresholds
    {"name": "score_contested_threshold", "label": "Contested Threshold", "type": "float", "group": "Scoring Thresholds"},
    {"name": "score_consensus_threshold", "label": "Consensus Threshold", "type": "float", "group": "Scoring Thresholds"},
    {"name": "confidence_high_threshold", "label": "Confidence High", "type": "float", "group": "Scoring Thresholds"},
    {"name": "confidence_low_threshold", "label": "Confidence Low", "type": "float", "group": "Scoring Thresholds"},
]

_WEIGHT_NAMES = {"w_sports_analyst", "w_odds_analyst", "w_historical_analyst", "w_injury_analyst"}


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class FieldInfo(BaseModel):
    name: str
    label: str
    type: str
    group: str
    value: Any
    default: Any
    is_overridden: bool


class PromptInfo(BaseModel):
    name: str
    content: str
    default_content: str
    is_overridden: bool


class ConfigResponse(BaseModel):
    fields: list[FieldInfo]
    prompts: list[PromptInfo]


class PatchConfigRequest(BaseModel):
    fields: dict[str, Any] | None = None
    prompts: dict[str, str] | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_field_value(name: str, cfg: AppConfig) -> Any:
    """Extract a (possibly virtual) field value from AppConfig."""
    if name.startswith("w_"):
        role = name[2:]
        return cfg.default_analyst_weights.get(role)
    return getattr(cfg, name)


def _build_config_response() -> ConfigResponse:
    import bracket_team.config as _cfg_mod

    cfg = get_config()
    default_cfg = AppConfig()
    current_overrides = _cfg_mod._overrides

    fields_out: list[FieldInfo] = []
    for spec in FIELD_SPECS:
        name = spec["name"]
        value = _get_field_value(name, cfg)
        default = _get_field_value(name, default_cfg)
        if name in _WEIGHT_NAMES:
            is_overridden = "default_analyst_weights" in current_overrides
        else:
            is_overridden = name in current_overrides
        fields_out.append(FieldInfo(
            name=name,
            label=spec["label"],
            type=spec["type"],
            group=spec["group"],
            value=value,
            default=default,
            is_overridden=is_overridden,
        ))

    from bracket_team.agents import prompt_loader as _pl
    prompt_overrides = _pl._overrides

    prompts_out: list[PromptInfo] = []
    for pname in PROMPT_NAMES:
        prompts_out.append(PromptInfo(
            name=pname,
            content=get_prompt(pname),
            default_content=get_prompt_default(pname),
            is_overridden=pname in prompt_overrides,
        ))

    return ConfigResponse(fields=fields_out, prompts=prompts_out)


async def _load_all_overrides(conn) -> tuple[dict[str, Any], dict[str, str]]:
    """Read config_overrides table and split into config/prompt dicts."""
    async with conn.execute("SELECT key, value FROM config_overrides") as cur:
        rows = await cur.fetchall()
    config_dict: dict[str, Any] = {}
    prompt_dict: dict[str, str] = {}
    for row in rows:
        key, val = row[0], row[1]
        if key.startswith("config."):
            config_dict[key[len("config."):]] = json.loads(val)
        elif key.startswith("prompt."):
            prompt_dict[key[len("prompt."):]] = val
    return config_dict, prompt_dict


def _apply_and_reload(config_dict: dict[str, Any], prompt_dict: dict[str, str]) -> None:
    set_config_overrides(config_dict)
    set_prompt_overrides(prompt_dict)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("", response_model=ConfigResponse)
async def get_config_endpoint() -> ConfigResponse:
    return _build_config_response()


@router.patch("", response_model=ConfigResponse)
async def patch_config(body: PatchConfigRequest) -> ConfigResponse:
    async with get_connection() as conn:
        config_dict, prompt_dict = await _load_all_overrides(conn)

        if body.fields:
            # Separate virtual weight fields from real AppConfig fields
            weight_updates = {k: v for k, v in body.fields.items() if k in _WEIGHT_NAMES}
            real_updates = {k: v for k, v in body.fields.items() if k not in _WEIGHT_NAMES}

            # Validate real field names
            valid_names = {s["name"] for s in FIELD_SPECS if s["name"] not in _WEIGHT_NAMES}
            for name in real_updates:
                if name not in valid_names:
                    raise HTTPException(status_code=422, detail=f"Unknown config field: {name!r}")

            # Update real scalar fields
            for name, value in real_updates.items():
                config_dict[name] = value
                await conn.execute(
                    "INSERT OR REPLACE INTO config_overrides (key, value, updated_at) "
                    "VALUES (?, ?, datetime('now'))",
                    (f"config.{name}", json.dumps(value)),
                )

            # Update analyst weights (merge into current dict, store as one key)
            if weight_updates:
                current_weights = dict(get_config().default_analyst_weights)
                for name, value in weight_updates.items():
                    role = name[2:]  # "sports_analyst" etc.
                    current_weights[role] = float(value)
                config_dict["default_analyst_weights"] = current_weights
                await conn.execute(
                    "INSERT OR REPLACE INTO config_overrides (key, value, updated_at) "
                    "VALUES (?, ?, datetime('now'))",
                    ("config.default_analyst_weights", json.dumps(current_weights)),
                )

        if body.prompts:
            for pname, content in body.prompts.items():
                if pname not in PROMPT_NAMES:
                    raise HTTPException(status_code=422, detail=f"Unknown prompt: {pname!r}")
                prompt_dict[pname] = content
                await conn.execute(
                    "INSERT OR REPLACE INTO config_overrides (key, value, updated_at) "
                    "VALUES (?, ?, datetime('now'))",
                    (f"prompt.{pname}", content),
                )

        await conn.commit()

    _apply_and_reload(config_dict, prompt_dict)
    return _build_config_response()


@router.delete("/fields/{name}", response_model=ConfigResponse)
async def delete_field_override(name: str) -> ConfigResponse:
    valid_names = {s["name"] for s in FIELD_SPECS}
    if name not in valid_names:
        raise HTTPException(status_code=404, detail=f"Unknown config field: {name!r}")

    async with get_connection() as conn:
        config_dict, prompt_dict = await _load_all_overrides(conn)

        if name in _WEIGHT_NAMES:
            # Reset all analyst weights together
            config_dict.pop("default_analyst_weights", None)
            await conn.execute(
                "DELETE FROM config_overrides WHERE key = 'config.default_analyst_weights'"
            )
        else:
            config_dict.pop(name, None)
            await conn.execute(
                "DELETE FROM config_overrides WHERE key = ?", (f"config.{name}",)
            )

        await conn.commit()

    _apply_and_reload(config_dict, prompt_dict)
    return _build_config_response()


@router.delete("/prompts/{name}", response_model=ConfigResponse)
async def delete_prompt_override(name: str) -> ConfigResponse:
    if name not in PROMPT_NAMES:
        raise HTTPException(status_code=404, detail=f"Unknown prompt: {name!r}")

    async with get_connection() as conn:
        config_dict, prompt_dict = await _load_all_overrides(conn)
        prompt_dict.pop(name, None)
        await conn.execute(
            "DELETE FROM config_overrides WHERE key = ?", (f"prompt.{name}",)
        )
        await conn.commit()

    _apply_and_reload(config_dict, prompt_dict)
    return _build_config_response()
