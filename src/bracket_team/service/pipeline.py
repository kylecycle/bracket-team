"""MatchupPipeline — orchestrates research → discussion → decision for one matchup."""

from __future__ import annotations

import asyncio
import logging

from bracket_team.agents.analysts import ANALYST_ROLES, run_analyst
from bracket_team.agents.llm import AgentLLM
from bracket_team.agents.manager import run_challenge, run_manager, run_rebuttal
from bracket_team.agents.schemas import AnalystReport, DiscussionChallenge, DiscussionRebuttal
from bracket_team.config import AppConfig
from bracket_team.db.connection import get_connection
from bracket_team.db.models import Matchup, Prediction
from bracket_team.db.repositories.cost_repo import CostRepository
from bracket_team.db.repositories.discussion_repo import DiscussionRepository
from bracket_team.db.repositories.matchup_repo import MatchupRepository
from bracket_team.db.repositories.prediction_repo import PredictionRepository
from bracket_team.db.repositories.report_repo import ReportRepository
from bracket_team.db.repositories.run_repo import RunRepository
from bracket_team.exceptions import FatalLLMError, PipelineError
from bracket_team.service.scoring import (
    compute_weighted_score,
    derive_confidence,
    select_manager_model,
)

logger = logging.getLogger(__name__)

_MAX_ROUNDS = 6


async def analyze_bracket(
    run_id: int,
    llm: AgentLLM,
    config: AppConfig,
    progress_callback=None,
) -> list[Prediction]:
    """
    Analyze a full bracket round-by-round.

    Round 1 matchups must already exist in the DB (imported via bracket_service).
    After each round, winners are paired up to create the next round's matchups.
    Matchups within each round run concurrently, capped by config.max_concurrent_matchups.

    progress_callback(round_num, total_rounds, completed, total) is called after
    each matchup completes if provided.
    """
    async with get_connection() as conn:
        run = await RunRepository(conn).get(run_id)
        if run is None:
            raise PipelineError(f"Run {run_id} not found", run_id=run_id)
        all_existing_matchups = await MatchupRepository(conn).list_by_bracket(run.bracket_id, run_id=run_id)
        existing_preds = await PredictionRepository(conn).list_by_run(run_id)

    matchups_by_round: dict[int, list[Matchup]] = {}
    for m in all_existing_matchups:
        matchups_by_round.setdefault(m.round_num, []).append(m)

    round1_matchups = matchups_by_round.get(1, [])
    if not round1_matchups:
        raise PipelineError(
            f"No round-1 matchups found for bracket {run.bracket_id}", run_id=run_id
        )

    pred_map: dict[int, Prediction] = {p.matchup_id: p for p in existing_preds}
    pipeline = MatchupPipeline(llm, config, user_preferences=run.user_preferences)
    all_predictions: list[Prediction] = []
    current_matchups = round1_matchups

    for round_num in range(1, _MAX_ROUNDS + 1):
        if not current_matchups:
            break

        # Stop if run was paused via the API
        async with get_connection() as conn:
            current_run = await RunRepository(conn).get(run_id)
        if current_run and current_run.status == "paused":
            logger.info("Bracket run_id=%d: paused before round %d — stopping", run_id, round_num)
            return all_predictions

        pending = [m for m in current_matchups if m.id not in pred_map]
        skipped = len(current_matchups) - len(pending)

        logger.info(
            "Bracket run_id=%d: round %d — %d matchup(s), %d already complete",
            run_id, round_num, len(current_matchups), skipped,
        )

        if pending:
            sem = asyncio.Semaphore(config.max_concurrent_matchups)
            completed_count = 0
            paused = False

            async def _throttled(m: Matchup) -> Prediction | None:
                nonlocal completed_count, paused
                async with sem:
                    # Check for pause before starting each matchup
                    async with get_connection() as conn:
                        current_run = await RunRepository(conn).get(run_id)
                    if current_run and current_run.status == "paused":
                        paused = True
                        return None
                    pred = await pipeline.run(run_id, m)
                    completed_count += 1
                    if progress_callback:
                        progress_callback(
                            round_num, _MAX_ROUNDS,
                            skipped + completed_count, len(current_matchups),
                        )
                    return pred

            try:
                async with asyncio.TaskGroup() as tg:
                    tasks = [tg.create_task(_throttled(m)) for m in pending]
            except* FatalLLMError as eg:
                raise eg.exceptions[0]  # unwrap ExceptionGroup → plain FatalLLMError

            new_preds = [t.result() for t in tasks if t.result() is not None]
            for p in new_preds:
                pred_map[p.matchup_id] = p
            all_predictions.extend(new_preds)

            if paused:
                logger.info("Bracket run_id=%d: paused mid-round %d — stopping", run_id, round_num)
                return all_predictions

        round_predictions = [pred_map[m.id] for m in current_matchups if m.id in pred_map]

        if round_num < _MAX_ROUNDS:
            next_matchups = matchups_by_round.get(round_num + 1, [])
            if not next_matchups:
                next_matchups = await _create_next_round_matchups(
                    run.bracket_id, round_num + 1, current_matchups, round_predictions, run_id
                )
                matchups_by_round[round_num + 1] = next_matchups
            current_matchups = next_matchups

    return all_predictions


