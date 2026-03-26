"""Application configuration via Pydantic Settings."""

from __future__ import annotations

from typing import Any

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BT_", env_file=".env")

    anthropic_api_key: SecretStr = SecretStr("dummy-key")
    api_key: SecretStr | None = None  # BT_API_KEY — if set, required on all API requests
    odds_api_key: SecretStr | None = None  # BT_ODDS_API_KEY — The Odds API key (free tier)

    # LLM provider: "auto" | "anthropic" | "gemini" | "stub"
    # "auto" (default): use Gemini if BT_GEMINI_API_KEY is set, else Anthropic
    llm_provider: str = "auto"
    gemini_api_key: SecretStr | None = None  # BT_GEMINI_API_KEY

    # Model selection — Anthropic
    analyst_model: str = "claude-sonnet-4-5"
    manager_model_contested: str = "claude-opus-4-5"
    manager_model_moderate: str = "claude-sonnet-4-5"
    manager_model_consensus: str = "claude-haiku-4-5"

    # Model selection — Gemini defaults
    gemini_analyst_model: str = "gemini-2.5-flash"
    gemini_manager_model_contested: str = "gemini-2.5-flash"
    gemini_manager_model_moderate: str = "gemini-2.5-flash"
    gemini_manager_model_consensus: str = "gemini-2.5-flash"

    # Gemini free-tier rate limit (requests per minute); used for proactive throttling
    gemini_rpm: int = 25
    # Thinking token budget for gemini-2.5 models (0 = disabled).
    # Thinking tokens count against max_output_tokens, so the effective output
    # budget is llm_max_tokens + gemini_thinking_budget.
    gemini_thinking_budget: int = 1024

    @property
    def effective_provider(self) -> str:
        """Resolve 'auto' to the actual provider based on available API keys."""
        provider = self.llm_provider.lower()
        if provider != "auto":
            return provider
        if self.gemini_api_key and self.gemini_api_key.get_secret_value():
            return "gemini"
        if self.anthropic_api_key.get_secret_value() not in ("dummy-key", "stub"):
            return "anthropic"
        return "stub"

    @property
    def effective_analyst_model(self) -> str:
        if self.effective_provider == "gemini":
            return self.gemini_analyst_model
        return self.analyst_model

    @property
    def effective_manager_model_contested(self) -> str:
        if self.effective_provider == "gemini":
            return self.gemini_manager_model_contested
        return self.manager_model_contested

    @property
    def effective_manager_model_moderate(self) -> str:
        if self.effective_provider == "gemini":
            return self.gemini_manager_model_moderate
        return self.manager_model_moderate

    @property
    def effective_manager_model_consensus(self) -> str:
        if self.effective_provider == "gemini":
            return self.gemini_manager_model_consensus
        return self.manager_model_consensus

    # LLM behavior
    llm_temperature: float = 0.7
    llm_max_tokens: int = 2048
    llm_max_retries: int = 3
    gemini_request_delay: float = 0.0  # seconds to wait between Gemini API calls (BT_GEMINI_REQUEST_DELAY)
    discussion_max_chars: int = 500  # max characters for challenge/rebuttal text fed to manager (BT_DISCUSSION_MAX_CHARS)

    # Database
    database_url: str = "bracket_team.db"

    # Logging — set to a file path to capture full LLM conversations
    llm_conversation_log: str | None = None  # BT_LLM_CONVERSATION_LOG

    # Concurrency
    max_concurrent_matchups: int = 1
    max_concurrent_api_calls: int = 4  # global cap on in-flight Anthropic API requests

    # Analyst weights (must sum to 1.0)
    default_analyst_weights: dict[str, float] = {
        "sports_analyst": 0.30,
        "odds_analyst": 0.25,
        "historical_analyst": 0.25,
        "injury_analyst": 0.20,
    }

    # Relevance multipliers applied to each analyst's weight
    relevance_multipliers: dict[str, float] = {
        "low": 0.25,
        "medium": 0.5,
        "high": 1.0,
    }

    # Scoring thresholds for model selection and confidence
    score_contested_threshold: float = 1.5   # |score| <= this → contested → Opus
    score_consensus_threshold: float = 3.0   # |score| >= this → consensus → Haiku
    confidence_high_threshold: float = 3.0
    confidence_low_threshold: float = 1.5


_overrides: dict[str, Any] = {}
_instance: AppConfig | None = None


def set_config_overrides(overrides: dict[str, Any]) -> None:
    """Replace in-memory config overrides and invalidate the cached instance."""
    global _overrides, _instance
    _overrides = overrides
    _instance = None


def get_config() -> AppConfig:
    global _instance
    if _instance is None:
        _instance = AppConfig(**_overrides)
    return _instance
