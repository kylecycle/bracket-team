"""Unit tests for agents/schemas.py — Pydantic validation."""

import pytest
from pydantic import ValidationError

from bracket_team.agents.schemas import (
    AnalystReport,
    DiscussionChallenge,
    ManagerPrediction,
)

# ---------------------------------------------------------------------------
# AnalystReport
# ---------------------------------------------------------------------------

def test_valid_analyst_report():
    r = AnalystReport(pick="favorite", score=3, relevance="high", thesis="Strong team.")
    assert r.pick == "favorite"
    assert r.score == 3


def test_score_too_high():
    with pytest.raises(ValidationError):
        AnalystReport(pick="favorite", score=6, relevance="medium", thesis="x")


def test_score_too_low():
    with pytest.raises(ValidationError):
        AnalystReport(pick="underdog", score=-6, relevance="medium", thesis="x")


def test_score_boundary_valid():
    r1 = AnalystReport(pick="favorite", score=5, relevance="low", thesis="x")
    r2 = AnalystReport(pick="underdog", score=-5, relevance="low", thesis="x")
    assert r1.score == 5
    assert r2.score == -5


def test_invalid_pick():
    with pytest.raises(ValidationError):
        AnalystReport(pick="both", score=1, relevance="medium", thesis="x")


def test_invalid_relevance():
    with pytest.raises(ValidationError):
        AnalystReport(pick="favorite", score=1, relevance="very_high", thesis="x")


# ---------------------------------------------------------------------------
# DiscussionChallenge
# ---------------------------------------------------------------------------

def test_valid_challenge():
    c = DiscussionChallenge(
        steelman_against_own_pick="Could be wrong.",
        target_analyst="odds_analyst",
        challenge="Markets are stale.",
    )
    assert c.target_analyst == "odds_analyst"


# ---------------------------------------------------------------------------
# ManagerPrediction
# ---------------------------------------------------------------------------

def test_valid_manager_prediction():
    p = ManagerPrediction(
        predicted_winner="Duke",
        outcome_type="expected",
        weighted_score=2.5,
        synthesis="Duke wins convincingly.",
    )
    assert p.outcome_type == "expected"


def test_invalid_outcome_type():
    with pytest.raises(ValidationError):
        ManagerPrediction(
            predicted_winner="Duke",
            outcome_type="blowout",  # invalid
            weighted_score=2.5,
            synthesis="x",
        )
