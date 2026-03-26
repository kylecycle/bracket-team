"""ESPN player stats scraper for NCAA Men's Basketball.

Fetches top players (by minutes) for each team from ESPN's JSON API.
Stores in team_player_stats table. Used by the injury analyst to assess
the impact of specific player injuries.

Rate-limited to 2 req/sec (reuses ESPN team IDs from injury scraper).
"""

from __future__ import annotations

import difflib
import logging
from typing import Any

import aiosqlite
import httpx

from bracket_team.scraper import FUZZY_MATCH_CUTOFF
from bracket_team.scraper.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

_ESPN_TEAM_API = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/teams"
)

# Explicit overrides for bracket names that are ambiguous or differ from ESPN display names
_ESPN_NAME_MAP: dict[str, str] = {
    "Miami FL": "Miami",
    "Miami OH": "Miami (OH)",
}
_RATE_LIMITER = RateLimiter(rate=2.0)
_TOP_PLAYERS = 5  # number of top-minutes players to store per team


def _normalize_name(name: str) -> str:
    return name.lower().replace(".", "").replace("'", "").strip()


def _fuzzy_match_team(team_name: str, candidates: list[str]) -> str | None:
    matches = difflib.get_close_matches(
        _normalize_name(team_name),
        [_normalize_name(c) for c in candidates],
        n=1,
        cutoff=FUZZY_MATCH_CUTOFF,
    )
    if matches:
        norm_to_orig = {_normalize_name(c): c for c in candidates}
        return norm_to_orig.get(matches[0])
    return None


class ESPNPlayerScraper:
    """Fetches top player stats per team from ESPN's JSON API."""

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; bracket-team-research-bot/1.0)",
                "Accept": "application/json",
            },
            timeout=30.0,
            follow_redirects=True,
        )
        self._team_id_cache: dict[str, str] = {}

    async def close(self) -> None:
        await self._client.aclose()

    async def _get_team_id(self, team_name: str) -> str | None:
        if team_name in self._team_id_cache:
            return self._team_id_cache[team_name]

        # Resolve bracket name aliases before hitting ESPN
        lookup_name = _ESPN_NAME_MAP.get(team_name, team_name)

        await _RATE_LIMITER.acquire()
        try:
            resp = await self._client.get(_ESPN_TEAM_API, params={"limit": 500})
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Failed to fetch ESPN team list: %s", exc)
            return None

        teams = data.get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", [])
        name_to_id: dict[str, str] = {}
        for entry in teams:
            t = entry.get("team", {})
            for key in ("displayName", "shortDisplayName", "name"):
                n = t.get(key, "")
                if n:
                    name_to_id[n] = t.get("id", "")

        for n, tid in name_to_id.items():
            self._team_id_cache[n] = tid

        if lookup_name in name_to_id:
            tid = name_to_id[lookup_name]
            self._team_id_cache[team_name] = tid
            return tid

        matched = _fuzzy_match_team(lookup_name, list(name_to_id.keys()))
        if matched:
            tid = name_to_id[matched]
            self._team_id_cache[team_name] = tid
            return tid

        logger.warning("ESPN player scraper: team not found for %r", team_name)
        return None

    async def scrape_team(
        self, bracket_id: int, team_name: str
    ) -> list[dict[str, Any]]:
        """Fetch top-N players by minutes for a team. Returns [] on failure."""
        team_id = await self._get_team_id(team_name)
        if not team_id:
            return []

        url = (
            f"https://site.api.espn.com/apis/site/v2/sports/basketball/"
            f"mens-college-basketball/teams/{team_id}/statistics"
        )
        await _RATE_LIMITER.acquire()
        try:
            resp = await self._client.get(url)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("ESPN player stats error for %r: %s", team_name, exc)
            return []

        players = self._parse_player_stats(bracket_id, team_name, data)

        # Fetch roster injury/status data and merge into player dicts
        injury_map = await self._fetch_roster_injuries(team_id, team_name)
        self._merge_injury_status(players, injury_map, team_name)

        return players

    async def _fetch_roster_injuries(
        self, team_id: str, team_name: str
    ) -> dict[str, dict[str, Any]]:
        """Fetch roster endpoint and return a map of normalized name -> injury info.

        Returns empty dict on failure (graceful degradation).
        """
        url = (
            f"https://site.api.espn.com/apis/site/v2/sports/basketball/"
            f"mens-college-basketball/teams/{team_id}/roster"
        )
        await _RATE_LIMITER.acquire()
        try:
            resp = await self._client.get(url)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("ESPN roster fetch error for %r: %s", team_name, exc)
            return {}

        injury_map: dict[str, dict[str, Any]] = {}
        athletes = data.get("athletes", [])
        for athlete in athletes:
            display_name = athlete.get("displayName", "")
            if not display_name:
                continue

            status = athlete.get("status", {})
            status_type = status.get("type", "active")
            status_name = status.get("name", "")
            injuries = athlete.get("injuries", [])

            injured = status_type != "active"

            injury_note: str | None = None
            if injured:
                parts = []
                if status_name:
                    parts.append(status_name)
                for inj in injuries:
                    detail = inj.get("details", {}).get("detail", "") or inj.get("longComment", "")
                    if detail:
                        parts.append(detail)
                        break  # use first injury detail
                injury_note = " - ".join(parts) if parts else None

            injury_map[_normalize_name(display_name)] = {
                "injured": injured,
                "injury_note": injury_note,
            }

        return injury_map

    @staticmethod
    def _merge_injury_status(
        players: list[dict[str, Any]],
        injury_map: dict[str, dict[str, Any]],
        team_name: str,
    ) -> None:
        """Cross-reference roster injury data into player dicts using fuzzy matching."""
        roster_names = list(injury_map.keys())
        flagged_count = 0

        for player in players:
            player_norm = _normalize_name(player["player_name"])

            # Try exact match first
            info = injury_map.get(player_norm)

            # Fuzzy match fallback
            if info is None and roster_names:
                matches = difflib.get_close_matches(
                    player_norm, roster_names, n=1, cutoff=FUZZY_MATCH_CUTOFF,
                )
                if matches:
                    info = injury_map.get(matches[0])

            if info is not None:
                player["injured"] = info["injured"]
                player["injury_note"] = info["injury_note"]
                if info["injured"]:
                    flagged_count += 1
            else:
                player["injured"] = False
                player["injury_note"] = None

        logger.info(
            "ESPN roster: %d/%d players flagged as non-active for %s",
            flagged_count,
            len(players),
            team_name,
        )

    def _parse_player_stats(
        self, bracket_id: int, team_name: str, data: dict
    ) -> list[dict[str, Any]]:
        """Extract top players from ESPN statistics response."""
        # ESPN statistics endpoint returns category arrays; player-level detail
        # may be in athletes sub-key depending on endpoint version.
        athletes: list[dict] = data.get("athletes", [])
        if not athletes:
            # Try nested path
            splits = data.get("splits", {})
            athletes = splits.get("athletes", [])

        if not athletes:
            logger.debug("ESPN player stats: no athlete data for %r", team_name)
            return []

        players: list[dict[str, Any]] = []
        for athlete in athletes:
            display_name = athlete.get("displayName") or athlete.get("name", "")
            position = athlete.get("position", {}).get("abbreviation", "")
            class_year = athlete.get("experience", {}).get("abbreviation", "")

            # Find stats
            stats_list = athlete.get("stats", [])
            stat_map: dict[str, float] = {}
            for stat in stats_list:
                name = stat.get("name", "").lower()
                try:
                    stat_map[name] = float(stat.get("value", 0))
                except (ValueError, TypeError):
                    pass

            mpg = stat_map.get("minutespergame") or stat_map.get("avgminutes")
            ppg = stat_map.get("pointspergame") or stat_map.get("avgpoints")
            rpg = stat_map.get("reboundspergame") or stat_map.get("avgtotalrebounds")
            apg = stat_map.get("assistspergame") or stat_map.get("avgassists")
            ft_pct = stat_map.get("freethrowpct") or stat_map.get("freethrowpercentage")
            three_pt_pct = stat_map.get("threepointfieldgoalpct") or stat_map.get("threepointpercentage")
            usage_rate = stat_map.get("usagerate") or stat_map.get("usage")

            if not display_name:
                continue

            players.append({
                "bracket_id": bracket_id,
                "team_name": team_name,
                "player_name": display_name,
                "position": position or None,
                "class_year": class_year or None,
                "ppg": ppg,
                "rpg": rpg,
                "apg": apg,
                "mpg": mpg,
                "ft_pct": ft_pct,
                "three_pt_pct": three_pt_pct,
                "usage_rate": usage_rate,
            })

        # Sort by minutes descending; return top N
        players.sort(key=lambda p: p.get("mpg") or 0, reverse=True)
        return players[:_TOP_PLAYERS]


