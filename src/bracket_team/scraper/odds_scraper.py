"""Odds scraper: The Odds API.

Provides spreads, moneylines, over/under, and implied win probabilities.
Lines only appear 1-2 weeks before games; returns empty data outside the
tournament window.

Rate limit: 500 requests/month on free tier — one event-list fetch per
gather session covers all matchups.

Usage:
    scraper = OddsScraper(api_key="your-key")
    odds = await scraper.scrape_matchup(bracket_id=1, matchup_id=5,
                                         favorite_name="Duke", underdog_name="Gonzaga")
    print(scraper.odds_api_remaining)  # requests left this month
"""

from __future__ import annotations

import difflib
import logging
from typing import Any

import aiosqlite
import httpx

from bracket_team.scraper import FUZZY_MATCH_CUTOFF

logger = logging.getLogger(__name__)

_ODDS_API_BASE = "https://api.the-odds-api.com/v4"
_SPORT_KEY = "basketball_ncaab"


def _ml_to_implied_pct(ml: int) -> float:
    """Convert American moneyline to implied win probability (0-1)."""
    if ml > 0:
        return 100.0 / (ml + 100.0)
    else:
        return abs(ml) / (abs(ml) + 100.0)


def _normalize_name(name: str) -> str:
    return name.lower().replace(".", "").replace("'", "").replace("(", "").replace(")", "").strip()


# Explicit overrides for bracket names that can't be matched automatically.
# Keys are bracket names; values are the Odds API team name substring to prefer.
_ODDS_TEAM_MAP: dict[str, str] = {
    "Miami FL": "Miami Hurricanes",
    "St Johns NY": "St. John's Red Storm",
    "NC State": "N.C. State",
    "Virginia Commonwealth": "VCU",
    "Central Florida": "UCF",
    # Short names that are prefixes of a different school's full name
    "Illinois": "Illinois Fighting Illini",
    "Iowa": "Iowa Hawkeyes",
    "Michigan": "Michigan Wolverines",
    "Penn": "Pennsylvania Quakers",
    "Tennessee": "Tennessee Volunteers",
}

# Disambiguation suffixes appended to bracket names to distinguish schools
# with identical city names. Strip before starts-with matching.
_BRACKET_SUFFIXES = (" fl", " oh", " ny", " ca", " tx", " pa", " va", " st")

# Tokens that indicate a school name extension rather than a nickname.
# "Illinois St" should not match "Illinois"; "Iowa State" should not match "Iowa".
_STATE_TOKENS = {"st", "state", "tech", "a&m", "am"}


def _match_odds_team(bracket_name: str, api_names: list[str]) -> str | None:
    """Match a bracket short name to an Odds API full name with nickname.

    Strategy (in order):
    1. Explicit override map
    2. Exact normalized match
    3. API name starts with bracket name, and the next word is NOT a state/tech
       qualifier (avoids matching "Illinois" to "Illinois St Redbirds")
       — only returns when there is exactly one unambiguous match
    4. Fuzzy fallback at relaxed cutoff
    """
    norm_b = _normalize_name(bracket_name)

    # 1. Explicit map
    override = _ODDS_TEAM_MAP.get(bracket_name)
    if override:
        override_norm = _normalize_name(override)
        for name in api_names:
            if override_norm in _normalize_name(name) or _normalize_name(name) in override_norm:
                return name
        return None

    # Strip disambiguation suffixes (e.g. "Miami FL" -> "miami", "St Johns NY" -> "st johns")
    clean_b = norm_b
    for suf in _BRACKET_SUFFIXES:
        if clean_b.endswith(suf):
            clean_b = clean_b[: -len(suf)].strip()
            break

    norm_map = {_normalize_name(c): c for c in api_names}

    # 2. Exact match
    if norm_b in norm_map:
        return norm_map[norm_b]

    # 3. Starts-with: API name begins with the bracket name + space.
    # Collect all candidates; discard those where the next token is a state qualifier.
    # Only return when exactly one non-state match exists (avoids false positives).
    sw_candidates = []
    for norm_api, orig_api in norm_map.items():
        if norm_api == clean_b:
            return orig_api  # exact match on stripped name
        if norm_api.startswith(clean_b + " "):
            next_token = norm_api[len(clean_b) + 1:].split()[0] if norm_api[len(clean_b) + 1:].split() else ""
            if next_token not in _STATE_TOKENS:
                sw_candidates.append(orig_api)

    if len(sw_candidates) == 1:
        return sw_candidates[0]

    # 4. Fuzzy fallback with relaxed cutoff
    matches = difflib.get_close_matches(norm_b, list(norm_map.keys()), n=1, cutoff=0.6)
    return norm_map[matches[0]] if matches else None


