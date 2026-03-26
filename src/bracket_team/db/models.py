"""Pydantic models representing database rows."""

from __future__ import annotations

from pydantic import BaseModel


class Bracket(BaseModel):
    id: int
    year: int
    tournament_name: str
    created_at: str


class Matchup(BaseModel):
    id: int
    bracket_id: int
    run_id: int | None = None
    round_num: int
    region: str
    favorite_name: str
    favorite_seed: int
    underdog_name: str
    underdog_seed: int
    created_at: str


class Run(BaseModel):
    id: int
    bracket_id: int
    name: str
    risk_appetite: str
    analyst_weights: str  # JSON string
    user_preferences: str | None = None
    status: str
    created_at: str
    completed_at: str | None = None
    error_message: str | None = None
    progress_info: str | None = None


class AnalystReport(BaseModel):
    id: int
    run_id: int
    matchup_id: int
    analyst_role: str
    pick: str
    score: int
    relevance: str
    thesis: str
    created_at: str


class DiscussionMessage(BaseModel):
    id: int
    run_id: int
    matchup_id: int
    phase: str
    author_role: str
    target_role: str | None
    steelman: str | None
    content: str
    created_at: str


class Prediction(BaseModel):
    id: int
    run_id: int
    matchup_id: int
    predicted_winner: str
    outcome_type: str
    weighted_score: float
    confidence: str
    synthesis: str
    manager_model: str
    status: str
    created_at: str


class LLMCost(BaseModel):
    id: int
    run_id: int
    matchup_id: int | None
    agent_role: str
    model: str
    phase: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    created_at: str
