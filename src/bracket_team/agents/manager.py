"""Manager agent implementation."""

from __future__ import annotations

from bracket_team.agents.llm import AgentConfig, AgentLLM, LLMResponse
from bracket_team.agents.prompt_loader import get_prompt
from bracket_team.agents.schemas import (
    AnalystReport,
    DiscussionChallenge,
    DiscussionRebuttal,
    ManagerPrediction,
)
from bracket_team.db.models import Matchup


def _discussion_challenge_message(
    matchup: Matchup,
    own_report: AnalystReport,
    own_role: str,
    other_reports: list[tuple[str, AnalystReport]],
    thesis_max_chars: int = 200,
) -> str:
    def _trim(text: str) -> str:
        return text[:thesis_max_chars] + "…" if len(text) > thesis_max_chars else text

    others_text = "\n".join(
        f"- {role}: pick={r.pick}, score={r.score}, relevance={r.relevance}\n  thesis: {_trim(r.thesis)}"
        for role, r in other_reports
    )
    return (
        f"Matchup: {matchup.favorite_name} (#{matchup.favorite_seed}) vs "
        f"{matchup.underdog_name} (#{matchup.underdog_seed})\n"
        f"Round {matchup.round_num}, {matchup.region} region\n\n"
        f"YOUR report ({own_role}):\n"
        f"  pick={own_report.pick}, score={own_report.score}, "
        f"relevance={own_report.relevance}\n"
        f"  thesis: {_trim(own_report.thesis)}\n\n"
        f"OTHER ANALYSTS:\n{others_text}\n\n"
        f"Now produce your DiscussionChallenge."
    )


def _rebuttal_message(
    matchup: Matchup,
    own_role: str,
    own_report: AnalystReport,
    challenge: DiscussionChallenge,
    challenger_role: str,
) -> str:
    return (
        f"Matchup: {matchup.favorite_name} (#{matchup.favorite_seed}) vs "
        f"{matchup.underdog_name} (#{matchup.underdog_seed})\n\n"
        f"Your pick ({own_role}): {own_report.pick} (score={own_report.score})\n"
        f"Your thesis: {own_report.thesis}\n\n"
        f"{challenger_role} challenges you:\n{challenge.challenge}\n\n"
        f"Respond with a DiscussionRebuttal."
    )


def _manager_decision_message(
    matchup: Matchup,
    reports: list[tuple[str, AnalystReport]],
    challenges: list[tuple[str, DiscussionChallenge]],
    rebuttals: list[tuple[str, DiscussionRebuttal]],
    weighted_score: float,
    confidence: str,
    user_preferences: str | None = None,
    discussion_max_chars: int = 700,
) -> str:
    def _trim(text: str) -> str:
        return text[:discussion_max_chars] + "…" if len(text) > discussion_max_chars else text

    thesis_max = discussion_max_chars // 2
    reports_text = "\n".join(
        f"  {role}: pick={r.pick}, score={r.score}, relevance={r.relevance}\n"
        f"    thesis: {r.thesis[:thesis_max] + '…' if len(r.thesis) > thesis_max else r.thesis}"
        for role, r in reports
    )
    challenges_text = "\n".join(
        f"  {role} → {c.target_analyst}: {_trim(c.challenge)}"
        for role, c in challenges
    )
    rebuttals_text = "\n".join(
        f"  {role}: {_trim(rb.rebuttal)}" for role, rb in rebuttals
    )

    prefs_block = ""
    if user_preferences and user_preferences.strip():
        prefs_block = (
            f"USER PREFERENCES (factor these into your decision):\n"
            f"{user_preferences.strip()}\n\n"
        )

    return (
        f"MATCHUP: {matchup.favorite_name} (#{matchup.favorite_seed} seed, FAVORITE) vs "
        f"{matchup.underdog_name} (#{matchup.underdog_seed} seed, UNDERDOG)\n"
        f"Round {matchup.round_num} | {matchup.region} region\n\n"
        f"ANALYST REPORTS:\n{reports_text}\n\n"
        f"DISCUSSION — CHALLENGES:\n{challenges_text}\n\n"
        f"DISCUSSION — REBUTTALS:\n{rebuttals_text}\n\n"
        f"{prefs_block}"
        f"PRE-COMPUTED WEIGHTED SCORE: {weighted_score:.3f} "
        f"(confidence: {confidence})\n\n"
        f"Produce your ManagerPrediction now."
    )


async def run_challenge(
    llm: AgentLLM,
    role: str,
    model: str,
    matchup: Matchup,
    own_report: AnalystReport,
    other_reports: list[tuple[str, AnalystReport]],
    temperature: float = 0.7,
    max_tokens: int = 1024,
    discussion_max_chars: int = 700,
) -> tuple[DiscussionChallenge, LLMResponse]:
    config = AgentConfig(
        role=role,
        model=model,
        system_prompt=get_prompt("discussion_challenge").format(max_chars=discussion_max_chars),
        temperature=temperature,
        max_tokens=max_tokens,
    )
    msg = _discussion_challenge_message(
        matchup, own_report, role, other_reports,
        thesis_max_chars=discussion_max_chars // 3,
    )
    response = await llm.generate(config, msg, DiscussionChallenge)
    challenge = DiscussionChallenge.model_validate_json(response.content)
    return challenge, response


async def run_rebuttal(
    llm: AgentLLM,
    role: str,
    model: str,
    matchup: Matchup,
    own_report: AnalystReport,
    challenge: DiscussionChallenge,
    challenger_role: str,
    temperature: float = 0.7,
    max_tokens: int = 1024,
    discussion_max_chars: int = 700,
) -> tuple[DiscussionRebuttal, LLMResponse]:
    rebuttal_prompt = (
        "You are a specialist analyst responding to a challenge on your pick. "
        "Defend your thesis with specific evidence. Be concise and direct. "
        f"Your rebuttal must be {discussion_max_chars} characters or fewer."
    )
    config = AgentConfig(
        role=role,
        model=model,
        system_prompt=rebuttal_prompt,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    msg = _rebuttal_message(matchup, role, own_report, challenge, challenger_role)
    response = await llm.generate(config, msg, DiscussionRebuttal)
    rebuttal = DiscussionRebuttal.model_validate_json(response.content)
    return rebuttal, response


async def run_manager(
    llm: AgentLLM,
    model: str,
    matchup: Matchup,
    reports: list[tuple[str, AnalystReport]],
    challenges: list[tuple[str, DiscussionChallenge]],
    rebuttals: list[tuple[str, DiscussionRebuttal]],
    weighted_score: float,
    confidence: str,
    temperature: float = 0.5,
    max_tokens: int = 2048,
    user_preferences: str | None = None,
    discussion_max_chars: int = 700,
) -> tuple[ManagerPrediction, LLMResponse]:
    manager_prompt = get_prompt("manager_decision")
    config = AgentConfig(
        role="manager",
        model=model,
        system_prompt=manager_prompt,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    msg = _manager_decision_message(
        matchup, reports, challenges, rebuttals, weighted_score, confidence,
        user_preferences=user_preferences,
        discussion_max_chars=discussion_max_chars,
    )
    response = await llm.generate(config, msg, ManagerPrediction)
    prediction = ManagerPrediction.model_validate_json(response.content)
    return prediction, response
