"""Sports Reference CBB scraper: team stats and roster data.

Scrapes season stats (record, efficiency, SOS, etc.) and roster composition
from sports-reference.com/cbb. Rate-limited to 1 req/sec per their guidelines.

Usage:
    scraper = SportsScraper()
    stats = await scraper.scrape_team(bracket_id=1, team_name="Duke")
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
from typing import Any

import aiosqlite
import httpx
from bs4 import BeautifulSoup, Comment

from bracket_team.scraper import FUZZY_MATCH_CUTOFF
from bracket_team.scraper.rate_limiter import RateLimiter
from bracket_team.scraper.team_slug_map import TEAM_SLUG_MAP

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.sports-reference.com/cbb/schools"
_RATE_LIMITER = RateLimiter(rate=0.33)  # 1 req per 3s — SR 429s above ~20 req/min; retry handles bursts
_MAX_RETRIES = 1
_RETRY_BACKOFF = 65.0  # seconds to wait after a 429


def _resolve_slug(team_name: str) -> str | None:
    """Return the Sports Reference slug for a team name, with fuzzy fallback."""
    # Exact match first
    if team_name in TEAM_SLUG_MAP:
        return TEAM_SLUG_MAP[team_name]
    # Case-insensitive exact
    lower = team_name.lower()
    for key, slug in TEAM_SLUG_MAP.items():
        if key.lower() == lower:
            return slug
    matches = difflib.get_close_matches(team_name, TEAM_SLUG_MAP.keys(), n=1, cutoff=FUZZY_MATCH_CUTOFF)
    if matches:
        logger.debug("Fuzzy matched %r → %r", team_name, matches[0])
        return TEAM_SLUG_MAP[matches[0]]
    return None


def _parse_record(text: str) -> tuple[int | None, int | None]:
    """Parse 'W-L' or 'W-L-T' record string → (wins, losses)."""
    m = re.match(r"(\d+)-(\d+)", text.strip())
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


def _safe_float(text: str) -> float | None:
    """Parse a float, returning None on failure."""
    try:
        return float(text.strip().replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _norm_name(name: str) -> str:
    """Normalize a player name for fuzzy matching between tables."""
    return re.sub(r"\s+", " ", name.lower().replace(".", "").replace("'", "").replace("-", " ")).strip()


def _safe_int(text: str) -> int | None:
    """Parse an int, returning None on failure."""
    try:
        return int(text.strip().replace(",", ""))
    except (ValueError, AttributeError):
        return None


class SportsScraper:
    """Scrapes Sports Reference CBB for team stats and roster data."""

    def __init__(self, year: int = 2025) -> None:
        self._year = year
        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; bracket-team-research-bot/1.0; "
                    "+https://github.com/bracket-team)"
                )
            },
            timeout=30.0,
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, url: str) -> httpx.Response | None:
        """Rate-limited GET with retry on 429. Returns None on persistent failure."""
        for attempt in range(_MAX_RETRIES + 1):
            await _RATE_LIMITER.acquire()
            try:
                resp = await self._client.get(url)
                if resp.status_code == 429:
                    if attempt < _MAX_RETRIES:
                        logger.warning("SR rate limited (429) on %s — waiting %.0fs", url, _RETRY_BACKOFF)
                        await asyncio.sleep(_RETRY_BACKOFF)
                        continue
                    logger.warning("SR rate limited (429) on %s after %d retries — skipping", url, _MAX_RETRIES)
                    return None
                resp.raise_for_status()
                return resp
            except httpx.HTTPStatusError as exc:
                logger.warning("HTTP error on %s: %s", url, exc)
                return None
            except httpx.HTTPError as exc:
                logger.warning("HTTP error on %s: %s", url, exc)
                return None
        return None

    async def scrape_team(
        self, bracket_id: int, team_name: str
    ) -> dict[str, Any] | None:
        """Scrape stats for a single team. Returns None if team not found.

        Makes two requests: main stats page + schedule page. The schedule page
        provides season record, conf record, neutral site record, last-10 form,
        and conference tournament record — all in one fetch.

        The returned dict includes a special ``_players`` key containing a list
        of player stat dicts (not a team_stats column — handled by coordinator).
        """
        slug = _resolve_slug(team_name)
        if slug is None:
            logger.warning("No slug found for team %r — skipping", team_name)
            return None

        # Request 1: main stats page
        # Sports Reference updated URL structure: /cbb/schools/{slug}/men/{year}.html
        # Try the new path first, fall back to the legacy path without /men/.
        main_url = f"{_BASE_URL}/{slug}/men/{self._year}.html"
        response = await self._get(main_url)
        if response is None:
            main_url = f"{_BASE_URL}/{slug}/{self._year}.html"
            response = await self._get(main_url)
        if response is None:
            return None

        soup = BeautifulSoup(response.text, "lxml")
        stats = self._extract_stats(soup, team_name, bracket_id)
        stats.update(self._extract_roster(soup))
        roster_info = self._get_roster_player_info(soup)
        players = self._extract_players(soup, bracket_id, team_name, roster_info)

        # Request 2: schedule page (record, conf W-L, neutral, last-10, conf tourney)
        sched_url = f"{_BASE_URL}/{slug}/men/{self._year}-schedule.html"
        sched_resp = await self._get(sched_url)
        if sched_resp is None:
            sched_url = f"{_BASE_URL}/{slug}/{self._year}-schedule.html"
            sched_resp = await self._get(sched_url)
        if sched_resp is not None:
            sched_soup = BeautifulSoup(sched_resp.text, "lxml")
            stats.update(self._parse_schedule_stats(sched_soup))

        # Request 3: coach page for NCAA tournament history
        coach_href = stats.pop("_coach_href", None)
        if coach_href:
            coach_data = await self._fetch_coach_tourney_record(coach_href)
            stats.update(coach_data)

        stats["_players"] = players
        return stats

    def _extract_stats(
        self, soup: BeautifulSoup, team_name: str, bracket_id: int
    ) -> dict[str, Any]:
        stats: dict[str, Any] = {
            "bracket_id": bracket_id,
            "team_name": team_name,
        }

        # Conference name
        conf_link = soup.find("a", href=re.compile(r"/cbb/conferences/"))
        stats["conference"] = conf_link.get_text().strip() if conf_link else None

        # Per-game team & opponent stats (Sports Reference table ID as of 2024+)
        # Table has rows with entity: 'Team', 'Rank', 'Opponent'
        pg_table = self._find_table_in_soup_or_comments(
            soup, ids=("season-total_per_game",)
        )
        if pg_table:
            for row in pg_table.find_all("tr"):
                cells = row.find_all(["td", "th"])
                if not cells:
                    continue
                row_data = {c.get("data-stat", ""): c.get_text().strip() for c in cells}

                if row_data.get("entity") == "Team":
                    stats["fg_pct"] = _safe_float(row_data.get("fg_pct", ""))
                    stats["three_pt_pct"] = _safe_float(row_data.get("fg3_pct", ""))
                    stats["ft_pct"] = _safe_float(row_data.get("ft_pct", ""))
                    stats["ppg"] = _safe_float(row_data.get("pts_per_g", ""))
                    stats["orb_per_g"] = _safe_float(row_data.get("orb_per_g", ""))
                    stats["drb_per_g"] = _safe_float(row_data.get("drb_per_g", ""))
                    stats["ast_per_g"] = _safe_float(row_data.get("ast_per_g", ""))
                    stats["stl_per_g"] = _safe_float(row_data.get("stl_per_g", ""))
                    stats["blk_per_g"] = _safe_float(row_data.get("blk_per_g", ""))
                    fta = _safe_float(row_data.get("fta_per_g", ""))
                    fga = _safe_float(row_data.get("fga_per_g", ""))
                    if fta is not None and fga:
                        stats["ft_rate"] = round(fta / fga, 4)
                    # Compute TOV% from per-game turnovers
                    tov_pg = _safe_float(row_data.get("tov_per_g", ""))
                    if tov_pg is not None and fga:
                        fta_adj = (fta or 0) * 0.44
                        poss = fga + fta_adj + tov_pg
                        if poss > 0:
                            stats["tov_pct"] = round(100 * tov_pg / poss, 1)

                elif row_data.get("entity") == "Opponent":
                    stats["opp_ppg"] = _safe_float(row_data.get("opp_pts_per_g", ""))
                    stats["opp_fg_pct"] = _safe_float(row_data.get("opp_fg_pct", ""))
                    stats["opp_three_pt_pct"] = _safe_float(row_data.get("opp_fg3_pct", ""))
                    stats["opp_tov_per_g"] = _safe_float(row_data.get("opp_tov_per_g", ""))

        # SRS, SOS, ORtg, DRtg, AP rank are displayed in <p><strong> info
        # paragraphs at the top of the page (not in any table).
        stats.update(self._extract_info_stats(soup))

        # Head coach — extract name and store the link for tourney appearances lookup
        coach_link = soup.find("a", href=re.compile(r"/cbb/coaches/"))
        stats["head_coach"] = coach_link.get_text().strip() if coach_link else None
        stats["_coach_href"] = coach_link["href"] if coach_link and coach_link.get("href") else None

        # NET ranking: SR pages often display NET rank in a summary/meta section
        # Look for "NET" or "NET Ranking" in page text
        net_rank = self._extract_net_rank(soup)
        if net_rank is not None:
            stats["net_rank"] = net_rank

        return stats

    def _extract_info_stats(self, soup: BeautifulSoup) -> dict[str, Any]:
        """Extract SRS, SOS, ORtg, DRtg from the page's info <p> paragraphs.

        Sports Reference displays these as:
          <p><strong>SRS:</strong> 20.67 (10th of 362)</p>
          <p><strong>ORtg:</strong> 117.1 (15th of 362)</p>
        """
        result: dict[str, Any] = {}

        for p in soup.find_all("p"):
            text = p.get_text()

            # SRS: 20.67 (10th of 362)
            m = re.search(r"SRS[:\s]+([-\d.]+)", text)
            if m and result.get("srs") is None:
                result["srs"] = _safe_float(m.group(1))

            # SOS: 8.36 (50th of 362)
            m = re.search(r"SOS[:\s]+([-\d.]+)", text)
            if m and result.get("sos_rank") is None:
                result["sos_rank"] = _safe_float(m.group(1))

            # ORtg: 117.1 (15th of 362)
            m = re.search(r"ORtg[:\s]+([\d.]+)", text)
            if m and result.get("adj_off_eff") is None:
                result["adj_off_eff"] = _safe_float(m.group(1))

            # DRtg: 98.8 (51st of 362)
            m = re.search(r"DRtg[:\s]+([\d.]+)", text)
            if m and result.get("adj_def_eff") is None:
                result["adj_def_eff"] = _safe_float(m.group(1))

            # Pace sometimes appears in info section too
            m = re.search(r"Pace[:\s]+([\d.]+)", text)
            if m and result.get("pace") is None:
                result["pace"] = _safe_float(m.group(1))

            # AP ranking: "Rank: 9th in the Final AP Poll"
            m = re.search(r"Rank[:\s]+(\d+)(?:st|nd|rd|th)\s+in\s+the\s+Final\s+AP", text)
            if m and result.get("ap_rank") is None:
                result["ap_rank"] = int(m.group(1))

        return result

    def _extract_net_rank(self, soup: BeautifulSoup) -> int | None:
        """Extract NET ranking from the team's season page.

        Sports Reference shows NET rank in the team summary section,
        often as "NET: #XX" or in a meta/info box.
        """
        # Check for NET in paragraph/div text near team summary
        for el in soup.find_all(["p", "div", "span", "strong"]):
            text = el.get_text()
            match = re.search(r"NET[:\s#]+(\d+)", text, re.I)
            if match:
                return int(match.group(1))

        # Also check for data-stat="net_rtg" or similar in tables
        for table_id in ("school_stats", "team_stats", "meta"):
            table = self._find_table_in_soup_or_comments(soup, ids=(table_id,))
            if not table:
                continue
            for row in table.find_all("tr"):
                cells = row.find_all(["td", "th"])
                row_data = {c.get("data-stat", ""): c.get_text().strip() for c in cells}
                net_val = row_data.get("net_rtg") or row_data.get("net_rank")
                if net_val:
                    return _safe_int(net_val)

        return None

    async def _fetch_coach_tourney_record(self, coach_href: str) -> dict[str, Any]:
        """Fetch the coach's SR page and extract NCAA tournament history.

        SR coach pages (e.g. /cbb/coaches/tom-izzo-1.html) have a season-by-season
        table with a Notes column containing entries like:
          "NCAA Tournament", "NCAA FF" (Final Four), "NCAA Champion"

        Returns dict with:
          - coach_tourney_appearances: int count of tournament appearances
          - coach_tourney_record: str summary like "25 apps, 3 Final Fours, 2 Championships"
        """
        result: dict[str, Any] = {}
        url = f"https://www.sports-reference.com{coach_href}"
        resp = await self._get(url)
        if resp is None:
            return result

        soup = BeautifulSoup(resp.text, "lxml")

        coach_table = self._find_table_in_soup_or_comments(
            soup, ids=("coach-stats", "coaching", "coaches")
        )
        if not coach_table:
            # Fallback: count "NCAA" occurrences in page text
            text = soup.get_text()
            ncaa_count = len(re.findall(r"\bNCAA\b", text))
            if ncaa_count > 0:
                result["coach_tourney_appearances"] = ncaa_count
            return result

        appearances = 0
        championships = 0
        final_fours = 0
        elite_eights = 0
        sweet_sixteens = 0

        for row in coach_table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if not cells:
                continue
            row_data = {c.get("data-stat", ""): c.get_text().strip() for c in cells}
            notes = (
                row_data.get("ncaa_tourney", "")
                or row_data.get("notes", "")
                or row_data.get("conf_postseason", "")
            )
            if not notes:
                notes = " ".join(c.get_text().strip() for c in cells)

            if "NCAA" not in notes:
                continue

            appearances += 1
            if "Champion" in notes and "NCAA Champion" in notes:
                championships += 1
            if "FF" in notes or "Final Four" in notes:
                final_fours += 1
            if "E8" in notes or "Elite Eight" in notes or "Elite 8" in notes:
                elite_eights += 1
            if "S16" in notes or "Sweet 16" in notes or "Sweet Sixteen" in notes:
                sweet_sixteens += 1

        if appearances > 0:
            result["coach_tourney_appearances"] = appearances
            # Build a concise summary for the LLM
            parts = [f"{appearances} NCAA tournament apps"]
            if championships:
                parts.append(f"{championships} Championship{'s' if championships > 1 else ''}")
            if final_fours:
                parts.append(f"{final_fours} Final Four{'s' if final_fours > 1 else ''}")
            if elite_eights:
                parts.append(f"{elite_eights} Elite Eight{'s' if elite_eights > 1 else ''}")
            if sweet_sixteens:
                parts.append(f"{sweet_sixteens} Sweet Sixteen{'s' if sweet_sixteens > 1 else ''}")
            result["coach_tourney_record"] = ", ".join(parts)

        return result

    def _parse_schedule_stats(self, soup: BeautifulSoup) -> dict[str, Any]:
        """Extract season record, conf W-L, neutral W-L, last-10, and conf tourney
        from the schedule page (/{slug}/{year}-schedule.html).

        Schedule rows carry cumulative wins/losses columns, so the last REG row
        gives the final regular-season record. Conf tourney uses game_type=CTOURN.
        """
        schedule_table = soup.find("table", {"id": "schedule"})
        result: dict[str, Any] = {
            "season_wins": None, "season_losses": None,
            "conf_wins": None, "conf_losses": None,
            "neutral_wins": 0, "neutral_losses": 0,
            "last10_wins": None, "last10_losses": None,
            "conf_tourney_wins": 0, "conf_tourney_losses": 0,
        }
        if not schedule_table:
            return result

        conf_wins = conf_losses = 0
        neutral_wins = neutral_losses = 0
        ct_wins = ct_losses = 0
        reg_season_results: list[str] = []
        last_reg_wins = last_reg_losses = None

        for row in schedule_table.find_all("tr"):
            cells = row.find_all("td")
            if not cells:
                continue
            row_data = {c.get("data-stat", ""): c.get_text().strip() for c in cells}
            game_type = row_data.get("game_type", "")
            game_result = row_data.get("game_result", "")
            outcome = "W" if game_result.startswith("W") else "L" if game_result.startswith("L") else None

            if game_type == "REG":
                if outcome:
                    reg_season_results.append(outcome)
                    # Track game_location for neutral site
                    if row_data.get("game_location") == "N":
                        if outcome == "W":
                            neutral_wins += 1
                        else:
                            neutral_losses += 1
                    # Conference game detection via conf_abbr presence on same team's schedule
                    # (schedule page uses game_location; conf flag inferred from conf_abbr match)
                    # Use wins/losses running total for final record
                    if row_data.get("wins"):
                        try:
                            last_reg_wins = int(row_data["wins"])
                            last_reg_losses = int(row_data.get("losses", "0") or 0)
                        except ValueError:
                            pass

            elif game_type == "CTOURN":
                if outcome == "W":
                    ct_wins += 1
                elif outcome == "L":
                    ct_losses += 1

        # Season record comes from the running totals in the last REG row
        if last_reg_wins is not None:
            result["season_wins"] = last_reg_wins
            result["season_losses"] = last_reg_losses

        # Conference record: count games where conf_abbr matches team's own conference.
        # Sports Reference schedule page doesn't have a direct conf_game flag, so we
        # approximate using game_type=REG and non-empty conf_abbr (always conference opponent).
        # Accurate conf W-L comes from iterating all rows a second time with the known conf.
        # For simplicity, count all REG games that have a conf_abbr value.
        conf_abbr_counts: dict[str, int] = {}
        for row in schedule_table.find_all("tr"):
            cells = row.find_all("td")
            if not cells:
                continue
            row_data = {c.get("data-stat", ""): c.get_text().strip() for c in cells}
            if row_data.get("game_type") != "REG":
                continue
            conf = row_data.get("conf_abbr", "").strip()
            if conf:
                conf_abbr_counts[conf] = conf_abbr_counts.get(conf, 0) + 1

        # The most-common conf_abbr is the team's own conference (they play most games against it)
        team_conf = max(conf_abbr_counts, key=conf_abbr_counts.get) if conf_abbr_counts else None

        if team_conf:
            for row in schedule_table.find_all("tr"):
                cells = row.find_all("td")
                if not cells:
                    continue
                row_data = {c.get("data-stat", ""): c.get_text().strip() for c in cells}
                if row_data.get("game_type") != "REG":
                    continue
                if row_data.get("conf_abbr", "").strip() != team_conf:
                    continue
                outcome = row_data.get("game_result", "")
                if outcome.startswith("W"):
                    conf_wins += 1
                elif outcome.startswith("L"):
                    conf_losses += 1

        result["conf_wins"] = conf_wins or None
        result["conf_losses"] = conf_losses or None
        result["neutral_wins"] = neutral_wins
        result["neutral_losses"] = neutral_losses
        result["conf_tourney_wins"] = ct_wins
        result["conf_tourney_losses"] = ct_losses

        if reg_season_results:
            last10 = reg_season_results[-10:]
            result["last10_wins"] = last10.count("W")
            result["last10_losses"] = last10.count("L")

        return result

    def _get_roster_player_info(self, soup: BeautifulSoup) -> dict[str, dict]:
        """Parse roster table → {normalized_name: {class_year, position}}.

        Sports Reference's roster table has per-player class year and more
        granular positions (C, PF, SF, SG, PG) than the per-game stats table
        which only uses F/G. SR sometimes wraps tables in HTML comments.
        """
        class_map = {
            "fr": "Fr", "freshman": "Fr", "rs fr": "Fr",
            "so": "So", "sophomore": "So",
            "jr": "Jr", "junior": "Jr",
            "sr": "Sr", "senior": "Sr",
            "gr": "Gr", "graduate": "Gr",
        }
        roster_table = self._find_table_in_soup_or_comments(soup, ids=("roster",))
        if not roster_table:
            return {}
        result: dict[str, dict] = {}
        for row in roster_table.find_all("tr"):
            cells = row.find_all("td")
            if not cells:
                continue
            row_data = {c.get("data-stat", ""): c.get_text().strip() for c in cells}
            raw_name = row_data.get("player", "").replace("*", "").strip()
            if not raw_name:
                continue
            class_raw = row_data.get("class", "").lower().strip()
            result[_norm_name(raw_name)] = {
                "class_year": class_map.get(class_raw) or (class_raw.capitalize() if class_raw else None),
                "position": row_data.get("pos") or None,
            }
        return result

    def _extract_players(
        self, soup: BeautifulSoup, bracket_id: int, team_name: str,
        roster_info: dict | None = None,
    ) -> list[dict[str, Any]]:
        """Extract top players by minutes from the players_per_game table.

        Cross-references the roster table for class year and granular position
        (SR's per-game table only uses F/G; roster has C, PF, SF, etc.).
        """
        table = self._find_table_in_soup_or_comments(soup, ("players_per_game",))
        if not table:
            return []

        if roster_info is None:
            roster_info = self._get_roster_player_info(soup)

        players: list[dict[str, Any]] = []
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if not cells:
                continue
            row_data = {c.get("data-stat", ""): c.get_text().strip() for c in cells}
            name = row_data.get("name_display", "").strip()
            if not name or name.lower() in ("", "team", "team totals"):
                continue
            mpg = _safe_float(row_data.get("mp_per_g", ""))
            # Prefer roster table for class year and granular position
            ri = roster_info.get(_norm_name(name), {})
            position = ri.get("position") or row_data.get("pos") or None
            class_yr = ri.get("class_year") or None
            players.append({
                "bracket_id": bracket_id,
                "team_name": team_name,
                "player_name": name,
                "position": position,
                "class_year": class_yr,
                "ppg": _safe_float(row_data.get("pts_per_g", "")),
                "rpg": _safe_float(row_data.get("trb_per_g", "")),
                "apg": _safe_float(row_data.get("ast_per_g", "")),
                "mpg": mpg,
            })

        # Sort by minutes desc, return top 5
        players.sort(key=lambda p: p.get("mpg") or 0, reverse=True)
        return players[:5]

    @staticmethod
    def _find_table_in_soup_or_comments(
        soup: BeautifulSoup, ids: tuple[str, ...]
    ) -> BeautifulSoup | None:
        """Return the first table matching any of the given IDs, searching both
        visible HTML and SR's HTML comment blocks."""
        # 1. Visible tables
        for tid in ids:
            t = soup.find("table", {"id": tid})
            if t:
                return t
        # 2. SR hides many tables inside <!-- --> comments
        for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
            inner = BeautifulSoup(comment, "lxml")
            for tid in ids:
                t = inner.find("table", {"id": tid})
                if t:
                    return t
        return None

    def _extract_roster(self, soup: BeautifulSoup) -> dict[str, Any]:
        """Count freshmen, seniors, and transfers from roster table."""
        roster_table = self._find_table_in_soup_or_comments(soup, ids=("roster",))
        if not roster_table:
            return {}

        freshmen = seniors = transfers = 0
        for row in roster_table.find_all("tr"):
            cells = row.find_all("td")
            row_data = {c.get("data-stat", ""): c.get_text().strip() for c in cells}
            class_yr = row_data.get("class", "").lower()
            if class_yr in ("fr", "freshman"):
                freshmen += 1
            elif class_yr in ("sr", "senior"):
                seniors += 1
            # Transfer indicator: asterisk in name or separate column
            if "*" in row_data.get("player", ""):
                transfers += 1

        return {
            "freshmen_count": freshmen or None,
            "senior_count": seniors or None,
            "transfer_count": transfers or None,
        }

    async def scrape_conf_tourney_record(
        self, bracket_id: int, team_name: str
    ) -> tuple[int, int]:
        """Deprecated: conf tourney record is now included in scrape_team()."""
        return 0, 0


async def upsert_team_stats(conn: aiosqlite.Connection, stats: dict[str, Any]) -> None:
    """Insert or replace team stats row."""
    await conn.execute(
        """
        INSERT INTO team_stats
            (bracket_id, team_name, season_wins, season_losses,
             conf_wins, conf_losses, conference, sos_rank, net_rank, srs,
             adj_off_eff, adj_def_eff, pace, fg_pct, three_pt_pct,
             conf_tourney_wins, conf_tourney_losses, head_coach,
             coach_tourney_appearances, coach_tourney_record,
             freshmen_count, senior_count, transfer_count,
             ft_rate, ft_pct, tov_pct,
             ppg, opp_ppg, orb_per_g, drb_per_g, ast_per_g,
             stl_per_g, blk_per_g, opp_fg_pct, opp_three_pt_pct,
             opp_tov_per_g, ap_rank,
             neutral_wins, neutral_losses, last10_wins, last10_losses,
             bart_adj_oe, bart_adj_de, barthag, bart_tempo, bart_luck, bart_wab,
             quad1_wins, quad1_losses, quad2_wins, quad2_losses)
        VALUES
            (:bracket_id, :team_name, :season_wins, :season_losses,
             :conf_wins, :conf_losses, :conference, :sos_rank, :net_rank, :srs,
             :adj_off_eff, :adj_def_eff, :pace, :fg_pct, :three_pt_pct,
             :conf_tourney_wins, :conf_tourney_losses, :head_coach,
             :coach_tourney_appearances, :coach_tourney_record,
             :freshmen_count, :senior_count, :transfer_count,
             :ft_rate, :ft_pct, :tov_pct,
             :ppg, :opp_ppg, :orb_per_g, :drb_per_g, :ast_per_g,
             :stl_per_g, :blk_per_g, :opp_fg_pct, :opp_three_pt_pct,
             :opp_tov_per_g, :ap_rank,
             :neutral_wins, :neutral_losses, :last10_wins, :last10_losses,
             :bart_adj_oe, :bart_adj_de, :barthag, :bart_tempo, :bart_luck, :bart_wab,
             :quad1_wins, :quad1_losses, :quad2_wins, :quad2_losses)
        ON CONFLICT(bracket_id, team_name) DO UPDATE SET
            season_wins=excluded.season_wins,
            season_losses=excluded.season_losses,
            conf_wins=excluded.conf_wins,
            conf_losses=excluded.conf_losses,
            conference=excluded.conference,
            sos_rank=excluded.sos_rank,
            net_rank=excluded.net_rank,
            srs=excluded.srs,
            adj_off_eff=excluded.adj_off_eff,
            adj_def_eff=excluded.adj_def_eff,
            pace=excluded.pace,
            fg_pct=excluded.fg_pct,
            three_pt_pct=excluded.three_pt_pct,
            conf_tourney_wins=excluded.conf_tourney_wins,
            conf_tourney_losses=excluded.conf_tourney_losses,
            head_coach=excluded.head_coach,
            coach_tourney_appearances=excluded.coach_tourney_appearances,
            coach_tourney_record=excluded.coach_tourney_record,
            freshmen_count=excluded.freshmen_count,
            senior_count=excluded.senior_count,
            transfer_count=excluded.transfer_count,
            ft_rate=excluded.ft_rate,
            ft_pct=excluded.ft_pct,
            tov_pct=excluded.tov_pct,
            ppg=excluded.ppg,
            opp_ppg=excluded.opp_ppg,
            orb_per_g=excluded.orb_per_g,
            drb_per_g=excluded.drb_per_g,
            ast_per_g=excluded.ast_per_g,
            stl_per_g=excluded.stl_per_g,
            blk_per_g=excluded.blk_per_g,
            opp_fg_pct=excluded.opp_fg_pct,
            opp_three_pt_pct=excluded.opp_three_pt_pct,
            opp_tov_per_g=excluded.opp_tov_per_g,
            ap_rank=excluded.ap_rank,
            neutral_wins=excluded.neutral_wins,
            neutral_losses=excluded.neutral_losses,
            last10_wins=excluded.last10_wins,
            last10_losses=excluded.last10_losses,
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
            scraped_at=datetime('now')
        """,
        {
            "bracket_id": stats.get("bracket_id"),
            "team_name": stats.get("team_name"),
            "season_wins": stats.get("season_wins"),
            "season_losses": stats.get("season_losses"),
            "conf_wins": stats.get("conf_wins"),
            "conf_losses": stats.get("conf_losses"),
            "conference": stats.get("conference"),
            "sos_rank": stats.get("sos_rank"),
            "net_rank": stats.get("net_rank"),
            "srs": stats.get("srs"),
            "adj_off_eff": stats.get("adj_off_eff"),
            "adj_def_eff": stats.get("adj_def_eff"),
            "pace": stats.get("pace"),
            "fg_pct": stats.get("fg_pct"),
            "three_pt_pct": stats.get("three_pt_pct"),
            "conf_tourney_wins": stats.get("conf_tourney_wins", 0),
            "conf_tourney_losses": stats.get("conf_tourney_losses", 0),
            "head_coach": stats.get("head_coach"),
            "coach_tourney_appearances": stats.get("coach_tourney_appearances"),
            "coach_tourney_record": stats.get("coach_tourney_record"),
            "freshmen_count": stats.get("freshmen_count"),
            "senior_count": stats.get("senior_count"),
            "transfer_count": stats.get("transfer_count"),
            "ft_rate": stats.get("ft_rate"),
            "ft_pct": stats.get("ft_pct"),
            "tov_pct": stats.get("tov_pct"),
            "ppg": stats.get("ppg"),
            "opp_ppg": stats.get("opp_ppg"),
            "orb_per_g": stats.get("orb_per_g"),
            "drb_per_g": stats.get("drb_per_g"),
            "ast_per_g": stats.get("ast_per_g"),
            "stl_per_g": stats.get("stl_per_g"),
            "blk_per_g": stats.get("blk_per_g"),
            "opp_fg_pct": stats.get("opp_fg_pct"),
            "opp_three_pt_pct": stats.get("opp_three_pt_pct"),
            "opp_tov_per_g": stats.get("opp_tov_per_g"),
            "ap_rank": stats.get("ap_rank"),
            "neutral_wins": stats.get("neutral_wins", 0),
            "neutral_losses": stats.get("neutral_losses", 0),
            "last10_wins": stats.get("last10_wins"),
            "last10_losses": stats.get("last10_losses"),
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
        },
    )
    await conn.commit()
