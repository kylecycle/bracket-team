"""BartTorvik (T-Rank) scraper for NCAA Basketball efficiency metrics.

Fetches AdjO, AdjD, Barthag, Tempo, WAB for all tournament teams via a single
CSV download. No API key required, no JS rendering issues.

URL: https://barttorvik.com/{year}_team_results.csv

CSV column mapping:
  team    → team name (full name, e.g. "Connecticut" not "UConn")
  adjoe   → bart_adj_oe
  adjde   → bart_adj_de
  barthag → barthag
  adjt    → bart_tempo
  WAB     → bart_wab

After the CSV download, a second pass fetches individual team pages to extract
Quad 1/2 records and Luck, which are not available in the CSV export.
Per-team URL: https://barttorvik.com/team.php?team={TeamName}&year={year}
"""

from __future__ import annotations

import csv
import difflib
import io
import logging
import re
from typing import Any
from urllib.parse import quote

import aiosqlite
import httpx
from bs4 import BeautifulSoup

from bracket_team.scraper import FUZZY_MATCH_CUTOFF, FUZZY_MATCH_CUTOFF_SLUG
from bracket_team.scraper.rate_limiter import RateLimiter
from bracket_team.scraper.team_slug_map import TEAM_SLUG_MAP

logger = logging.getLogger(__name__)

_BASE_URL = "https://barttorvik.com"
_RATE_LIMITER = RateLimiter(rate=1.0)

# Explicit overrides for teams whose bracket name and BartTorvik name are too
# different for fuzzy matching to bridge (typically full name vs abbreviation).
_BARTTORVIK_NAME_MAP: dict[str, str] = {
    "Virginia Commonwealth": "VCU",
    "Central Florida": "UCF",
    "UC San Diego": "UCSD",
    "Louisiana State": "LSU",
    "Southern Methodist": "SMU",
    "PV A&M": "Prairie View",
}


def _normalize(name: str) -> str:
    return name.lower().replace(".", "").replace("'", "").replace("-", " ").strip()


def _fuzzy_match(name: str, candidates: list[str], cutoff: float = FUZZY_MATCH_CUTOFF) -> str | None:
    norm_map = {_normalize(c): c for c in candidates}
    matches = difflib.get_close_matches(_normalize(name), list(norm_map), n=1, cutoff=cutoff)
    return norm_map[matches[0]] if matches else None


def _safe_float(text: str) -> float | None:
    try:
        return float(text.strip().replace(",", "").replace("%", ""))
    except (ValueError, AttributeError):
        return None


