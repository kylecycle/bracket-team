"""Pure scoring functions — no I/O, fully testable."""

from __future__ import annotations

from typing import Literal

from bracket_team.agents.schemas import AnalystReport


def compute_weighted_score(
    reports: list[tuple[str, AnalystReport]],
    base_weights: dict[str, float],
    relevance_multipliers: dict[str, float],
) -> float:
    """
    Compute a weighted aggregate score across analyst reports.

    Each analyst's contribution = base_weight * relevance_multiplier * score.
    The sum is then normalized by the sum of effective weights.

    Positive result → lean favorite; negative → lean underdog.
    Returns 0.0 if no reports provided.
    """
    if not reports:
        return 0.0

    total_weight = 0.0
    weighted_sum = 0.0

    for role, report in reports:
        bw = base_weights.get(role, 0.0)
        rm = relevance_multipliers.get(report.relevance, 1.0)
        effective_weight = bw * rm
        weighted_sum += effective_weight * report.score
        total_weight += effective_weight

    if total_weight == 0.0:
        return 0.0

    return weighted_sum / total_weight


def derive_confidence(
    weighted_score: float,
    high_threshold: float = 3.0,
    low_threshold: float = 1.5,
) -> Literal["high", "medium", "low"]:
    """
    Map weighted score magnitude to a confidence level.

    |score| >= high_threshold → "high"
    |score| >= low_threshold  → "medium"
    otherwise                 → "low"
    """
    magnitude = abs(weighted_score)
    if magnitude >= high_threshold:
        return "high"
    if magnitude >= low_threshold:
        return "medium"
    return "low"


def select_manager_model(
    weighted_score: float,
    has_contested_challenges: bool,
    contested_threshold: float = 1.0,
    consensus_threshold: float = 3.5,
    model_contested: str = "claude-opus-4-5",
    model_moderate: str = "claude-sonnet-4-5",
    model_consensus: str = "claude-haiku-4-5",
) -> str:
    """
    Choose the manager model based on how much disagreement exists.

    Contested  (|score| <= contested_threshold OR has_contested_challenges) → Opus
    Consensus  (|score| >= consensus_threshold AND no contested challenges) → Haiku
    Otherwise                                                                → Sonnet
    """
    magnitude = abs(weighted_score)

    if magnitude <= contested_threshold or has_contested_challenges:
        return model_contested
    if magnitude >= consensus_threshold:
        return model_consensus
    return model_moderate
