"""
Integration tests for the FastAPI layer.
Uses httpx.AsyncClient with the real app and in-memory SQLite (via in_memory_db fixture).
No LLM calls — stub mode only.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from bracket_team.api.app import create_app

_BRACKET_PAYLOAD = {
    "year": 2025,
    "tournament_name": "Test Tournament",
    "matchups": [
        {
            "round_num": 1, "region": "East",
            "favorite_name": "Duke", "favorite_seed": 2,
            "underdog_name": "UNC", "underdog_seed": 7,
        },
        {
            "round_num": 1, "region": "East",
            "favorite_name": "Kansas", "favorite_seed": 1,
            "underdog_name": "Howard", "underdog_seed": 16,
        },
    ],
}


@pytest.fixture
async def client(in_memory_db):
    # Patch _run_pipeline so background tasks complete instantly without LLM calls.
    with patch("bracket_team.api.routes.runs._run_pipeline", new=AsyncMock()):
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as c:
            yield c


@pytest.fixture
async def bracket(client):
    r = await client.post("/api/brackets", json=_BRACKET_PAYLOAD)
    return r.json()["bracket"]


# ── Brackets ──────────────────────────────────────────────────────────────────

async def test_import_bracket_returns_201(client):
    r = await client.post("/api/brackets", json=_BRACKET_PAYLOAD)
    assert r.status_code == 201
    body = r.json()
    assert body["bracket"]["tournament_name"] == "Test Tournament"
    assert body["matchup_count"] == 2


async def test_import_bracket_invalid_seeds_returns_422(client):
    bad = dict(_BRACKET_PAYLOAD)
    bad["matchups"] = [{
        "round_num": 1, "region": "East",
        "favorite_name": "A", "favorite_seed": 7,   # favorite_seed > underdog_seed
        "underdog_name": "B", "underdog_seed": 2,
    }]
    r = await client.post("/api/brackets", json=bad)
    assert r.status_code == 422


async def test_list_brackets(client, bracket):
    r = await client.get("/api/brackets")
    assert r.status_code == 200
    ids = [b["id"] for b in r.json()]
    assert bracket["id"] in ids


async def test_get_bracket(client, bracket):
    r = await client.get(f"/api/brackets/{bracket['id']}")
    assert r.status_code == 200
    assert r.json()["id"] == bracket["id"]


async def test_get_bracket_not_found(client):
    r = await client.get("/api/brackets/99999")
    assert r.status_code == 404


async def test_get_matchups(client, bracket):
    r = await client.get(f"/api/brackets/{bracket['id']}/matchups")
    assert r.status_code == 200
    assert len(r.json()) == 2


async def test_get_matchups_filtered_by_round(client, bracket):
    r = await client.get(f"/api/brackets/{bracket['id']}/matchups?round_num=1")
    assert r.status_code == 200
    assert all(m["round_num"] == 1 for m in r.json())


async def test_delete_bracket(client, bracket):
    r = await client.delete(f"/api/brackets/{bracket['id']}")
    assert r.status_code == 204
    # Confirm it's gone
    r2 = await client.get(f"/api/brackets/{bracket['id']}")
    assert r2.status_code == 404


async def test_delete_bracket_not_found(client):
    r = await client.delete("/api/brackets/99999")
    assert r.status_code == 404


# ── Runs ──────────────────────────────────────────────────────────────────────

async def test_create_run(client, bracket):
    r = await client.post("/api/runs", json={"bracket_id": bracket["id"], "name": "Test Run"})
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "Test Run"
    assert body["status"] == "pending"


async def test_list_runs(client, bracket):
    await client.post("/api/runs", json={"bracket_id": bracket["id"], "name": "Run A"})
    await client.post("/api/runs", json={"bracket_id": bracket["id"], "name": "Run B"})
    r = await client.get(f"/api/runs?bracket_id={bracket['id']}")
    assert r.status_code == 200
    assert len(r.json()) == 2


async def test_get_run(client, bracket):
    run = (await client.post("/api/runs", json={"bracket_id": bracket["id"], "name": "R"})).json()
    r = await client.get(f"/api/runs/{run['id']}")
    assert r.status_code == 200
    body = r.json()
    assert "run" in body
    assert "predictions" in body
    assert "total_cost_usd" in body


async def test_get_run_not_found(client):
    r = await client.get("/api/runs/99999")
    assert r.status_code == 404


async def test_analyze_returns_202(client, bracket):
    run = (await client.post("/api/runs", json={"bracket_id": bracket["id"], "name": "R"})).json()
    r = await client.post(f"/api/runs/{run['id']}/analyze")
    assert r.status_code == 202
    assert r.json()["status"] == "running"


async def test_analyze_already_running_returns_409(client, bracket):
    run = (await client.post("/api/runs", json={"bracket_id": bracket["id"], "name": "R"})).json()
    await client.post(f"/api/runs/{run['id']}/analyze")
    r = await client.post(f"/api/runs/{run['id']}/analyze")
    assert r.status_code == 409


async def test_get_costs(client, bracket):
    run = (await client.post("/api/runs", json={"bracket_id": bracket["id"], "name": "R"})).json()
    r = await client.get(f"/api/runs/{run['id']}/costs")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


# ── Matchups ──────────────────────────────────────────────────────────────────

async def test_get_matchup_detail(client, bracket):
    run = (await client.post("/api/runs", json={"bracket_id": bracket["id"], "name": "R"})).json()
    matchups = (await client.get(f"/api/brackets/{bracket['id']}/matchups")).json()
    mid = matchups[0]["id"]
    r = await client.get(f"/api/matchups/{mid}?run_id={run['id']}")
    assert r.status_code == 200
    body = r.json()
    assert body["matchup"]["id"] == mid
    assert "reports" in body
    assert "discussion" in body


async def test_get_matchup_not_found(client, bracket):
    run = (await client.post("/api/runs", json={"bracket_id": bracket["id"], "name": "R"})).json()
    r = await client.get(f"/api/matchups/99999?run_id={run['id']}")
    assert r.status_code == 404


# ── Delete cascade ────────────────────────────────────────────────────────────

async def test_delete_bracket_removes_runs(client, bracket):
    """Deleting a bracket should remove its runs too."""
    run = (await client.post("/api/runs", json={"bracket_id": bracket["id"], "name": "R"})).json()
    await client.delete(f"/api/brackets/{bracket['id']}")
    r = await client.get(f"/api/runs/{run['id']}")
    assert r.status_code == 404