class BartTorviKScraper:
    """Fetches efficiency metrics from barttorvik.com via CSV download."""

    def __init__(self, year: int = 2025) -> None:
        self._year = year
        self._client = httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0 (compatible; bracket-team-research-bot/1.0)"},
            timeout=30.0,
            follow_redirects=True,
        )
        self._data_cache: dict[str, dict[str, Any]] | None = None

    async def close(self) -> None:
        await self._client.aclose()

    async def _load_all_teams(self) -> dict[str, dict[str, Any]] | None:
        """Download and parse the BartTorvik CSV for all teams.
        Returns {team_name: stats_dict} or None on failure. Result is cached.
        """
        if self._data_cache is not None:
            return self._data_cache

        url = f"{_BASE_URL}/{self._year}_team_results.csv"
        await _RATE_LIMITER.acquire()
        try:
            resp = await self._client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("BartTorvik CSV fetch error: %s", exc)
            return None

        result = self._parse_csv(resp.text)
        if result:
            self._data_cache = result
            logger.info("BartTorvik: loaded %d team records from CSV", len(result))
        else:
            logger.warning("BartTorvik: failed to parse CSV — no data extracted")
        return result

    def _parse_csv(self, text: str) -> dict[str, dict[str, Any]] | None:
        """Parse BartTorvik CSV. Returns {team_name: stats_dict} or None."""
        try:
            reader = csv.DictReader(io.StringIO(text))
        except Exception as exc:
            logger.warning("BartTorvik CSV parse error: %s", exc)
            return None

        results: dict[str, dict[str, Any]] = {}
        for row in reader:
            name = row.get("team", "").strip()
            if not name:
                continue
            results[name] = {
                "bart_adj_oe": _safe_float(row.get("adjoe", "")),
                "bart_adj_de": _safe_float(row.get("adjde", "")),
                "barthag": _safe_float(row.get("barthag", "")),
                "bart_tempo": _safe_float(row.get("adjt", "")),
                "bart_luck": None,   # not in CSV export
                "bart_wab": _safe_float(row.get("WAB", "")),
                "quad1_wins": 0,     # not in CSV export
                "quad1_losses": 0,
                "quad2_wins": 0,
                "quad2_losses": 0,
                "quad3_wins": 0,
                "quad3_losses": 0,
                "quad4_wins": 0,
                "quad4_losses": 0,
            }

        return results if results else None

    async def get_team(self, team_name: str) -> dict[str, Any] | None:
        """Return BartTorvik stats for a single team. None if not found.

        Matching strategy (in order):
        1. Exact match on team_name
        2. Fuzzy match on team_name
        3. Fuzzy match using the Sports Reference slug (e.g. "UConn" → slug
           "connecticut" → matches BartTorvik row "Connecticut")

        After finding the team in the CSV, fetches the per-team page for
        Quad 1/2 records and Luck (not available in the CSV).
        """
        all_data = await self._load_all_teams()
        if not all_data:
            return None

        bt_name: str | None = None
        stats: dict[str, Any] | None = None

        # Explicit override map (full name → BartTorvik abbreviation)
        if team_name in _BARTTORVIK_NAME_MAP:
            mapped = _BARTTORVIK_NAME_MAP[team_name]
            if mapped in all_data:
                logger.debug("BartTorvik name-map: %r -> %r", team_name, mapped)
                bt_name = mapped
                stats = all_data[mapped]

        # Exact match
        if stats is None and team_name in all_data:
            bt_name = team_name
            stats = all_data[team_name]

        # Fuzzy match on team_name
        if stats is None:
            matched = _fuzzy_match(team_name, list(all_data.keys()))
            if matched:
                logger.debug("BartTorvik fuzzy matched %r -> %r", team_name, matched)
                bt_name = matched
                stats = all_data[matched]

        # Slug-based fallback
        if stats is None:
            slug = TEAM_SLUG_MAP.get(team_name) or next(
                (v for k, v in TEAM_SLUG_MAP.items() if k.lower() == team_name.lower()), None
            )
            if slug:
                slug_readable = slug.replace("-", " ")
                matched = _fuzzy_match(slug_readable, list(all_data.keys()), cutoff=FUZZY_MATCH_CUTOFF_SLUG)
                if matched:
                    logger.debug(
                        "BartTorvik slug-matched %r (slug=%r) -> %r", team_name, slug, matched
                    )
                    bt_name = matched
                    stats = all_data[matched]

        if stats is None:
            logger.warning("BartTorvik: no match for %r", team_name)
            return None

        # Second pass: fetch per-team page for Quad records and Luck
        if bt_name:
            extra = await self._fetch_team_page(bt_name)
            if extra:
                stats.update(extra)

        return stats

    async def _fetch_team_page(self, bt_team_name: str) -> dict[str, Any] | None:
        """Fetch BartTorvik per-team page and extract Quad records + Luck.

        URL: https://barttorvik.com/team.php?team={TeamName}&year={year}
        """
        url = f"{_BASE_URL}/team.php?team={quote(bt_team_name)}&year={self._year}"
        await _RATE_LIMITER.acquire()
        try:
            resp = await self._client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("BartTorvik team page error for %r: %s", bt_team_name, exc)
            return None

        return self._parse_team_page(resp.text)

    def _parse_team_page(self, html: str) -> dict[str, Any] | None:
        """Extract Quad 1/2 records and Luck from BartTorvik team page HTML.

        BartTorvik displays quad records in various formats. Common patterns:
        - "Quad 1: 5-3" or "Q1: 5-3" in table cells or text
        - Luck value near "Luck" label
        """
        result: dict[str, Any] = {}
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text()

        # Look for Quad records: "Q1: W-L" or "Quad 1: W-L" patterns
        q1_match = re.search(r"(?:Quad\s*1|Q1)[:\s]+(\d+)\s*-\s*(\d+)", text)
        if q1_match:
            result["quad1_wins"] = int(q1_match.group(1))
            result["quad1_losses"] = int(q1_match.group(2))

        q2_match = re.search(r"(?:Quad\s*2|Q2)[:\s]+(\d+)\s*-\s*(\d+)", text)
        if q2_match:
            result["quad2_wins"] = int(q2_match.group(1))
            result["quad2_losses"] = int(q2_match.group(2))

        q3_match = re.search(r"(?:Quad\s*3|Q3)[:\s]+(\d+)\s*-\s*(\d+)", text)
        if q3_match:
            result["quad3_wins"] = int(q3_match.group(1))
            result["quad3_losses"] = int(q3_match.group(2))

        q4_match = re.search(r"(?:Quad\s*4|Q4)[:\s]+(\d+)\s*-\s*(\d+)", text)
        if q4_match:
            result["quad4_wins"] = int(q4_match.group(1))
            result["quad4_losses"] = int(q4_match.group(2))

        # Look for Luck value
        luck_match = re.search(r"Luck[:\s]+([-+]?\d*\.?\d+)", text)
        if luck_match:
            result["bart_luck"] = float(luck_match.group(1))

        if result:
            logger.debug(
                "BartTorvik team page extracted: %s",
                {k: v for k, v in result.items()},
            )
        return result if result else None