def _fuzzy_match(name: str, candidates: list[str], cutoff: float = FUZZY_MATCH_CUTOFF) -> str | None:
    """Legacy fuzzy match used outside the Odds API team-name context."""
    matches = difflib.get_close_matches(
        _normalize_name(name),
        [_normalize_name(c) for c in candidates],
        n=1,
        cutoff=cutoff,
    )
    if matches:
        norm_map = {_normalize_name(c): c for c in candidates}
        return norm_map.get(matches[0])
    return None


class OddsScraper:
    """Fetches betting odds from The Odds API and enrichment from Covers.com."""

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0 (compatible; bracket-team-research-bot/1.0)"},
            timeout=30.0,
            follow_redirects=True,
        )
        self._events_cache: list | None = None  # fetched once per scraper session
        self._odds_api_remaining: int | None = None  # updated after each event-list fetch

    @property
    def odds_api_remaining(self) -> int | None:
        """Requests remaining this month from The Odds API (None if not yet fetched)."""
        return self._odds_api_remaining

    async def close(self) -> None:
        await self._client.aclose()

    async def scrape_matchup(
        self,
        bracket_id: int,
        matchup_id: int,
        favorite_name: str,
        underdog_name: str,
    ) -> dict[str, Any]:
        """Fetch odds for a specific matchup. Returns empty record if lines unavailable."""
        if not self._api_key:
            logger.info("No Odds API key configured — skipping odds scrape")
            return self._empty_record(bracket_id, matchup_id, favorite_name, underdog_name)

        odds_data = await self._fetch_odds_api(favorite_name, underdog_name)
        if not odds_data:
            logger.info("No odds found for %s vs %s", favorite_name, underdog_name)
            return self._empty_record(bracket_id, matchup_id, favorite_name, underdog_name)

        return self._parse_odds_api(bracket_id, matchup_id, favorite_name, underdog_name, odds_data)

    async def _get_all_events(self) -> list:
        """Fetch all upcoming NCAAB events, cached for the scraper session (1 API call total)."""
        if self._events_cache is not None:
            return self._events_cache
        try:
            resp = await self._client.get(
                f"{_ODDS_API_BASE}/sports/{_SPORT_KEY}/odds",
                params={
                    "apiKey": self._api_key,
                    "regions": "us",
                    "markets": "h2h,spreads,totals",
                    "oddsFormat": "american",
                },
            )
            resp.raise_for_status()
            events = resp.json()
            self._events_cache = events if isinstance(events, list) else []
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Odds API error: %s", exc)
            self._events_cache = []
        remaining_str = resp.headers.get("x-requests-remaining", "")
        if remaining_str.isdigit():
            self._odds_api_remaining = int(remaining_str)
        logger.info(
            "Odds API: fetched %d events (%s requests remaining this month)",
            len(self._events_cache),
            self._odds_api_remaining if self._odds_api_remaining is not None else "?",
        )
        return self._events_cache

    async def _fetch_odds_api(
        self, favorite_name: str, underdog_name: str
    ) -> dict | None:
        """Find a matching event in the cached event list."""
        events = await self._get_all_events()
        if not events:
            return None

        # Find the game matching our teams
        team_names = [e.get("home_team", "") for e in events] + [
            e.get("away_team", "") for e in events
        ]
        fav_match = _match_odds_team(favorite_name, team_names)
        dog_match = _match_odds_team(underdog_name, team_names)

        # Both teams must be found — if only one matches we'd get that team's
        # unrelated real game, not the bracket matchup.
        if not fav_match or not dog_match:
            logger.debug(
                "Odds: one or both teams not in API — fav=%r→%r  dog=%r→%r",
                favorite_name, fav_match, underdog_name, dog_match,
            )
            return None

        for event in events:
            event_teams = {event.get("home_team", ""), event.get("away_team", "")}
            if {fav_match, dog_match}.issubset(event_teams):
                return event

        return None

    def _parse_odds_api(
        self,
        bracket_id: int,
        matchup_id: int,
        favorite_name: str,
        underdog_name: str,
        event: dict,
    ) -> dict[str, Any]:
        """Extract spread, ML, and O/U from Odds API event."""
        record: dict[str, Any] = {
            "bracket_id": bracket_id,
            "matchup_id": matchup_id,
            "favorite_name": favorite_name,
            "underdog_name": underdog_name,
            "spread": None,
            "favorite_ml": None,
            "underdog_ml": None,
            "over_under": None,
            "implied_fav_win_pct": None,
            "implied_dog_win_pct": None,
        }

        bookmakers = event.get("bookmakers", [])
        if not bookmakers:
            return record

        # Use first available bookmaker (prefer DraftKings or FanDuel)
        preferred = ["draftkings", "fanduel", "betmgm"]
        bookmaker = bookmakers[0]
        for bm in bookmakers:
            if bm.get("key", "") in preferred:
                bookmaker = bm
                break

        for market in bookmaker.get("markets", []):
            market_key = market.get("key", "")
            outcomes = market.get("outcomes", [])

            if market_key == "h2h":
                for o in outcomes:
                    team = o.get("name", "")
                    price = o.get("price", 0)
                    if _match_odds_team(favorite_name, [team]):
                        record["favorite_ml"] = price
                        record["implied_fav_win_pct"] = round(_ml_to_implied_pct(price), 4)
                    elif _match_odds_team(underdog_name, [team]):
                        record["underdog_ml"] = price
                        record["implied_dog_win_pct"] = round(_ml_to_implied_pct(price), 4)

            elif market_key == "spreads":
                for o in outcomes:
                    team = o.get("name", "")
                    point = o.get("point", 0)
                    if _match_odds_team(favorite_name, [team]):
                        record["spread"] = point  # negative = favorite giving points

            elif market_key == "totals":
                for o in outcomes:
                    if o.get("name") == "Over":
                        record["over_under"] = o.get("point")

        return record

    @staticmethod
    def _empty_record(
        bracket_id: int, matchup_id: int, favorite_name: str, underdog_name: str
    ) -> dict[str, Any]:
        return {
            "bracket_id": bracket_id,
            "matchup_id": matchup_id,
            "favorite_name": favorite_name,
            "underdog_name": underdog_name,
            "spread": None,
            "favorite_ml": None,
            "underdog_ml": None,
            "over_under": None,
            "implied_fav_win_pct": None,
            "implied_dog_win_pct": None,
        }


