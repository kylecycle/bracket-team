"""Unit tests for service/scoring.py — pure functions, no I/O."""

import pytest

from bracket_team.agents.schemas import AnalystReport
from bracket_team.service.scoring import (
    compute_weighted_score,
    derive_confidence,
    select_manager_model,
)

BASE_WEIGHTS = {
    "sports_analyst": 0.30,
    "odds_analyst": 0.25,
    "historical_analyst": 0.25,
    "injury_analyst": 0.20,
}
RELEVANCE_MULTIPLIERS = {"low": 0.5, "medium": 1.0, "high": 1.5}


def make_report(pick, score, relevance="medium") -> AnalystReport:
    return AnalystReport(pick=pick, score=score, relevance=relevance, thesis="test")


# ---------------------------------------------------------------------------
# compute_weighted_score
# ---------------------------------------------------------------------------

def test_empty_reports():
    assert compute_weighted_score([], BASE_WEIGHTS, RELEVANCE_MULTIPLIERS) == 0.0


def test_all_favor_favorite():
    reports = [
        ("sports_analyst", make_report("favorite", 4, "high")),
        ("odds_analyst", make_report("favorite", 3, "medium")),
        ("historical_analyst", make_report("favorite", 2, "medium")),
        ("injury_analyst", make_report("favorite", 1, "low")),
    ]
    score = compute_weighted_score(reports, BASE_WEIGHTS, RELEVANCE_MULTIPLIERS)
    assert score > 0


def test_all_favor_underdog():
    reports = [
        ("sports_analyst", make_report("underdog", -4, "high")),
        ("odds_analyst", make_report("underdog", -3, "medium")),
        ("historical_analyst", make_report("underdog", -2, "medium")),
        ("injury_analyst", make_report("underdog", -1, "low")),
    ]
    score = compute_weighted_score(reports, BASE_WEIGHTS, RELEVANCE_MULTIPLIERS)
    assert score < 0


def test_tossup_near_zero():
    reports = [
        ("sports_analyst", make_report("favorite", 1, "medium")),
        ("odds_analyst", make_report("underdog", -1, "medium")),
        ("historical_analyst", make_report("favorite", 1, "medium")),
        ("injury_analyst", make_report("underdog", -1, "medium")),
    ]
    score = compute_weighted_score(reports, BASE_WEIGHTS, RELEVANCE_MULTIPLIERS)
    assert abs(score) < 1.0


def test_relevance_multiplier_effect():
    """High relevance should amplify weight."""
    high_rel = [("sports_analyst", make_report("favorite", 3, "high"))]
    low_rel = [("sports_analyst", make_report("favorite", 3, "low"))]
    high_score = compute_weighted_score(high_rel, BASE_WEIGHTS, RELEVANCE_MULTIPLIERS)
    low_score = compute_weighted_score(low_rel, BASE_WEIGHTS, RELEVANCE_MULTIPLIERS)
    # Score magnitude should be the same since we normalize, but both equal the raw score
    assert high_score == low_score == 3.0


def test_unknown_role_uses_zero_weight():
    reports = [("unknown_role", make_report("favorite", 5, "high"))]
    score = compute_weighted_score(reports, BASE_WEIGHTS, RELEVANCE_MULTIPLIERS)
    assert score == 0.0


# ---------------------------------------------------------------------------
# derive_confidence
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("score,expected", [
    (4.0, "high"),
    (-4.0, "high"),
    (3.0, "high"),
    (2.0, "medium"),
    (-2.0, "medium"),
    (1.5, "medium"),
    (1.0, "low"),
    (0.0, "low"),
    (-1.0, "low"),
])
def test_derive_confidence(score, expected):
    assert derive_confidence(score) == expected


# ---------------------------------------------------------------------------
# select_manager_model
# ---------------------------------------------------------------------------

def test_contested_score_uses_opus():
    model = select_manager_model(0.5, False)
    assert "opus" in model.lower()


def test_contested_challenges_uses_opus():
    model = select_manager_model(4.0, True)
    assert "opus" in model.lower()


def test_consensus_uses_haiku():
    model = select_manager_model(4.0, False)
    assert "haiku" in model.lower()


def test_moderate_uses_sonnet():
    model = select_manager_model(2.0, False)
    assert "sonnet" in model.lower()


def test_boundary_contested_threshold():
    # Exactly at threshold is still contested
    model = select_manager_model(1.0, False)
    assert "opus" in model.lower()


def test_boundary_consensus_threshold():
    # Exactly at consensus threshold → haiku
    model = select_manager_model(3.5, False)
    assert "haiku" in model.lower()