async def _create_next_round_matchups(
    bracket_id: int,
    next_round_num: int,
    prev_matchups: list[Matchup],
    predictions: list[Prediction],
    run_id: int,
) -> list[Matchup]:
    """
    Pair up winners from the previous round to produce the next round's matchups.

    Matchups are paired consecutively by insertion order (0 vs 1, 2 vs 3, …).
    The lower seed is always the favorite.
    """
    # Build prediction lookup: matchup_id → predicted winner info
    pred_by_matchup: dict[int, Prediction] = {p.matchup_id: p for p in predictions}

    # Collect (winner_name, winner_seed) for each previous matchup in order
    winners: list[tuple[str, int, str]] = []  # (name, seed, region)
    for m in prev_matchups:
        pred = pred_by_matchup.get(m.id)
        if pred is None:
            logger.warning("No prediction for matchup %d — skipping bracket propagation", m.id)
            continue
        if pred.predicted_winner == m.favorite_name:
            winners.append((m.favorite_name, m.favorite_seed, m.region))
        elif pred.predicted_winner == m.underdog_name:
            winners.append((m.underdog_name, m.underdog_seed, m.region))
        else:
            # predicted_winner doesn't match either team name (e.g. stub mode).
            # Default to the favorite so bracket propagation can continue.
            logger.warning(
                "predicted_winner %r doesn't match either team in matchup %d "
                "('%s' vs '%s') — defaulting to favorite",
                pred.predicted_winner, m.id, m.favorite_name, m.underdog_name,
            )
            winners.append((m.favorite_name, m.favorite_seed, m.region))

    # Pair consecutive winners: (0,1), (2,3), ...
    if len(winners) % 2 != 0 and len(winners) > 1:
        logger.warning(
            "Odd number of winners (%d) for round %d — last team gets a bye",
            len(winners), next_round_num,
        )

    new_matchups: list[Matchup] = []
    async with get_connection() as conn:
        repo = MatchupRepository(conn)
        for i in range(0, len(winners) - 1, 2):
            name_a, seed_a, region_a = winners[i]
            name_b, seed_b, region_b = winners[i + 1]
            # Lower seed = favorite
            if seed_a <= seed_b:
                fav_name, fav_seed, dog_name, dog_seed = name_a, seed_a, name_b, seed_b
            else:
                fav_name, fav_seed, dog_name, dog_seed = name_b, seed_b, name_a, seed_a
            # Use the first team's region; Final Four/Championship gets "National"
            region = region_a if next_round_num <= 4 else "National"
            matchup = await repo.create(
                bracket_id=bracket_id,
                round_num=next_round_num,
                region=region,
                favorite_name=fav_name,
                favorite_seed=fav_seed,
                underdog_name=dog_name,
                underdog_seed=dog_seed,
                run_id=run_id,
            )
            new_matchups.append(matchup)

    return new_matchups