async def upsert_team_odds(
    conn: aiosqlite.Connection, record: dict[str, Any]
) -> None:
    """Insert or replace odds record for a matchup."""
    await conn.execute(
        """
        INSERT INTO team_odds
            (bracket_id, matchup_id, favorite_name, underdog_name,
             spread, favorite_ml, underdog_ml, over_under,
             implied_fav_win_pct, implied_dog_win_pct)
        VALUES
            (:bracket_id, :matchup_id, :favorite_name, :underdog_name,
             :spread, :favorite_ml, :underdog_ml, :over_under,
             :implied_fav_win_pct, :implied_dog_win_pct)
        ON CONFLICT(matchup_id) DO UPDATE SET
            spread=excluded.spread,
            favorite_ml=excluded.favorite_ml,
            underdog_ml=excluded.underdog_ml,
            over_under=excluded.over_under,
            implied_fav_win_pct=excluded.implied_fav_win_pct,
            implied_dog_win_pct=excluded.implied_dog_win_pct,
            scraped_at=datetime('now')
        """,
        {
            "bracket_id": record["bracket_id"],
            "matchup_id": record["matchup_id"],
            "favorite_name": record["favorite_name"],
            "underdog_name": record["underdog_name"],
            "spread": record.get("spread"),
            "favorite_ml": record.get("favorite_ml"),
            "underdog_ml": record.get("underdog_ml"),
            "over_under": record.get("over_under"),
            "implied_fav_win_pct": record.get("implied_fav_win_pct"),
            "implied_dog_win_pct": record.get("implied_dog_win_pct"),
        },
    )
    await conn.commit()
