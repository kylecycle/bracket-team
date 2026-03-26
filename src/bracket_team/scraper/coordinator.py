"""GatherCoordinator: orchestrates all scrapers for a bracket.

Runs sports and odds scrapers across all teams in a bracket,
populates the cache tables, and writes seed history. Designed to be
called via the `gather-data` CLI command before a `run-bracket` run.

Usage:
    coord = GatherCoordinator(bracket_id=1, year=2025, odds_api_key=None)
    summary = await coord.run(progress_callback=print)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import aiosqlite  # noqa: F401 – used in type hints for cache-check helpers

from bracket_team.db.connection import get_connection
from bracket_team.db.repositories.matchup_repo import MatchupRepository
from bracket_team.scraper.barttorvik_scraper import BartTorviKScraper, upsert_barttorvik_stats
from bracket_team.scraper.espn_player_scraper import upsert_player_stats
from bracket_team.scraper.odds_scraper import OddsScraper, upsert_team_odds
from bracket_team.scraper.seed_history import write_seed_history
from bracket_team.scraper.sports_scraper import SportsScraper, upsert_team_stats

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], None]


VALID_SOURCES = frozenset(["sports", "barttorvik", "odds", "seed_history"])


class GatherCoordinator:
    """Orchestrates all scrapers for a bracket.

    Scraping order:
    1. Seed history (hardcoded, no network)
    2. Team stats + roster + player stats (Sports Reference, 2 req/team at 0.25 req/sec)
    3. BartTorvik efficiency metrics (1 CSV download, all teams)
    4. Matchup odds (The Odds API, rate-limited by free quota)

    By default each step skips teams/matchups that already have cached data.
    Pass force=True to re-scrape everything.
    Pass sources=frozenset(["odds"]) to run only specific scrapers.
    """

    def __init__(
        self,
        bracket_id: int,
        year: int = 2025,
        odds_api_key: str | None = None,
        force: bool = False,
        sources: frozenset[str] | None = None,
    ) -> None:
        self._bracket_id = bracket_id
        self._year = year
        self._odds_api_key = odds_api_key
        self._force = force
        self._sources = sources  # None means all sources

    def _should_run(self, source: str) -> bool:
        return self._sources is None or source in self._sources

    async def run(
        self, progress: ProgressCallback | None = None
    ) -> dict[str, Any]:
        """Run all scrapers and return a summary dict."""
        summary: dict[str, Any] = {
            "seed_history_rows": 0,
            "teams_scraped": 0,
            "teams_skipped": 0,
            "barttorvik_scraped": 0,
            "barttorvik_skipped": 0,
            "players_scraped": 0,
            "odds_scraped": 0,
            "odds_skipped": 0,
            "errors": [],
            "force": self._force,
        }

        async with get_connection() as conn:
            # Step 1: Seed history (no network, always runs)
            if self._should_run("seed_history"):
                _progress(progress, "Writing seed matchup history...")
                rows = await write_seed_history(conn)
                summary["seed_history_rows"] = rows
                _progress(progress, f"  ✓ {rows} seed history rows written")

            # Step 2: Collect all unique teams from round-1 matchups
            matchups = await MatchupRepository(conn).list_by_bracket(
                self._bracket_id, round_num=1
            )
            teams: set[str] = set()
            for m in matchups:
                teams.add(m.favorite_name)
                teams.add(m.underdog_name)

            _progress(progress, f"Found {len(teams)} teams to scrape.")

        # Step 3: Sports stats + player stats (Sports Reference, 2 req/team)
        if self._should_run("sports"):
            scraped, skipped, players = await self._scrape_sports(
                list(teams), progress, summary["errors"]
            )
            summary["teams_scraped"] = scraped
            summary["teams_skipped"] = skipped
            summary["players_scraped"] = players

        # Step 3.5: BartTorvik efficiency metrics (1 CSV download)
        if self._should_run("barttorvik"):
            bt_scraped, bt_skipped = await self._scrape_barttorvik(
                list(teams), progress, summary["errors"]
            )
            summary["barttorvik_scraped"] = bt_scraped
            summary["barttorvik_skipped"] = bt_skipped

        # Step 5: Odds (per-matchup, all rounds)
        if self._should_run("odds"):
            odds_scraped, odds_skipped, odds_remaining = await self._scrape_odds(
                progress, summary["errors"]
            )
            summary["odds_scraped"] = odds_scraped
            summary["odds_skipped"] = odds_skipped
            if odds_remaining is not None:
                summary["odds_api_remaining"] = odds_remaining

        _progress(
            progress,
            f"\nGather complete: {summary['teams_scraped']} scraped "
            f"({summary['teams_skipped']} skipped), "
            f"{summary['players_scraped']} player records, "
            f"{summary['barttorvik_scraped']} BartTorvik records "
            f"({summary['barttorvik_skipped']} skipped), "
            f"{summary['odds_scraped']} odds records "
            f"({summary['odds_skipped']} skipped).",
        )
        return summary

    async def _scrape_sports(
        self,
        teams: list[str],
        progress: ProgressCallback | None,
        errors: list[str],
    ) -> tuple[int, int, int]:
        """Scrape Sports Reference for team stats and player stats.
        Returns (teams_scraped, teams_skipped, player_rows_written).
        """
        scraper = SportsScraper(year=self._year)
        teams_scraped = teams_skipped = players_written = 0
        try:
            for i, team_name in enumerate(teams, start=1):
                if not self._force:
                    async with get_connection() as conn:
                        has_stats = await _stats_exist(conn, self._bracket_id, team_name)
                        has_players = await _players_exist(conn, self._bracket_id, team_name)
                    if has_stats and has_players:
                        _progress(
                            progress,
                            f"  [{i}/{len(teams)}] Cached: {team_name} — skipping",
                        )
                        teams_skipped += 1
                        continue
                _progress(progress, f"  [{i}/{len(teams)}] Sports stats: {team_name}")
                try:
                    stats = await scraper.scrape_team(self._bracket_id, team_name)
                    if stats:
                        players = stats.pop("_players", [])
                        async with get_connection() as conn:
                            await upsert_team_stats(conn, stats)
                            for p in players:
                                await upsert_player_stats(conn, p)
                        teams_scraped += 1
                        players_written += len(players)
                    else:
                        errors.append(f"No stats found for {team_name}")
                except Exception as exc:
                    logger.warning("Sports scrape error for %r: %s", team_name, exc)
                    errors.append(f"Sports error for {team_name}: {exc}")
        finally:
            await scraper.close()
        return teams_scraped, teams_skipped, players_written

    async def _scrape_barttorvik(
        self,
        teams: list[str],
        progress: ProgressCallback | None,
        errors: list[str],
    ) -> tuple[int, int]:
        """Returns (scraped, skipped)."""
        # Determine which teams actually need BartTorvik data
        if not self._force:
            teams_needed: list[str] = []
            async with get_connection() as conn:
                for team_name in teams:
                    if not await _barttorvik_exists(conn, self._bracket_id, team_name):
                        teams_needed.append(team_name)
            if not teams_needed:
                _progress(progress, "  BartTorvik: all teams already cached — skipping")
                return 0, len(teams)
        else:
            teams_needed = list(teams)

        skipped = len(teams) - len(teams_needed)
        scraper = BartTorviKScraper(year=self._year)
        scraped = 0
        try:
            _progress(progress, f"Fetching BartTorvik efficiency data ({len(teams_needed)} teams)...")
            for i, team_name in enumerate(teams_needed, start=1):
                _progress(progress, f"  [{i}/{len(teams_needed)}] BartTorvik: {team_name}")
                try:
                    stats = await scraper.get_team(team_name)
                    if stats:
                        async with get_connection() as conn:
                            await upsert_barttorvik_stats(conn, self._bracket_id, team_name, stats)
                        scraped += 1
                    else:
                        errors.append(f"BartTorvik: no data for {team_name}")
                except Exception as exc:
                    logger.warning("BartTorvik error for %r: %s", team_name, exc)
                    errors.append(f"BartTorvik error for {team_name}: {exc}")
        finally:
            await scraper.close()
        return scraped, skipped

    async def _scrape_odds(
        self,
        progress: ProgressCallback | None,
        errors: list[str],
    ) -> tuple[int, int, int | None]:
        """Returns (scraped, skipped, odds_api_remaining)."""
        scraper = OddsScraper(api_key=self._odds_api_key)
        scraped = skipped = 0
        try:
            async with get_connection() as conn:
                matchups = await MatchupRepository(conn).list_by_bracket(
                    self._bracket_id
                )

            for m in matchups:
                if not self._force:
                    async with get_connection() as conn:
                        if await _odds_have_data(conn, m.id):
                            _progress(
                                progress,
                                f"  Cached: {m.favorite_name} vs {m.underdog_name} odds — skipping",
                            )
                            skipped += 1
                            continue
                _progress(
                    progress,
                    f"  Odds: {m.favorite_name} vs {m.underdog_name}",
                )
                try:
                    record = await scraper.scrape_matchup(
                        bracket_id=self._bracket_id,
                        matchup_id=m.id,
                        favorite_name=m.favorite_name,
                        underdog_name=m.underdog_name,
                    )
                    async with get_connection() as conn:
                        await upsert_team_odds(conn, record)
                    if record.get("spread") is not None:
                        scraped += 1
                except Exception as exc:
                    logger.warning(
                        "Odds scrape error for matchup %d: %s", m.id, exc
                    )
                    errors.append(f"Odds error for matchup {m.id}: {exc}")
        finally:
            await scraper.close()
        return scraped, skipped, scraper.odds_api_remaining


def _progress(callback: ProgressCallback | None, msg: str) -> None:
    if callback:
        callback(msg)
    else:
        logger.info(msg)


# ---------------------------------------------------------------------------
# Cache-check helpers
# ---------------------------------------------------------------------------

async def _stats_exist(conn: aiosqlite.Connection, bracket_id: int, team_name: str) -> bool:
    async with conn.execute(
        "SELECT COUNT(*) FROM team_stats WHERE bracket_id=? AND team_name=?",
        (bracket_id, team_name),
    ) as cur:
        return (await cur.fetchone())[0] > 0


async def _players_exist(conn: aiosqlite.Connection, bracket_id: int, team_name: str) -> bool:
    async with conn.execute(
        "SELECT COUNT(*) FROM team_player_stats WHERE bracket_id=? AND team_name=?",
        (bracket_id, team_name),
    ) as cur:
        return (await cur.fetchone())[0] > 0


async def _barttorvik_exists(conn: aiosqlite.Connection, bracket_id: int, team_name: str) -> bool:
    async with conn.execute(
        "SELECT COUNT(*) FROM team_stats WHERE bracket_id=? AND team_name=? AND barthag IS NOT NULL",
        (bracket_id, team_name),
    ) as cur:
        return (await cur.fetchone())[0] > 0


async def _odds_have_data(conn: aiosqlite.Connection, matchup_id: int) -> bool:
    """True only if odds have been scraped with actual data (spread not null)."""
    async with conn.execute(
        "SELECT COUNT(*) FROM team_odds WHERE matchup_id=? AND spread IS NOT NULL",
        (matchup_id,),
    ) as cur:
        return (await cur.fetchone())[0] > 0
