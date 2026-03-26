"""AgentLLM protocol and ClaudeBackend implementation."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import anthropic
from pydantic import BaseModel

from bracket_team.config import AppConfig
from bracket_team.exceptions import FatalLLMError, LLMRetryExhaustedError, LLMValidationError

logger = logging.getLogger(__name__)

_CONVO_SEP = "=" * 80


def _make_conversation_logger(path: str) -> logging.Logger:
    """Return a dedicated logger that appends raw LLM conversations to *path*."""
    log = logging.getLogger(f"bracket_team.conversations.{path}")
    if not log.handlers:
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.DEBUG)
        log.propagate = False  # don't echo to the root/uvicorn logger
    return log

# Pricing per million tokens (input, output) — update as needed
_PRICING: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-opus-4-5": (15.00, 75.00),
    "claude-haiku-4-5": (0.80, 4.00),
    "gemini-2.0-flash": (0.075, 0.30),
    "gemini-2.5-flash": (0.075, 0.30),
    "gemini-1.5-pro": (1.25, 5.00),
    # fallback for unknown models
    "default": (3.00, 15.00),
}

_RETRY_DELAYS = [1.0, 5.0, 15.0]
_GEMINI_RETRY_DELAYS = [30.0, 60.0, 90.0]  # free tier: 15 req/min Flash, 2 req/min Pro


@dataclass(frozen=True)
class AgentConfig:
    role: str
    model: str
    system_prompt: str
    temperature: float = 0.7
    max_tokens: int = 2048


@dataclass(frozen=True)
class LLMResponse:
    content: str
    input_tokens: int
    output_tokens: int
    model: str
    cost_usd: float


def _compute_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    key = model if model in _PRICING else "default"
    input_price, output_price = _PRICING[key]
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000


class AgentLLM(Protocol):
    """Swap LLM providers by implementing this interface."""

    async def generate(
        self,
        config: AgentConfig,
        user_message: str,
        response_schema: type[BaseModel] | None = None,
    ) -> LLMResponse: ...


def _parse_teams(user_message: str) -> tuple[str, str]:
    """
    Extract (favorite_name, underdog_name) from the structured user message.

    Matches the format produced by manager._manager_decision_message and
    agents.analysts._matchup_message:
        "MATCHUP: {favorite} (#N seed, FAVORITE) vs {underdog} (#N seed, UNDERDOG)"
    or the analyst variant:
        "Favorite: {name} (#N seed)\nUnderdog:  {name} (#N seed)"

    Returns ("", "") if the message format is unrecognised.
    """
    # Manager message format
    m = re.search(
        r"MATCHUP:\s+(.+?)\s+\(#\d+ seed, FAVORITE\) vs\s+(.+?)\s+\(#\d+ seed, UNDERDOG\)",
        user_message,
    )
    if m:
        return m.group(1).strip(), m.group(2).strip()

    # Analyst message format
    fav = re.search(r"Favorite:\s+(.+?)\s+\(#", user_message)
    dog = re.search(r"Underdog:\s+(.+?)\s+\(#", user_message)
    if fav and dog:
        return fav.group(1).strip(), dog.group(1).strip()

    return "", ""


class StubBackend:
    """
    Zero-cost backend for local testing — no API calls made.

    Returns plausible but canned structured responses so the full pipeline
    can run end-to-end without an API key.
    """

    # Varied stub opinions per analyst role so output looks realistic
    _ANALYST_STUBS: dict[str, dict] = {
        "sports_analyst": {
            "pick": "favorite", "score": 3, "relevance": "high",
            "thesis": (
                "[STUB] The favorite has demonstrated superior defensive efficiency "
                "and stronger recent form heading into the tournament."
            ),
        },
        "odds_analyst": {
            "pick": "favorite", "score": 2, "relevance": "medium",
            "thesis": (
                "[STUB] Market consensus favors the higher seed; line movement "
                "indicates sharp money on the favorite."
            ),
        },
        "historical_analyst": {
            "pick": "underdog", "score": -1, "relevance": "medium",
            "thesis": (
                "[STUB] This seed matchup historically produces upsets roughly 35% "
                "of the time — worth accounting for regression to the mean."
            ),
        },
        "injury_analyst": {
            "pick": "favorite", "score": 1, "relevance": "low",
            "thesis": (
                "[STUB] Both rosters appear healthy; no significant injury news "
                "changes the baseline assessment."
            ),
        },
    }

    async def generate(
        self,
        config: AgentConfig,
        user_message: str,
        response_schema: type[BaseModel] | None = None,
    ) -> LLMResponse:
        from bracket_team.agents.schemas import (
            AnalystReport,
            DiscussionChallenge,
            DiscussionRebuttal,
            ManagerPrediction,
        )

        favorite, underdog = _parse_teams(user_message)

        if response_schema is AnalystReport:
            stub = dict(self._ANALYST_STUBS.get(
                config.role, self._ANALYST_STUBS["sports_analyst"]
            ))
            # historical_analyst picks the underdog — use their actual name in thesis
            if config.role == "historical_analyst" and underdog:
                stub["thesis"] = (
                    f"[STUB] This seed matchup historically produces upsets roughly 35% "
                    f"of the time — {underdog} has a realistic path to an upset."
                )
            result = AnalystReport(**stub)

        elif response_schema is DiscussionChallenge:
            result = DiscussionChallenge(
                steelman_against_own_pick=(
                    "[STUB] The opposing team's guard play could neutralise "
                    "our anticipated advantage."
                ),
                target_analyst="odds_analyst",
                challenge=(
                    "[STUB] The market spread may not reflect late-breaking "
                    "injury news that shifts the expected value."
                ),
            )

        elif response_schema is DiscussionRebuttal:
            result = DiscussionRebuttal(
                rebuttal=(
                    "[STUB] The market is efficient over a large sample; "
                    "this specific injury concern is already priced in."
                )
            )

        elif response_schema is ManagerPrediction:
            winner = favorite or "[unknown]"
            result = ManagerPrediction(
                predicted_winner=winner,
                outcome_type="expected",
                weighted_score=1.8,
                synthesis=(
                    f"[STUB] Three of four analysts favour {winner}, "
                    "with the historical analyst providing the lone dissent. "
                    "The weighted score and market signals align; predicting "
                    "the favourite to advance."
                ),
            )

        else:
            content = f"[STUB] Response from {config.role}"
            return LLMResponse(
                content=content, input_tokens=0, output_tokens=0,
                model="stub", cost_usd=0.0,
            )

        return LLMResponse(
            content=result.model_dump_json(),
            input_tokens=0,
            output_tokens=0,
            model="stub",
            cost_usd=0.0,
        )


class ClaudeBackend:
    """Anthropic AsyncAnthropic implementation of AgentLLM."""

    def __init__(
        self,
        api_key: str,
        max_retries: int = 3,
        max_concurrent: int | None = None,
        conversation_log: str | None = None,
    ):
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._max_retries = max_retries
        self._semaphore = asyncio.Semaphore(max_concurrent) if max_concurrent else None
        self._conv_logger = _make_conversation_logger(conversation_log) if conversation_log else None

    async def generate(
        self,
        config: AgentConfig,
        user_message: str,
        response_schema: type[BaseModel] | None = None,
    ) -> LLMResponse:
        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                if self._semaphore:
                    async with self._semaphore:
                        return await self._call(config, user_message, response_schema, attempt)
                else:
                    return await self._call(config, user_message, response_schema, attempt)
            except anthropic.RateLimitError as e:
                last_error = e
                logger.warning(
                    "Rate limit hit (role=%s, attempt=%d)", config.role, attempt
                )
            except anthropic.APIStatusError as e:
                if e.status_code >= 500:
                    last_error = e
                    logger.warning(
                        "5xx error (role=%s, attempt=%d, status=%d)",
                        config.role, attempt, e.status_code,
                    )
                elif e.status_code in (401, 403) or (
                    e.status_code == 400
                    and any(
                        kw in str(e).lower()
                        for kw in ("credit", "balance", "billing", "payment")
                    )
                ):
                    raise FatalLLMError(str(e), role=config.role) from e
                else:
                    raise  # other 4xx — don't retry
            except LLMValidationError:
                last_error = LLMValidationError(
                    f"Schema validation failed after {attempt + 1} attempts",
                    role=config.role,
                    attempt=attempt,
                )
                logger.warning(
                    "Schema validation failed (role=%s, attempt=%d)", config.role, attempt
                )

            if attempt < self._max_retries:
                base_delay = _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]
                jitter = random.uniform(0, base_delay * 0.5)
                await asyncio.sleep(base_delay + jitter)

        raise LLMRetryExhaustedError(
            f"All {self._max_retries + 1} attempts failed for role={config.role}",
            role=config.role,
            attempt=self._max_retries,
        ) from last_error

    async def _call(
        self,
        config: AgentConfig,
        user_message: str,
        response_schema: type[BaseModel] | None,
        attempt: int,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": config.model,
            "max_tokens": config.max_tokens,
            "system": config.system_prompt,
            "messages": [{"role": "user", "content": user_message}],
        }

        if response_schema is not None:
            # Use tool-use for structured output
            schema = response_schema.model_json_schema()
            tool_name = response_schema.__name__
            kwargs["tools"] = [
                {
                    "name": tool_name,
                    "description": f"Return a structured {tool_name}",
                    "input_schema": schema,
                }
            ]
            kwargs["tool_choice"] = {"type": "tool", "name": tool_name}

        response = await self._client.messages.create(**kwargs)

        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        cost = _compute_cost(config.model, input_tokens, output_tokens)

        if response_schema is not None:
            # Extract tool use block
            tool_block = next(
                (b for b in response.content if b.type == "tool_use"), None
            )
            if tool_block is None:
                raise LLMValidationError(
                    "No tool_use block in response", role=config.role, attempt=attempt
                )
            try:
                parsed = response_schema.model_validate(tool_block.input)
                content = parsed.model_dump_json()
            except Exception as exc:
                raise LLMValidationError(
                    f"Schema validation failed: {exc}", role=config.role, attempt=attempt
                ) from exc
        else:
            text_block = next(
                (b for b in response.content if b.type == "text"), None
            )
            content = text_block.text if text_block else ""

        result = LLMResponse(
            content=content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=config.model,
            cost_usd=cost,
        )

        if self._conv_logger:
            ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
            self._conv_logger.debug(
                "\n%s\n[%s]  role=%s  model=%s  attempt=%d\n\n"
                "--- SYSTEM ---\n%s\n\n"
                "--- USER ---\n%s\n\n"
                "--- RESPONSE ---\n%s\n\n"
                "[tokens: %d in / %d out | cost: $%.4f]\n%s",
                _CONVO_SEP,
                ts, config.role, config.model, attempt,
                config.system_prompt,
                user_message,
                content,
                input_tokens, output_tokens, cost,
                _CONVO_SEP,
            )

        return result


class GeminiBackend:
    """Google Gemini implementation of AgentLLM via google-genai SDK."""

    def __init__(
        self,
        api_key: str,
        max_retries: int = 3,
        max_concurrent: int | None = None,
        conversation_log: str | None = None,
        requests_per_minute: int = 5,
        request_delay: float = 0.0,
        thinking_budget: int = 1024,
    ):
        try:
            from google import genai
        except ImportError as exc:
            raise ImportError(
                "google-genai is required for Gemini support. "
                "Install it with: pip install 'bracket-team[gemini]'"
            ) from exc
        self._client = genai.Client(api_key=api_key)
        self._max_retries = max_retries
        self._request_delay = request_delay
        self._thinking_budget = thinking_budget
        self._semaphore = asyncio.Semaphore(max_concurrent) if max_concurrent else None
        self._conv_logger = _make_conversation_logger(conversation_log) if conversation_log else None
        from bracket_team.scraper.rate_limiter import RateLimiter
        self._rate_limiter = RateLimiter(rate=requests_per_minute / 60)

    async def generate(
        self,
        config: AgentConfig,
        user_message: str,
        response_schema: type[BaseModel] | None = None,
    ) -> LLMResponse:
        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                if self._semaphore:
                    async with self._semaphore:
                        return await self._call(config, user_message, response_schema, attempt)
                else:
                    return await self._call(config, user_message, response_schema, attempt)
            except Exception as exc:
                exc_type = type(exc).__name__
                status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)

                # Rate limit (429)
                if status_code == 429 or "ResourceExhausted" in exc_type or "rate" in str(exc).lower():
                    last_error = exc
                    logger.warning(
                        "Gemini rate limit hit (role=%s, attempt=%d): [%s] %s",
                        config.role, attempt, exc_type, exc,
                    )
                # Server errors (5xx)
                elif status_code and 500 <= int(status_code) < 600 or "ServerError" in exc_type:
                    last_error = exc
                    logger.warning(
                        "Gemini 5xx error (role=%s, attempt=%d, status=%s)",
                        config.role, attempt, status_code,
                    )
                # Auth errors — fatal
                elif (
                    status_code in (401, 403)
                    or "PermissionDenied" in exc_type
                    or "Unauthenticated" in exc_type
                    or (status_code == 400 and "API_KEY_INVALID" in str(exc))
                ):
                    raise FatalLLMError(str(exc), role=config.role) from exc
                elif isinstance(exc, LLMValidationError):
                    last_error = exc
                    logger.warning(
                        "Schema validation failed (role=%s, attempt=%d)", config.role, attempt
                    )
                # Network/DNS errors — transient, retryable
                elif isinstance(exc, OSError) or any(
                    name in exc_type for name in ("Connect", "Connection", "Network", "Transport", "Timeout")
                ):
                    last_error = exc
                    logger.warning(
                        "Gemini network error (role=%s, attempt=%d): %s", config.role, attempt, exc
                    )
                else:
                    raise  # non-retryable

            if attempt < self._max_retries:
                base_delay = _GEMINI_RETRY_DELAYS[min(attempt, len(_GEMINI_RETRY_DELAYS) - 1)]
                jitter = random.uniform(0, base_delay * 0.5)
                await asyncio.sleep(base_delay + jitter)

        raise LLMRetryExhaustedError(
            f"All {self._max_retries + 1} attempts failed for role={config.role}",
            role=config.role,
            attempt=self._max_retries,
        ) from last_error

    async def _call(
        self,
        config: AgentConfig,
        user_message: str,
        response_schema: type[BaseModel] | None,
        attempt: int,
    ) -> LLMResponse:
        from google.genai import types

        # Gemma models don't support system_instruction or JSON mode
        is_gemma = config.model.lower().startswith("gemma")
        if is_gemma and response_schema is not None:
            schema_str = json.dumps(response_schema.model_json_schema(), indent=2)
            contents = (
                f"{config.system_prompt}\n\n"
                f"You MUST respond with valid JSON matching this schema exactly:\n{schema_str}\n\n"
                f"{user_message}\n\nRespond with JSON only, no other text."
            )
        elif is_gemma:
            contents = f"{config.system_prompt}\n\n{user_message}"
        else:
            contents = user_message

        gen_config_kwargs: dict[str, Any] = {
            "temperature": config.temperature,
            "max_output_tokens": config.max_tokens,
        }
        if not is_gemma:
            gen_config_kwargs["system_instruction"] = config.system_prompt
        if response_schema is not None and not is_gemma:
            gen_config_kwargs["response_mime_type"] = "application/json"
            gen_config_kwargs["response_schema"] = response_schema
        # For gemini-2.5 thinking models, bump max_output_tokens so thinking
        # tokens don't eat into the response budget, then cap thinking explicitly.
        if not is_gemma and "2.5" in config.model:
            budget = self._thinking_budget
            gen_config_kwargs["max_output_tokens"] = config.max_tokens + budget
            gen_config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=budget)

        await self._rate_limiter.acquire()
        if self._request_delay > 0:
            await asyncio.sleep(self._request_delay)
        response = await self._client.aio.models.generate_content(
            model=config.model,
            contents=contents,
            config=types.GenerateContentConfig(**gen_config_kwargs),
        )

        usage = response.usage_metadata
        input_tokens = usage.prompt_token_count or 0
        output_tokens = usage.candidates_token_count or 0
        cost = _compute_cost(config.model, input_tokens, output_tokens)

        if response_schema is not None:
            raw_text = response.text or ""
            # Gemma may wrap JSON in markdown code fences or use +N for positive numbers
            if is_gemma:
                raw_text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text.strip())
                raw_text = re.sub(r":\s*\+(\d)", r": \1", raw_text)  # +5 → 5
            try:
                parsed = response_schema.model_validate_json(raw_text)
                content = parsed.model_dump_json()
            except Exception as exc:
                logger.warning(
                    "Schema validation failed (role=%s, attempt=%d): %s\nRaw response: %s",
                    config.role, attempt, exc, raw_text[:500],
                )
                raise LLMValidationError(
                    f"Schema validation failed: {exc}", role=config.role, attempt=attempt
                ) from exc
        else:
            content = response.text or ""

        result = LLMResponse(
            content=content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=config.model,
            cost_usd=cost,
        )

        if self._conv_logger:
            ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
            self._conv_logger.debug(
                "\n%s\n[%s]  role=%s  model=%s  attempt=%d\n\n"
                "--- SYSTEM ---\n%s\n\n"
                "--- USER ---\n%s\n\n"
                "--- RESPONSE ---\n%s\n\n"
                "[tokens: %d in / %d out | cost: $%.4f]\n%s",
                _CONVO_SEP,
                ts, config.role, config.model, attempt,
                config.system_prompt,
                user_message,
                content,
                input_tokens, output_tokens, cost,
                _CONVO_SEP,
            )

        return result


def create_backend(cfg: AppConfig, override_provider: str | None = None) -> AgentLLM:
    """Factory: select and instantiate the right LLM backend from config.

    Pass *override_provider* to force a specific backend regardless of
    ``cfg.llm_provider`` (e.g. the CLI ``--stub`` flag passes ``"stub"``).

    Provider resolution order for ``llm_provider="auto"`` (the default):
      1. Gemini  — if BT_GEMINI_API_KEY is set
      2. Anthropic — if BT_ANTHROPIC_API_KEY is set (and not "dummy-key")
      3. Stub    — no real key available
    """
    provider = (override_provider or cfg.llm_provider).lower()

    if provider == "stub":
        return StubBackend()

    if provider == "auto":
        gemini_key = cfg.gemini_api_key.get_secret_value() if cfg.gemini_api_key else ""
        anthropic_key = cfg.anthropic_api_key.get_secret_value()
        if gemini_key:
            provider = "gemini"
        elif anthropic_key not in ("dummy-key", "stub"):
            provider = "anthropic"
        else:
            logger.warning("No API key found; falling back to stub backend")
            return StubBackend()

    if provider == "gemini":
        api_key = cfg.gemini_api_key.get_secret_value() if cfg.gemini_api_key else ""
        return GeminiBackend(
            api_key=api_key,
            max_retries=cfg.llm_max_retries,
            max_concurrent=cfg.max_concurrent_api_calls,
            conversation_log=cfg.llm_conversation_log,
            requests_per_minute=cfg.gemini_rpm,
            request_delay=cfg.gemini_request_delay,
            thinking_budget=cfg.gemini_thinking_budget,
        )

    # anthropic
    api_key = cfg.anthropic_api_key.get_secret_value()
    if api_key in ("dummy-key", "stub"):
        return StubBackend()
    return ClaudeBackend(
        api_key=api_key,
        max_retries=cfg.llm_max_retries,
        max_concurrent=cfg.max_concurrent_api_calls,
        conversation_log=cfg.llm_conversation_log,
    )
