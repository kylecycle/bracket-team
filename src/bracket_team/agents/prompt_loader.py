"""Centralized prompt loading with runtime override support."""

from __future__ import annotations

from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent / "prompts"

PROMPT_NAMES = [
    "sports_analyst",
    "odds_analyst",
    "historical_analyst",
    "injury_analyst",
    "discussion_challenge",
    "manager_decision",
]

_overrides: dict[str, str] = {}


def set_prompt_overrides(overrides: dict[str, str]) -> None:
    """Replace the in-memory prompt overrides (called at startup and on save)."""
    global _overrides
    _overrides = overrides


def get_prompt(name: str) -> str:
    """Return the prompt text: DB override if present, else the file on disk."""
    if name in _overrides:
        return _overrides[name]
    return get_prompt_default(name)


def get_prompt_default(name: str) -> str:
    """Always read from disk, ignoring any overrides."""
    return (_PROMPTS_DIR / f"{name}.txt").read_text()
