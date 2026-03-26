"""Repository for matchups table."""

from __future__ import annotations

import aiosqlite

from bracket_team.db.models import Matchup


class MatchupRepository:
    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn

    async def create(
        self,
        bracket_id: int,
        round_num: int,
        region: str,
        favorite_name: str,
        favorite_seed: int,
        underdog_name: str,
        underdog_seed: int,
        run_id: int | None = None,
    ) -> Matchup:
        cursor = await self._conn.execute(
            """INSERT INTO matchups
               (bracket_id, run_id, round_num, region, favorite_name, favorite_seed,
                underdog_name, underdog_seed)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING *""",
            (bracket_id, run_id, round_num, region, favorite_name, favorite_seed,
             underdog_name, underdog_seed),
        )
        row = await cursor.fetchone()
        await self._conn.commit()
        return Matchup(**dict(row))

    async def get(self, matchup_id: int) -> Matchup | None:
        cursor = await self._conn.execute(
            "SELECT * FROM matchups WHERE id = ?", (matchup_id,)
        )
        row = await cursor.fetchone()
        return Matchup(**dict(row)) if row else None

    async def find_by_team_in_round(
        self, bracket_id: int, round_num: int, team_name: str, run_id: int | None = None
    ) -> Matchup | None:
        if run_id is not None and round_num > 1:
            cursor = await self._conn.execute(
                """SELECT * FROM matchups WHERE bracket_id = ? AND round_num = ? AND run_id = ?
                   AND (favorite_name = ? OR underdog_name = ?)""",
                (bracket_id, round_num, run_id, team_name, team_name),
            )
        else:
            cursor = await self._conn.execute(
                """SELECT * FROM matchups WHERE bracket_id = ? AND round_num = ?
                   AND (favorite_name = ? OR underdog_name = ?)""",
                (bracket_id, round_num, team_name, team_name),
            )
        row = await cursor.fetchone()
        return Matchup(**dict(row)) if row else None

    async def replace_team(
        self, matchup_id: int, old_name: str, new_name: str, new_seed: int
    ) -> Matchup:
        """Replace one team in a matchup, re-assigning favorite/underdog by seed."""
        matchup = await self.get(matchup_id)
        if matchup is None:
            raise ValueError(f"Matchup {matchup_id} not found")
        if matchup.favorite_name == old_name:
            other_name, other_seed = matchup.underdog_name, matchup.underdog_seed
        else:
            other_name, other_seed = matchup.favorite_name, matchup.favorite_seed
        if new_seed <= other_seed:
            fav_name, fav_seed, dog_name, dog_seed = new_name, new_seed, other_name, other_seed
        else:
            fav_name, fav_seed, dog_name, dog_seed = other_name, other_seed, new_name, new_seed
        cursor = await self._conn.execute(
            """UPDATE matchups SET favorite_name=?, favorite_seed=?, underdog_name=?, underdog_seed=?
               WHERE id=? RETURNING *""",
            (fav_name, fav_seed, dog_name, dog_seed, matchup_id),
        )
        row = await cursor.fetchone()
        await self._conn.commit()
        return Matchup(**dict(row))

    async def list_by_bracket(
        self, bracket_id: int, round_num: int | None = None, run_id: int | None = None
    ) -> list[Matchup]:
        if run_id is not None:
            # Run-specific view: shared Round 1 + this run's Round 2+ matchups
            if round_num is not None:
                if round_num == 1:
                    cursor = await self._conn.execute(
                        "SELECT * FROM matchups WHERE bracket_id = ? AND round_num = 1 ORDER BY id",
                        (bracket_id,),
                    )
                else:
                    cursor = await self._conn.execute(
                        "SELECT * FROM matchups WHERE bracket_id = ? AND round_num = ? AND run_id = ? ORDER BY id",
                        (bracket_id, round_num, run_id),
                    )
            else:
                cursor = await self._conn.execute(
                    """SELECT * FROM matchups WHERE bracket_id = ?
                       AND (round_num = 1 OR run_id = ?)
                       ORDER BY round_num, id""",
                    (bracket_id, run_id),
                )
        else:
            if round_num is not None:
                cursor = await self._conn.execute(
                    "SELECT * FROM matchups WHERE bracket_id = ? AND round_num = ? ORDER BY id",
                    (bracket_id, round_num),
                )
            else:
                cursor = await self._conn.execute(
                    "SELECT * FROM matchups WHERE bracket_id = ? ORDER BY round_num, id",
                    (bracket_id,),
                )
        rows = await cursor.fetchall()
        return [Matchup(**dict(r)) for r in rows]