async def upsert_barttorvik_stats(
    conn: aiosqlite.Connection,
    bracket_id: int,
    team_name: str,
    stats: dict[str, Any],
) -> None:
    """Merge BartTorvik metrics into an existing team_stats row.

    Only updates BartTorvik columns — does not touch Sports Reference columns.
    Inserts a minimal row if none exists (so BartTorvik data isn't lost).
    """
    await conn.execute(
        """
        INSERT INTO team_stats (bracket_id, team_name,
            bart_adj_oe, bart_adj_de, barthag, bart_tempo, bart_luck, bart_wab,
            quad1_wins, quad1_losses, quad2_wins, quad2_losses,
            quad3_wins, quad3_losses, quad4_wins, quad4_losses)
        VALUES (:bracket_id, :team_name,
            :bart_adj_oe, :bart_adj_de, :barthag, :bart_tempo, :bart_luck, :bart_wab,
            :quad1_wins, :quad1_losses, :quad2_wins, :quad2_losses,
            :quad3_wins, :quad3_losses, :quad4_wins, :quad4_losses)
        ON CONFLICT(bracket_id, team_name) DO UPDATE SET
            bart_adj_oe=excluded.bart_adj_oe,
            bart_adj_de=excluded.bart_adj_de,
            barthag=excluded.barthag,
            bart_tempo=excluded.bart_tempo,
            bart_luck=excluded.bart_luck,
            bart_wab=excluded.bart_wab,
            quad1_wins=excluded.quad1_wins,
            quad1_losses=excluded.quad1_losses,
            quad2_wins=excluded.quad2_wins,
            quad2_losses=excluded.quad2_losses,
            quad3_wins=excluded.quad3_wins,
            quad3_losses=excluded.quad3_losses,
            quad4_wins=excluded.quad4_wins,
            quad4_losses=excluded.quad4_losses,
            scraped_at=datetime('now')
        """,
        {
            "bracket_id": bracket_id,
            "team_name": team_name,
            "bart_adj_oe": stats.get("bart_adj_oe"),
            "bart_adj_de": stats.get("bart_adj_de"),
            "barthag": stats.get("barthag"),
            "bart_tempo": stats.get("bart_tempo"),
            "bart_luck": stats.get("bart_luck"),
            "bart_wab": stats.get("bart_wab"),
            "quad1_wins": stats.get("quad1_wins", 0),
            "quad1_losses": stats.get("quad1_losses", 0),
            "quad2_wins": stats.get("quad2_wins", 0),
            "quad2_losses": stats.get("quad2_losses", 0),
            "quad3_wins": stats.get("quad3_wins", 0),
            "quad3_losses": stats.get("quad3_losses", 0),
            "quad4_wins": stats.get("quad4_wins", 0),
            "quad4_losses": stats.get("quad4_losses", 0),
        },
    )
    await conn.commit()
