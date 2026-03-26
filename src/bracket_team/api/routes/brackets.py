"""Bracket routes: import and query brackets."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from bracket_team.api.app import _require_api_key
from bracket_team.db.connection import get_connection
from bracket_team.db.models import Bracket, Matchup
from bracket_team.db.repositories.bracket_repo import BracketRepository
from bracket_team.exceptions import BracketImportError
from bracket_team.service.bracket_service import (
    MatchupInput,
    get_bracket,
    get_matchups,
    import_bracket,
    list_brackets,
)

router = APIRouter(prefix="/api/brackets", dependencies=[Depends(_require_api_key)])


class MatchupInputSchema(BaseModel):
    round_num: int
    region: str
    favorite_name: str
    favorite_seed: int
    underdog_name: str
    underdog_seed: int


class ImportBracketRequest(BaseModel):
    year: int
    tournament_name: str
    matchups: list[MatchupInputSchema]


class ImportBracketResponse(BaseModel):
    bracket: Bracket
    matchup_count: int


@router.post("", response_model=ImportBracketResponse, status_code=201)
async def api_import_bracket(body: ImportBracketRequest) -> ImportBracketResponse:
    try:
        matchups = [
            MatchupInput(
                round_num=m.round_num,
                region=m.region,
                favorite_name=m.favorite_name,
                favorite_seed=m.favorite_seed,
                underdog_name=m.underdog_name,
                underdog_seed=m.underdog_seed,
            )
            for m in body.matchups
        ]
        bracket, created = await import_bracket(body.year, body.tournament_name, matchups)
    except BracketImportError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ImportBracketResponse(bracket=bracket, matchup_count=len(created))


@router.get("", response_model=list[Bracket])
async def api_list_brackets() -> list[Bracket]:
    return await list_brackets()


@router.get("/{bracket_id}", response_model=Bracket)
async def api_get_bracket(bracket_id: int) -> Bracket:
    bracket = await get_bracket(bracket_id)
    if bracket is None:
        raise HTTPException(status_code=404, detail=f"Bracket {bracket_id} not found")
    return bracket


@router.delete("/{bracket_id}", status_code=204)
async def api_delete_bracket(bracket_id: int) -> Response:
    async with get_connection() as conn:
        deleted = await BracketRepository(conn).delete(bracket_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Bracket {bracket_id} not found")
    return Response(status_code=204)


@router.get("/{bracket_id}/matchups", response_model=list[Matchup])
async def api_get_matchups(bracket_id: int, run_id: int | None = None, round_num: int | None = None) -> list[Matchup]:
    bracket = await get_bracket(bracket_id)
    if bracket is None:
        raise HTTPException(status_code=404, detail=f"Bracket {bracket_id} not found")
    return await get_matchups(bracket_id, round_num, run_id)