async def upsert_player_stats(
    conn: aiosqlite.Connection, player: dict[str, Any]
) -> None:
    """Insert or replace a player stats row."""
    await conn.execute(
        """
        INSERT INTO team_player_stats
            (bracket_id, team_name, player_name, position, class_year,
             ppg, rpg, apg, mpg, ft_pct, three_pt_pct, usage_rate,
             injured, injury_note)
        VALUES
            (:bracket_id, :team_name, :player_name, :position, :class_year,
             :ppg, :rpg, :apg, :mpg, :ft_pct, :three_pt_pct, :usage_rate,
             :injured, :injury_note)
        ON CONFLICT(bracket_id, team_name, player_name) DO UPDATE SET
            position=excluded.position,
            class_year=excluded.class_year,
            ppg=excluded.ppg,
            rpg=excluded.rpg,
            apg=excluded.apg,
            mpg=excluded.mpg,
            ft_pct=excluded.ft_pct,
            three_pt_pct=excluded.three_pt_pct,
            usage_rate=excluded.usage_rate,
            injured=excluded.injured,
            injury_note=excluded.injury_note,
            scraped_at=datetime('now')
        """,
        {
            "bracket_id": player["bracket_id"],
            "team_name": player["team_name"],
            "player_name": player["player_name"],
            "position": player.get("position"),
            "class_year": player.get("class_year"),
            "ppg": player.get("ppg"),
            "rpg": player.get("rpg"),
            "apg": player.get("apg"),
            "mpg": player.get("mpg"),
            "ft_pct": player.get("ft_pct"),
            "three_pt_pct": player.get("three_pt_pct"),
            "usage_rate": player.get("usage_rate"),
            "injured": player.get("injured", False),
            "injury_note": player.get("injury_note"),
        },
    )
    await conn.commit()
