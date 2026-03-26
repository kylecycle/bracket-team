"""Pydantic models for structured LLM output."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, field_validator


class AnalystReport(BaseModel):
    pick: Literal["favorite", "underdog"]
    score: int  # -5 to +5; negative = lean underdog, positive = lean favorite
    relevance: Literal["low", "medium", "high"]
    thesis: str

    @field_validator("score")
    @classmethod
    def score_in_range(cls, v: int) -> int:
        if not -5 <= v <= 5:
            raise ValueError(f"score must be between -5 and 5, got {v}")
        return v


class DiscussionChallenge(BaseModel):
    steelman_against_own_pick: str
    target_analyst: str  # role name of the analyst being challenged
    challenge: str


class DiscussionRebuttal(BaseModel):
    rebuttal: str


class ManagerPrediction(BaseModel):
    predicted_winner: str
    outcome_type: Literal["expected", "upset"]
    weighted_score: float
    synthesis: str
