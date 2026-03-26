# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Golden Question
Ask clarifying questions if there are major assumptions being made in the discussion. Don't just assume — clarify.

---

## Commands

```bash
# Install (dev mode)
pip install -e ".[web,dev]"

# Run all unit tests (~30s, no network/API calls)
pytest

# Run a single test
pytest tests/unit/test_pipeline.py::test_analyze_matchup

# Include slow tests (real LLM calls, requires BT_ANTHROPIC_API_KEY)
pytest -m slow

# Lint
ruff check src/

# Start web server (dev mode with reload)
bracket-team serve --reload
```

---

## Architecture

### High-Level Flow

```
bracket-team gather-data   →  scrapers populate SQLite cache tables
bracket-team run-bracket   →  pipeline reads cache → LLM agents → predictions
bracket-team serve         →  FastAPI + single-file HTML frontend
```

### Three-Layer Structure

- **`api/routes/`** — FastAPI endpoints + Pydantic request/response models. Analysis is triggered as a `BackgroundTask` (non-blocking 202 response).
- **`service/`** — Orchestration and business logic. The pipeline layer never touches the DB directly; it always goes through repositories.
- **`db/repositories/`** — All async SQLite I/O via `aiosqlite`. All writes `await conn.commit()` immediately.

### Pipeline Phases (per matchup)

1. **Research**: 4 analyst agents run in parallel (`asyncio.TaskGroup`). Each reads its specialty data from the SQLite cache and returns a structured `AnalystReport` (pick, score -5..+5, relevance, thesis).
2. **Discussion**: All analysts issue challenges simultaneously, then targeted analysts rebut simultaneously. Individual failures are logged and skipped; the pipeline continues.
3. **Decision**: Manager agent synthesizes all reports + discussion. Model is selected based on disagreement:
   - `|weighted_score| ≤ 1.5` OR analysts disagree → Opus (configured via `BT_MANAGER_MODEL_CONTESTED`)
   - `|score| < 3.5` → Sonnet
   - `|score| ≥ 3.5` → Haiku (cost-optimal for consensus picks)

### LLM Abstraction

`AgentLLM` is a Protocol (`agents/llm.py`). Active backends: `ClaudeBackend` (default), `GeminiBackend` (optional dep), `StubBackend` (testing — triggered by `anthropic_api_key="stub"` or `--stub` CLI flag). All backends share:
- Retry logic with jitter on rate-limit/5xx errors
- Global API concurrency cap via `asyncio.Semaphore(max_concurrent_api_calls)`
- Structured output via Claude tool_use / Gemini JSON mode

### Scraper / Cache Layer

`scraper/coordinator.py` orchestrates four sources into SQLite cache tables. By default it skips teams already cached unless `force=True`. Rate-limited with a token-bucket (`scraper/rate_limiter.py`): Sports Reference at 0.33 req/s, BartTorvik at 1 req/s.

| Source | Cache Table | Key Fields |
|--------|-------------|------------|
| Sports Reference | `team_stats` | Record, efficiency, pace, shooting %, SOS, coach |
| BartTorvik | `team_stats` (merged) | AdjOE/DE, Barthag, Tempo, Luck, WAB, Quad records |
| ESPN | `team_player_stats` | PPG/RPG/APG, injury flags |
| The Odds API | `team_odds` | Spread, ML, O/U, implied win % |

### Error Handling Hierarchy

- **`FatalLLMError`** (auth/billing/quota): Immediately bubbles through `ExceptionGroup` unwrapping and stops the entire run.
- **`LLMRetryExhaustedError`**: Also propagates upward.
- **Individual analyst failure**: Logged and skipped; the other 3 analysts continue.
- **All 4 analysts fail**: Raises `PipelineError`, matchup is aborted.

### Configuration

All settings use the `BT_` env prefix or a `.env` file. Key settings: `BT_ANTHROPIC_API_KEY`, `BT_ODDS_API_KEY`, model overrides (`BT_ANALYST_MODEL`, `BT_MANAGER_MODEL_*`), concurrency (`max_concurrent_matchups=8`, `max_concurrent_api_calls=4`). Loaded as a cached singleton via `get_config()`.

### Tournament Schedule

Hardcoded in `scraper/tournament_schedule.py`. Add new years there as schedules are announced.
