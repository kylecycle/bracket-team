# Software Architecture: Multi-Agent Bracket Analysis Team

## Context

A multi-agent LLM system that predicts NCAA men's basketball tournament brackets. Four analyst agents independently research each matchup with pre-scraped data, debate via a structured discussion phase, then a manager agent synthesizes a final prediction. The system supports both CLI and web clients through a shared service layer, with all outputs persisted as structured data.

---

## 1. Tech Stack

| Layer | Choice | Rationale |
|-------|--------|-----------|
| Language | Python 3.12 | Async-native, strong LLM ecosystem |
| Package manager | `uv` | Fast resolver, lockfile support |
| LLM SDK | `anthropic` (official) | Claude-only; native async via `AsyncAnthropic` |
| Database | SQLite via `aiosqlite` | Zero-config, single-file, sufficient for local use |
| Async runtime | `asyncio` + `asyncio.TaskGroup` | Structured concurrency for parallel analyst calls |
| Web framework | FastAPI | Async-native, Pydantic integration, auto OpenAPI docs |
| CLI framework | `click` | Simple, composable |
| Validation / serialization | Pydantic v2 | LLM structured output, API schemas, config — one library |
| HTTP client | `httpx` (async) | All scraper and LLM HTTP requests |
| HTML parsing | `beautifulsoup4` + `lxml` | Sports Reference HTML scraping |
| Testing | `pytest` + `pytest-asyncio` | 110 unit tests, no network required |
| Linting | Ruff | Fast, all-in-one |

### Explicitly NOT using

- **LangChain / LlamaIndex** — Too much abstraction for well-defined LLM call patterns.
- **Celery / task queues** — `asyncio.TaskGroup` handles all needed parallelism.
- **SQLAlchemy ORM** — Flat tables with simple queries. Repository pattern with raw parameterized SQL is more transparent.
- **Vector DB** — 64 teams, structured stats; all lookups are `WHERE team_name = ?`.

---

## 2. Project Structure

```
src/bracket_team/
├── agents/
│   ├── llm.py                   # AgentLLM protocol, ClaudeBackend, StubBackend
│   ├── analysts.py              # 4 analyst agents + context injection
│   ├── manager.py               # Manager agent + challenge/rebuttal runners
│   ├── schemas.py               # Pydantic models for structured LLM output
│   └── prompts/
│       ├── sports_analyst.txt
│       ├── odds_analyst.txt
│       ├── historical_analyst.txt
│       ├── injury_analyst.txt
│       ├── manager_decision.txt
│       └── discussion_challenge.txt
│
├── api/
│   ├── app.py                   # FastAPI app factory, API key middleware
│   └── routes/
│       ├── brackets.py          # Import/query brackets and matchups
│       ├── runs.py              # Create runs, trigger analysis, poll status
│       ├── matchups.py          # Matchup queries
│       └── data.py              # Gather trigger, per-source update, player injury flag
│
├── cli/
│   └── commands.py              # Click commands: serve, import-bracket, gather-data, run-bracket, etc.
│
├── db/
│   ├── connection.py            # aiosqlite factory + init_db() with ALTER TABLE migrations
│   ├── models.py                # Pydantic models for DB rows
│   ├── schema.sql               # CREATE TABLE IF NOT EXISTS for all tables
│   └── repositories/
│       ├── bracket_repo.py
│       ├── matchup_repo.py
│       ├── run_repo.py
│       ├── report_repo.py
│       ├── discussion_repo.py
│       ├── prediction_repo.py
│       └── cost_repo.py
│
├── scraper/
│   ├── coordinator.py           # GatherCoordinator: orchestrates all scrapers
│   ├── rate_limiter.py          # Async token-bucket rate limiter
│   ├── sports_scraper.py        # Sports Reference CBB: stats, roster, coach, conf tourney
│   ├── barttorvik_scraper.py    # BartTorvik T-Rank: AdjOE/DE, Barthag, WAB, Quad records
│   ├── espn_player_scraper.py   # ESPN Statistics API: top-5 players per team
│   ├── odds_scraper.py          # The Odds API: lines for all bracket rounds
│   ├── seed_history.py          # Hardcoded 1985–2024 seed matchup data
│   ├── tournament_schedule.py   # 2026 tournament dates, venues, rest-day calculations
│   └── team_slug_map.py         # Tournament name → Sports Reference URL slug
│
└── service/
    ├── bracket_service.py       # Import/query brackets
    ├── run_service.py           # Create/manage prediction runs
    ├── pipeline.py              # MatchupPipeline: research → discussion → decision
    └── scoring.py               # Weighted score, confidence, model selection (pure functions)

static/
└── index.html                   # Single-file web UI

data/
└── 2026_bracket.json            # Sample bracket (32 round-1 matchups)

tests/
└── unit/
    ├── test_context_injection.py
    ├── test_rate_limiter.py
    ├── test_seed_history.py
    └── test_sports_scraper.py
```

