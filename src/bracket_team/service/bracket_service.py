"""Bracket import and query service."""

from __future__ import annotations

from dataclasses import dataclass

from bracket_team.db.connection import get_connection
from bracket_team.db.models import Bracket, Matchup
from bracket_team.db.repositories.bracket_repo import BracketRepository
from bracket_team.db.repositories.matchup_repo import MatchupRepository
from bracket_team.exceptions import BracketImportError


@dataclass
class MatchupInput:
    round_num: int
    region: str
    favorite_name: str
    favorite_seed: int
    underdog_name: str
    underdog_seed: int

    def __post_init__(self) -> None:
        if not 1 <= self.round_num <= 6:
            raise BracketImportError(f"round_num must be 1-6, got {self.round_num}")
        if not 1 <= self.favorite_seed <= 16:
            raise BracketImportError(f"favorite_seed must be 1-16, got {self.favorite_seed}")
        if not 1 <= self.underdog_seed <= 16:
            raise BracketImportError(f"underdog_seed must be 1-16, got {self.underdog_seed}")
        if self.favorite_seed >= self.underdog_seed:
            raise BracketImportError(
                f"favorite_seed ({self.favorite_seed}) must be lower than "
                f"underdog_seed ({self.underdog_seed})"
            )


async def import_bracket(
    year: int, tournament_name: str, matchups: list[MatchupInput]
) -> tuple[Bracket, list[Matchup]]:
    """Persist a new bracket and its matchups. Returns (bracket, matchups)."""
    if not matchups:
        raise BracketImportError("Cannot import bracket with no matchups")

    async with get_connection() as conn:
        bracket = await BracketRepository(conn).create(year, tournament_name)
        repo = MatchupRepository(conn)
        created = []
        for m in matchups:
            matchup = await repo.create(
                bracket_id=bracket.id,
                round_num=m.round_num,
                region=m.region,
                favorite_name=m.favorite_name,
                favorite_seed=m.favorite_seed,
                underdog_name=m.underdog_name,
                underdog_seed=m.underdog_seed,
            )
            created.append(matchup)

    return bracket, created


async def get_bracket(bracket_id: int) -> Bracket | None:
    async with get_connection() as conn:
        return await BracketRepository(conn).get(bracket_id)


async def get_matchups(bracket_id: int, round_num: int | None = None, run_id: int | None = None) -> list[Matchup]:
    async with get_connection() as conn:
        return await MatchupRepository(conn).list_by_bracket(bracket_id, round_num, run_id)


async def list_brackets() -> list[Bracket]:
    async with get_connection() as conn:
        return await BracketRepository(conn).list_all()