class MatchupPipeline:
    """Orchestrates the three phases for a single matchup."""

    def __init__(self, llm: AgentLLM, config: AppConfig, user_preferences: str | None = None):
        self._llm = llm
        self._config = config
        self._user_preferences = user_preferences

    async def run(self, run_id: int, matchup: Matchup) -> Prediction:
        """Execute research → discussion → decision. Persists all outputs."""
        logger.info(
            "Pipeline start run_id=%d matchup_id=%d (%s vs %s)",
            run_id, matchup.id, matchup.favorite_name, matchup.underdog_name,
        )
        teams = f"{matchup.favorite_name} vs {matchup.underdog_name}"
        try:
            await self._set_progress(run_id, teams, "research")
            reports = await self._research_phase(run_id, matchup)
            await self._set_progress(run_id, teams, "discussion")
            challenges, rebuttals = await self._discussion_phase(run_id, matchup, reports)
            await self._set_progress(run_id, teams, "decision")
            prediction = await self._decision_phase(
                run_id, matchup, reports, challenges, rebuttals
            )
            return prediction
        except FatalLLMError:
            raise  # propagate — don't wrap in PipelineError
        except Exception as exc:
            logger.error(
                "Pipeline error run_id=%d matchup_id=%d: %s",
                run_id, matchup.id, exc, exc_info=True,
            )
            raise PipelineError(str(exc), run_id=run_id, matchup_id=matchup.id) from exc

    async def _set_progress(self, run_id: int, teams: str, phase: str) -> None:
        try:
            async with get_connection() as conn:
                await RunRepository(conn).update_progress(run_id, teams, phase)
        except Exception:
            pass  # progress is best-effort

    # ------------------------------------------------------------------
    # Phase 1: Research — 4 parallel analyst calls
    # ------------------------------------------------------------------

    async def _research_phase(
        self, run_id: int, matchup: Matchup
    ) -> list[tuple[str, AnalystReport]]:
        fatal_error: FatalLLMError | None = None

        async def _one_analyst(role: str) -> tuple[str, AnalystReport] | None:
            nonlocal fatal_error
            try:
                report, llm_resp = await run_analyst(
                    self._llm,
                    role,
                    self._config.effective_analyst_model,
                    matchup,
                    temperature=self._config.llm_temperature,
                    max_tokens=self._config.llm_max_tokens,
                )
                async with get_connection() as conn:
                    await ReportRepository(conn).create(
                        run_id=run_id,
                        matchup_id=matchup.id,
                        analyst_role=role,
                        pick=report.pick,
                        score=report.score,
                        relevance=report.relevance,
                        thesis=report.thesis,
                    )
                    await CostRepository(conn).create(
                        run_id=run_id,
                        matchup_id=matchup.id,
                        agent_role=role,
                        model=llm_resp.model,
                        phase="research",
                        input_tokens=llm_resp.input_tokens,
                        output_tokens=llm_resp.output_tokens,
                        cost_usd=llm_resp.cost_usd,
                    )
                logger.debug("Analyst %s: pick=%s score=%d", role, report.pick, report.score)
                return role, report
            except FatalLLMError as exc:
                fatal_error = exc
                return None
            except Exception as exc:
                logger.warning("Analyst %s failed: %s — continuing without them", role, exc)
                return None

        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(_one_analyst(role)) for role in ANALYST_ROLES]

        if fatal_error is not None:
            raise fatal_error

        results = [t.result() for t in tasks if t.result() is not None]
        if not results:
            raise PipelineError(
                "All analysts failed — cannot continue",
                run_id=run_id,
                matchup_id=matchup.id,
            )
        return results

    # ------------------------------------------------------------------
    # Phase 2: Discussion — challenges then rebuttals, each batch parallel
    # ------------------------------------------------------------------

    async def _discussion_phase(
        self,
        run_id: int,
        matchup: Matchup,
        reports: list[tuple[str, AnalystReport]],
    ) -> tuple[
        list[tuple[str, DiscussionChallenge]],
        list[tuple[str, DiscussionRebuttal]],
    ]:
        # --- challenges (all analysts challenge simultaneously) ---
        async def _one_challenge(
            role: str, own_report: AnalystReport
        ) -> tuple[str, DiscussionChallenge] | None:
            other_reports = [(r, rep) for r, rep in reports if r != role]
            try:
                challenge, llm_resp = await run_challenge(
                    self._llm,
                    role,
                    self._config.effective_analyst_model,
                    matchup,
                    own_report,
                    other_reports,
                    temperature=self._config.llm_temperature,
                    discussion_max_chars=self._config.discussion_max_chars,
                )
                async with get_connection() as conn:
                    await DiscussionRepository(conn).create(
                        run_id=run_id,
                        matchup_id=matchup.id,
                        phase="challenge",
                        author_role=role,
                        target_role=challenge.target_analyst,
                        steelman=challenge.steelman_against_own_pick,
                        content=challenge.challenge,
                    )
                    await CostRepository(conn).create(
                        run_id=run_id,
                        matchup_id=matchup.id,
                        agent_role=role,
                        model=llm_resp.model,
                        phase="challenge",
                        input_tokens=llm_resp.input_tokens,
                        output_tokens=llm_resp.output_tokens,
                        cost_usd=llm_resp.cost_usd,
                    )
                return role, challenge
            except Exception as exc:
                logger.warning("Challenge for %s failed: %s", role, exc)
                return None

        async with asyncio.TaskGroup() as tg:
            ch_tasks = [
                tg.create_task(_one_challenge(role, report))
                for role, report in reports
            ]

        challenges: list[tuple[str, DiscussionChallenge]] = [
            t.result() for t in ch_tasks if t.result() is not None
        ]

        # Build a lookup: target_role → (challenger_role, challenge)
        challenge_by_target: dict[str, tuple[str, DiscussionChallenge]] = {}
        for challenger_role, ch in challenges:
            target = ch.target_analyst
            if target not in challenge_by_target:
                challenge_by_target[target] = (challenger_role, ch)

        # --- rebuttals (all targeted analysts respond simultaneously) ---
        async def _one_rebuttal(
            role: str, own_report: AnalystReport
        ) -> tuple[str, DiscussionRebuttal] | None:
            if role not in challenge_by_target:
                return None
            challenger_role, ch = challenge_by_target[role]
            try:
                rebuttal, llm_resp = await run_rebuttal(
                    self._llm,
                    role,
                    self._config.effective_analyst_model,
                    matchup,
                    own_report,
                    ch,
                    challenger_role,
                    temperature=self._config.llm_temperature,
                    discussion_max_chars=self._config.discussion_max_chars,
                )
                async with get_connection() as conn:
                    await DiscussionRepository(conn).create(
                        run_id=run_id,
                        matchup_id=matchup.id,
                        phase="rebuttal",
                        author_role=role,
                        content=rebuttal.rebuttal,
                    )
                    await CostRepository(conn).create(
                        run_id=run_id,
                        matchup_id=matchup.id,
                        agent_role=role,
                        model=llm_resp.model,
                        phase="rebuttal",
                        input_tokens=llm_resp.input_tokens,
                        output_tokens=llm_resp.output_tokens,
                        cost_usd=llm_resp.cost_usd,
                    )
                return role, rebuttal
            except Exception as exc:
                logger.warning("Rebuttal for %s failed: %s", role, exc)
                return None

        async with asyncio.TaskGroup() as tg:
            rb_tasks = [
                tg.create_task(_one_rebuttal(role, report))
                for role, report in reports
            ]

        rebuttals: list[tuple[str, DiscussionRebuttal]] = [
            t.result() for t in rb_tasks if t.result() is not None
        ]

        return challenges, rebuttals

    # ------------------------------------------------------------------
    # Phase 3: Decision — single manager call
    # ------------------------------------------------------------------

    async def _decision_phase(
        self,
        run_id: int,
        matchup: Matchup,
        reports: list[tuple[str, AnalystReport]],
        challenges: list[tuple[str, DiscussionChallenge]],
        rebuttals: list[tuple[str, DiscussionRebuttal]],
    ) -> Prediction:
        weighted_score = compute_weighted_score(
            reports,
            self._config.default_analyst_weights,
            self._config.relevance_multipliers,
        )
        confidence = derive_confidence(
            weighted_score,
            high_threshold=self._config.confidence_high_threshold,
            low_threshold=self._config.confidence_low_threshold,
        )
        # contested = analysts disagree on pick direction
        picks = {rep.pick for _, rep in reports}
        has_contested_challenges = len(picks) > 1

        manager_model = select_manager_model(
            weighted_score,
            has_contested_challenges,
            contested_threshold=self._config.score_contested_threshold,
            consensus_threshold=self._config.score_consensus_threshold,
            model_contested=self._config.effective_manager_model_contested,
            model_moderate=self._config.effective_manager_model_moderate,
            model_consensus=self._config.effective_manager_model_consensus,
        )

        manager_pred, llm_resp = await run_manager(
            self._llm,
            manager_model,
            matchup,
            reports,
            challenges,
            rebuttals,
            weighted_score,
            confidence,
            temperature=0.7,
            max_tokens=self._config.llm_max_tokens,
            user_preferences=self._user_preferences,
            discussion_max_chars=self._config.discussion_max_chars,
        )

        # Clamp predicted_winner to exactly one of the two team names so the
        # UI string comparison always works, regardless of LLM formatting quirks.
        raw_winner = manager_pred.predicted_winner.strip()
        raw_lower  = raw_winner.lower()
        fav_lower  = matchup.favorite_name.lower()
        dog_lower  = matchup.underdog_name.lower()
        if raw_lower == fav_lower or fav_lower in raw_lower or raw_lower in fav_lower:
            predicted_winner = matchup.favorite_name
        elif raw_lower == dog_lower or dog_lower in raw_lower or raw_lower in dog_lower:
            predicted_winner = matchup.underdog_name
        else:
            # Fallback: pick whichever name is more similar
            import difflib
            best = difflib.get_close_matches(raw_lower, [fav_lower, dog_lower], n=1, cutoff=0.0)
            predicted_winner = matchup.favorite_name if (not best or best[0] == fav_lower) else matchup.underdog_name
            logger.warning(
                "predicted_winner %r didn't match either team ('%s' / '%s') — using %r",
                raw_winner, matchup.favorite_name, matchup.underdog_name, predicted_winner,
            )

        is_upset = predicted_winner == matchup.underdog_name
        outcome_type = "upset" if is_upset else "expected"

        async with get_connection() as conn:
            prediction = await PredictionRepository(conn).create(
                run_id=run_id,
                matchup_id=matchup.id,
                predicted_winner=predicted_winner,
                outcome_type=outcome_type,
                weighted_score=weighted_score,
                confidence=confidence,
                synthesis=manager_pred.synthesis,
                manager_model=manager_model,
            )
            await CostRepository(conn).create(
                run_id=run_id,
                matchup_id=matchup.id,
                agent_role="manager",
                model=llm_resp.model,
                phase="decision",
                input_tokens=llm_resp.input_tokens,
                output_tokens=llm_resp.output_tokens,
                cost_usd=llm_resp.cost_usd,
            )

        logger.info(
            "Decision: %s wins (outcome=%s, score=%.2f, confidence=%s, model=%s)",
            manager_pred.predicted_winner, outcome_type, weighted_score,
            confidence, manager_model,
        )
        return prediction