---

## 3. Core Abstractions

### 3.1 AgentLLM — LLM Abstraction

```python
@dataclass(frozen=True)
class AgentConfig:
    role: str                    # "sports_analyst", "manager", etc.
    model: str                   # "claude-sonnet-4-5"
    system_prompt: str
    temperature: float = 0.7
    max_tokens: int = 2048

@dataclass(frozen=True)
class LLMResponse:
    content: str
    input_tokens: int
    output_tokens: int
    model: str
    cost_usd: float

class AgentLLM(Protocol):
    async def generate(
        self,
        config: AgentConfig,
        user_message: str,
        response_schema: type[BaseModel] | None = None,
    ) -> LLMResponse: ...
```

`ClaudeBackend` implements this via `anthropic.AsyncAnthropic`. `StubBackend` returns deterministic fixture responses for testing without API calls (activated when `BT_ANTHROPIC_API_KEY=stub`).

### 3.2 Structured Output Schemas

```python
class AnalystReport(BaseModel):
    pick: Literal["favorite", "underdog"]
    score: int          # -5 to +5
    relevance: Literal["low", "medium", "high"]
    thesis: str

class DiscussionChallenge(BaseModel):
    steelman_against_own_pick: str
    target_analyst: str
    challenge: str

class DiscussionRebuttal(BaseModel):
    rebuttal: str

class ManagerPrediction(BaseModel):
    predicted_winner: str
    outcome_type: Literal["expected", "upset"]
    weighted_score: float
    synthesis: str
```

All structured outputs use Anthropic's tool-use mechanism — no fragile output parsing.

### 3.3 Pipeline Orchestrator

```python
class MatchupPipeline:
    def __init__(self, llm, config, user_preferences=None): ...

    async def run(self, run_id: int, matchup: Matchup) -> Prediction:
        reports = await self._research_phase(run_id, matchup)        # 4 parallel LLM calls
        challenges, rebuttals = await self._discussion_phase(...)    # 2 batches of 4
        prediction = await self._decision_phase(...)                 # 1 manager call
        return prediction
```

`user_preferences` (free-form text from the user) flows through to the manager's decision message.

### 3.4 Scoring — Pure Functions

```python
def compute_weighted_score(
    reports: list[tuple[str, AnalystReport]],
    base_weights: dict[str, float],        # {"sports_analyst": 0.4, ...}
    relevance_multipliers: dict[str, float],  # {"high": 1.5, "medium": 1.0, "low": 0.5}
) -> float: ...

def derive_confidence(weighted_score: float, high_threshold, low_threshold) -> str: ...

def select_manager_model(weighted_score, has_contested_challenges, ...) -> str: ...
```

---

## 4. Data Flow

```
User clicks "New Run"
    ↓
POST /api/runs  { bracket_id, name, user_preferences }
POST /api/runs/{id}/analyze
    ↓
Background: _run_pipeline(run_id)
    ↓
analyze_bracket(run_id, llm, config)
    For each round 1–6:
        For each matchup (concurrent, capped by config.max_concurrent_matchups):
            MatchupPipeline.run(run_id, matchup)
                ├─ _research_phase()    → 4 parallel analyst LLM calls
                │     Each analyst fetches relevant DB data first
                ├─ _discussion_phase()  → 4 challenges + 4 rebuttals (parallel)
                └─ _decision_phase()
                      compute_weighted_score()
                      select_manager_model()
                      run_manager(... user_preferences=...)
                      → Prediction written to DB
        Create next round's matchups from predicted winners
```

### Context Injection

`analysts.py` fetches pre-scraped data before each LLM call:

```python
async def run_analyst(llm, role, model, matchup, ...):
    context_block = await _fetch_context(matchup, role)  # silent fail → ""
    response = await llm.generate(config, _matchup_message(matchup, context_block), AnalystReport)
```

Each role sees only its relevant data — sports analyst never sees odds, injury analyst never sees betting lines.

---

## 5. API Routes

