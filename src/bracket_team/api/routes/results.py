"""Results route: fetch actual tournament outcomes from ESPN."""

from __future__ import annotations

import difflib
import logging
import re

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from bracket_team.api.app import _require_api_key
from bracket_team.db.connection import get_connection
from bracket_team.db.repositories.bracket_repo import BracketRepository
from bracket_team.db.repositories.matchup_repo import MatchupRepository
from bracket_team.scraper.tournament_schedule import TOURNAMENT_SCHEDULE

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/brackets", dependencies=[Depends(_require_api_key)])

_ESPN_SCOREBOARD = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball"
    "/mens-college-basketball/scoreboard"
)


def _team_id(region: str, seed: int) -> str:
    return f"{region}-{seed}"


def _parse_region(notes: list[dict]) -> str | None:
    for note in notes:
        m = re.search(r'\b(East|West|Midwest|South)\b', note.get("headline", ""))
        if m:
            return m.group(1)
    return None


def _normalize(name: str) -> str:
    return name.lower().replace(".", "").replace("'", "").replace("-", "").strip()


def _fuzzy_match(espn_name: str, bracket_name_to_id: dict[str, str]) -> str | None:
    norm = _normalize(espn_name)
    norm_map = {_normalize(n): tid for n, tid in bracket_name_to_id.items()}
    if norm in norm_map:
        return norm_map[norm]
    matches = difflib.get_close_matches(norm, list(norm_map), n=1, cutoff=0.8)
    if matches:
        return norm_map[matches[0]]
    return None


class GameResult(BaseModel):
    team1: str
    team2: str
    winner: str
    round_num: int


class ResultsResponse(BaseModel):
    games: list[GameResult]


@router.get("/{bracket_id}/results", response_model=ResultsResponse)
async def api_get_bracket_results(bracket_id: int) -> ResultsResponse:
    """Return completed tournament games with their actual winner."""
    async with get_connection() as conn:
        bracket = await BracketRepository(conn).get(bracket_id)
        if bracket is None:
            raise HTTPException(status_code=404, detail=f"Bracket {bracket_id} not found")
        r1_matchups = await MatchupRepository(conn).list_by_bracket(bracket_id, round_num=1)

    # Set of valid team IDs to confirm we're seeing tournament bracket teams
    valid_ids: set[str] = set()
    bracket_name_to_id: dict[str, str] = {}
    for m in r1_matchups:
        fid = _team_id(m.region, m.favorite_seed)
        uid = _team_id(m.region, m.underdog_seed)
        valid_ids.add(fid)
        valid_ids.add(uid)
        bracket_name_to_id[m.favorite_name] = fid
        bracket_name_to_id[m.underdog_name] = uid

    sched = TOURNAMENT_SCHEDULE.get(bracket.year)
    if not sched or not valid_ids:
        return ResultsResponse(games=[])

    # Build a map of date → round number so each game is tagged with its round
    date_to_round: dict[str, int] = {}
    for round_num, rinfo in sched.get("rounds", {}).items():
        for d in rinfo.get("dates", []):
            date_to_round[d] = int(round_num)

    games: list[GameResult] = []
    seen_pairs: set[frozenset[str]] = set()  # prevent same game appearing on multiple dates

    async with httpx.AsyncClient(timeout=10.0) as client:
        for date_str in sorted(date_to_round):
            espn_date = date_str.replace("-", "")
            try:
                resp = await client.get(
                    _ESPN_SCOREBOARD,
                    params={"dates": espn_date, "limit": 50},
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                logger.warning("ESPN fetch failed for %s: %s", date_str, exc)
                continue

            for event in data.get("events", []):
                for comp in event.get("competitions", []):
                    if not comp.get("status", {}).get("type", {}).get("completed", False):
                        continue
                    competitors = comp.get("competitors", [])
                    if len(competitors) != 2:
                        continue

                    region = _parse_region(comp.get("notes", []))
                    resolved: list[tuple[str, bool]] = []

                    if region is not None:
                        # Regional games (rounds 1–4): resolve by region + seed
                        for c in competitors:
                            seed = c.get("curatedRank", {}).get("current")
                            if seed is None:
                                continue
                            tid = _team_id(region, int(seed))
                            if tid in valid_ids:
                                resolved.append((tid, bool(c.get("winner", False))))
                    else:
                        # Non-regional games (Final Four / Championship): resolve by name
                        for c in competitors:
                            tid = None
                            for key in ("displayName", "shortDisplayName"):
                                tn = c.get("team", {}).get(key, "")
                                if tn:
                                    tid = _fuzzy_match(tn, bracket_name_to_id)
                                    if tid:
                                        break
                            if tid and tid in valid_ids:
                                resolved.append((tid, bool(c.get("winner", False))))

                    if len(resolved) == 2:
                        winner_id = next((tid for tid, won in resolved if won), None)
                        pair = frozenset([resolved[0][0], resolved[1][0]])
                        if winner_id and pair not in seen_pairs:
                            seen_pairs.add(pair)
                            games.append(GameResult(
                                team1=resolved[0][0],
                                team2=resolved[1][0],
                                winner=winner_id,
                                round_num=date_to_round[date_str],
                            ))

    return ResultsResponse(games=games)