```
POST   /api/brackets                              import_bracket
GET    /api/brackets                              list_brackets
GET    /api/brackets/{id}                         get_bracket
DELETE /api/brackets/{id}                         delete_bracket
GET    /api/brackets/{id}/matchups                get_matchups

POST   /api/runs                                  create_run (accepts user_preferences)
GET    /api/runs?bracket_id=N                     list_runs
GET    /api/runs/{id}                             get_run (includes predictions + cost)
POST   /api/runs/{id}/analyze                     trigger pipeline
DELETE /api/runs/{id}                             delete_run
GET    /api/runs/{id}/costs                       cost breakdown

GET    /api/data/summary?bracket_id=N             row counts per table
GET    /api/data/team_stats?bracket_id=N          team stats
GET    /api/data/team_player_stats?bracket_id=N   player stats with injury flags
GET    /api/data/team_odds?bracket_id=N           betting lines
GET    /api/data/seed_history                     seed matchup history
GET    /api/data/gather-status?bracket_id=N       gather job status
POST   /api/data/gather?bracket_id=N[&source=X][&force=true]  trigger gather
PATCH  /api/data/player_injury                    set injured flag + note on a player
DELETE /api/data/clear?bracket_id=N               clear all scraped data
```

---

## 6. Concurrency Model

### Within a single matchup (13 LLM calls total)

| Phase | Calls | Concurrency |
|-------|-------|-------------|
| Research | 4 analyst reports | All 4 parallel via `TaskGroup` |
| Discussion — challenges | 4 challenges | All 4 parallel |
| Discussion — rebuttals | 4 rebuttals | All 4 parallel |
| Decision | 1 manager call | Sequential |

### Across matchups (63 matchups per full bracket)

- **Within a round**: all matchups are independent — run concurrently via `asyncio.Semaphore(config.max_concurrent_matchups)`
- **Between rounds**: sequential — next round needs predicted winners from the current round

### Scraper rate limits

| Scraper | Rate | Notes |
|---------|------|-------|
| Sports Reference | 0.33 req/sec | Token-bucket via `RateLimiter`; 429 triggers 65s backoff |
| ESPN | 2 req/sec | Team stats + roster per team |
| BartTorvik | 1 CSV download | All teams in one request |
| Odds API | 1 event-list fetch + N matchup lookups | Event list cached per gather session |

---

## 7. Database Schema

Tables (all in `schema.sql`, applied idempotently via `init_db()`):

| Table | Purpose |
|-------|---------|
| `brackets` | Tournament brackets (year, name) |
| `matchups` | Per-round matchups (favorite + underdog, seed, region) |
| `runs` | Analysis runs (name, weights, user_preferences, status) |
| `analyst_reports` | Per-analyst picks for each matchup in a run |
| `discussion_messages` | Challenges and rebuttals |
| `predictions` | Final manager picks with synthesis and cost |
| `llm_costs` | Token usage and USD cost per LLM call |
| `team_stats` | Scraped team stats + BartTorvik metrics (per bracket) |
| `team_player_stats` | Top-5 players per team with manual injury flags |
| `team_odds` | Betting lines per matchup |
| `seed_matchup_history` | 1985–2024 seed upset rates (global, not per-bracket) |

New columns are added via `ALTER TABLE` migrations in `connection.py::init_db()`. Each migration runs individually so "duplicate column" errors are silently ignored on existing databases.

---

## 8. Error Handling

### LLM Retry Strategy (in `ClaudeBackend`)
- Retries on: `RateLimitError`, 5xx errors, `ValidationError`
- Non-retryable: 4xx errors (bad request, auth)
- Max retries configurable via `BT_LLM_MAX_RETRIES` (default 3)

### Pipeline Resilience
- If one analyst fails: continue with remaining reports (manager notes absent analyst)
- If manager call fails: matchup marked `status=error`, surfaced in UI
- Gather errors are collected per-source and returned in the gather status summary

---

## 9. Configuration

```python
class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BT_", env_file=".env")

    anthropic_api_key: SecretStr = "dummy-key"
    odds_api_key: SecretStr | None = None
    api_key: SecretStr | None = None           # protects API endpoints if set
    database_url: str = "bracket_team.db"

    analyst_model: str = "claude-sonnet-4-5"
    manager_model_contested: str = "claude-opus-4-5"
    manager_model_moderate: str = "claude-sonnet-4-5"
    manager_model_consensus: str = "claude-haiku-4-5-20251001"

    llm_temperature: float = 0.7
    llm_max_retries: int = 3
    llm_max_tokens: int = 2048
    max_concurrent_matchups: int = 4

    default_analyst_weights: dict = {
        "sports_analyst": 0.35,
        "odds_analyst": 0.30,
        "historical_analyst": 0.20,
        "injury_analyst": 0.15,
    }
    relevance_multipliers: dict = {"high": 1.5, "medium": 1.0, "low": 0.5}
```

---

## 10. Cost Estimate

Per full bracket run (63 matchups × 13 LLM calls = 819 calls):
- Consensus picks (Haiku): minimal cost
- Moderate picks (Sonnet): ~$0.01–0.02 per matchup
- Contested picks (Opus): ~$0.05–0.10 per matchup
- **Typical full bracket**: ~$2–5 total

Data gathering: free (no LLM calls).
