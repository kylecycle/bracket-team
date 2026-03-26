# Bracket Team

Multi-agent LLM system for NCAA tournament bracket prediction. Four specialized analyst agents (sports, odds, historical, injury/fatigue) independently analyze each matchup, debate via a challenge/rebuttal discussion phase, and a manager agent synthesizes a final prediction.

## Installation

```bash
# Requires Python 3.12+
# Clone and install (includes web server dependencies)
pip install -e ".[web]"

# For development (adds pytest + ruff)
pip install -e ".[web,dev]"
```

---

## Configuration

All settings are read from environment variables with the `BT_` prefix, or from a `.env` file in the project root. Copy `.env.template` to `.env` and fill in what you need.

### API Keys

| Variable | Required | Description |
|----------|----------|-------------|
| `BT_ANTHROPIC_API_KEY` | Yes (if no Gemini) | Anthropic API key. Get one at [console.anthropic.com](https://console.anthropic.com). |
| `BT_GEMINI_API_KEY` | Yes (if no Anthropic) | Google Gemini API key. Get one at [aistudio.google.com](https://aistudio.google.com). Free tier available. |
| `BT_ODDS_API_KEY` | No | [The Odds API](https://the-odds-api.com) key — enables the odds analyst. Free tier: 500 req/month. Without this, odds data is skipped and the odds analyst has nothing to work with. |
| `BT_API_KEY` | No | If set, all web API requests must include this as `X-API-Key`. Use when exposing the server publicly. |

### LLM Provider

| Variable | Default | Description |
|----------|---------|-------------|
| `BT_LLM_PROVIDER` | `auto` | Which LLM backend to use. `auto` picks Gemini if `BT_GEMINI_API_KEY` is set, otherwise Anthropic. Set to `anthropic`, `gemini`, or `stub` (for testing without API calls) to force a specific backend. |

### Anthropic Model Selection

Used when the provider is `anthropic`. The manager routes each matchup to a model based on how contested the analysts' scores are.

| Variable | Default | Description |
|----------|---------|-------------|
| `BT_ANALYST_MODEL` | `claude-sonnet-4-5` | Model for the 4 analyst agents (sports, odds, historical, injury). Runs in parallel per matchup. |
| `BT_MANAGER_MODEL_CONTESTED` | `claude-opus-4-5` | Model for close calls where analysts disagree or the weighted score is within ±1.5. Use the most capable model here — these are the picks that matter. |
| `BT_MANAGER_MODEL_MODERATE` | `claude-sonnet-4-5` | Model for matchups with a moderate score (±1.5–3.5). |
| `BT_MANAGER_MODEL_CONSENSUS` | `claude-haiku-4-5` | Model for consensus picks where the score is ≥3.5 and analysts agree. Fast and cheap — the outcome is rarely in doubt. |

### Gemini Model Selection

Used when the provider is `gemini`. All four slots default to `gemini-2.5-flash`.

| Variable | Default | Description |
|----------|---------|-------------|
| `BT_GEMINI_ANALYST_MODEL` | `gemini-2.5-flash` | Model for analyst agents. |
| `BT_GEMINI_MANAGER_MODEL_CONTESTED` | `gemini-2.5-flash` | Model for contested matchups. |
| `BT_GEMINI_MANAGER_MODEL_MODERATE` | `gemini-2.5-flash` | Model for moderate matchups. |
| `BT_GEMINI_MANAGER_MODEL_CONSENSUS` | `gemini-2.5-flash` | Model for consensus matchups. |
| `BT_GEMINI_RPM` | `25` | Requests per minute cap for Gemini. Match this to your tier's limit to avoid throttling. Free tier is 15 RPM for 2.5 Flash. |
| `BT_GEMINI_THINKING_BUDGET` | `1024` | Thinking token budget for Gemini 2.5 models. Set to `0` to disable thinking (faster, cheaper). Thinking tokens count against `BT_LLM_MAX_TOKENS`. |
| `BT_GEMINI_REQUEST_DELAY` | `0` | Seconds to wait between Gemini API calls. Useful on free-tier plans to manually throttle below the RPM cap. |

### LLM Behavior

| Variable | Default | Description |
|----------|---------|-------------|
| `BT_LLM_TEMPERATURE` | `0.7` | Sampling temperature. Lower = more deterministic picks; higher = more varied. |
| `BT_LLM_MAX_TOKENS` | `2048` | Max output tokens per LLM call. Reduce to cut costs on large brackets. |
| `BT_LLM_MAX_RETRIES` | `3` | How many times to retry a failed API call (rate limits, 5xx errors) before giving up. |
| `BT_DISCUSSION_MAX_CHARS` | `500` | Max characters from each analyst's challenge/rebuttal that are fed to the manager. Shorter = faster and cheaper manager calls; longer = more context. |

### Concurrency

| Variable | Default | Description |
|----------|---------|-------------|
| `BT_MAX_CONCURRENT_MATCHUPS` | `1` | How many matchups to analyze in parallel. Increase (e.g., `4`) to speed up a full bracket run — each matchup still runs its 4 analysts in parallel internally. Be mindful of API rate limits. |
| `BT_MAX_CONCURRENT_API_CALLS` | `4` | Global cap on in-flight LLM API requests at any time. Acts as a safety valve to prevent burst throttling even when matchup concurrency is high. |

### Database & Logging

| Variable | Default | Description |
|----------|---------|-------------|
| `BT_DATABASE_URL` | `bracket_team.db` | Path to the SQLite database file. Change to store the DB in a specific directory (e.g., `/data/bracket_team.db` in Docker). |
| `BT_LLM_CONVERSATION_LOG` | — | File path to log full LLM prompt/response conversations. Useful for debugging agent reasoning. Leave unset to disable. |

---

## Quick Start

Two ways to use Bracket Team:

- **Web UI** — visual, point-and-click. Best for most users.
- **CLI** — scriptable, runs headlessly. Best for automation or if you prefer the terminal.

### Web UI

#### 1. Start the server

```bash
bracket-team serve
```

Open `http://localhost:8000` in your browser.

#### 2. Import a bracket

Click **Brackets** → **Import Bracket**. Upload a bracket JSON file (see format below) or paste the JSON directly. A sample 2026 bracket is included at `data/2026_bracket.json`.

#### 3. Gather tournament data

Open the bracket → click the **Data** tab → click **Update All**. This pre-scrapes stats, efficiency metrics, player rosters, and odds into a local cache so the analysts have real data to work with. Takes 6–8 minutes.

You can also update individual sources using the per-source Update buttons (Sports, BartTorvik, ESPN, Odds).

#### 4. Flag injuries (optional)

On the **Players** tab, click **OK** next to any player to mark them as out. You'll be prompted for a note (e.g., "ACL tear", "suspended"). The injury analyst will factor this into every matchup involving that team.

> Injury data is not available via API — this step is always manual.

#### 5. Add preferences (optional)

On the bracket detail page, enter anything in the **Your Preferences** box — your alma mater, a gut feeling, or a specific instruction like "always favor underdogs in round 2." The manager agent incorporates this when making each pick.

#### 6. Run the bracket

Click **New Run** on the bracket detail page. The pipeline works through all 63 matchups round by round, propagating predicted winners. You can watch progress in real time; the bracket tree fills in as each matchup completes.

#### 7. View and compare results

Click any run to see the full bracket tree with predictions, confidence ratings, and analyst cost. Toggle **Results** to overlay actual tournament outcomes against your predictions (green = correct, red strikethrough = wrong pick).

Use the **<** / **>** buttons to flip between multiple runs for the same bracket.

### Config Tab

The **⚙ Config** link in the nav bar opens the runtime configuration editor. Changes take effect immediately without restarting the server and are persisted to the database, so they survive restarts. Fields overridden from their defaults are highlighted in amber.

#### Settings tab

Grouped fields covering the same knobs as the `BT_` environment variables, but editable live:

| Group | What you can change |
|-------|---------------------|
| **Models** | Anthropic model for analysts and each manager tier (contested / moderate / consensus) |
| **Gemini Models** | Gemini model per tier, requests-per-minute cap, thinking token budget |
| **LLM Behavior** | Temperature, max output tokens, retry count, Gemini request delay, discussion max chars |
| **Concurrency** | Max concurrent matchups, max concurrent API calls |
| **Analyst Weights** | Relative influence of each analyst (sports / odds / historical / injury). Must sum to 1.0. |
| **Scoring Thresholds** | Score cutoffs that control model tier routing and confidence badge levels |

Each field shows its current value and the compiled default. Click the **×** reset button next to any field to revert it to the `.env`/default value.

> Settings in Config override `.env` values. If you want a permanent change, update `.env` instead — that way it applies on a fresh install too.

#### Prompts tab

Shows the full system prompt for each analyst and the manager. You can edit any prompt directly in the browser and save it. Overridden prompts are highlighted and have a **Reset** button to restore the built-in default.

Useful for:
- Adjusting analyst focus or tone without touching source code
- Injecting tournament-specific context into every analysis (e.g., "this year's bracket has unusually many 5-12 upsets")
- Experimenting with different reasoning styles for the manager

### CLI

#### 1. Import a bracket

Bracket JSON is a list of round-1 matchups:

```json
[
  {"round_num": 1, "region": "East", "favorite_name": "UConn", "favorite_seed": 1,
   "underdog_name": "Stetson", "underdog_seed": 16},
  ...
]
```

```bash
bracket-team import-bracket data/2026_bracket.json --year 2026 --name "March Madness 2026"
# → Imported bracket ID=1
```

#### 2. Gather tournament data

```bash
bracket-team gather-data --bracket-id 1 --year 2026
```

Takes 6–8 minutes (rate-limited for Sports Reference). Data sources:

| Data | Source | Notes |
|------|--------|-------|
| Team stats, roster, coach info | Sports Reference CBB | ~2 req/team at 0.33 req/sec |
| Efficiency metrics (AdjOE/DE, Barthag, WAB, Quad records) | BartTorvik T-Rank | 1 CSV download for all teams |
| Player stats (top 5 by minutes, PPG/RPG/APG) | ESPN Statistics API | 2 req/sec |
| Betting lines (spread, ML, O/U) | The Odds API | Requires `BT_ODDS_API_KEY`; scraped for all bracket rounds |
| Seed matchup history (1985–2024) | Hardcoded | No network required |

> **Odds note:** Lines are only available ~1–2 weeks before games tip off. Run `gather-data` close to the tournament window to get useful odds data.

#### 3. Run the bracket

```bash
bracket-team run-bracket --bracket-id 1 --run-name "My Run"
```

Processes all 63 matchups round by round.

#### 4. View results

```bash
bracket-team show-run --run-id 1
```

To list all runs for a bracket:

```bash
bracket-team list-runs --bracket-id 1
```

---

## CLI Reference

```
$ bracket-team --help
Usage: bracket-team [OPTIONS] COMMAND [ARGS]...

  bracket-team: Multi-agent NCAA bracket prediction.

Options:
  --help  Show this message and exit.

Commands:
  analyze         Analyze a single matchup using all four analysts + manager.
  gather-data     Scrape and cache tournament data (stats, injuries, odds).
  import-bracket  Import bracket matchups from a JSON file.
  list-brackets   List all imported brackets.
  list-runs       List all runs for a bracket.
  run-bracket     Analyze every round of a bracket, propagating winners.
  serve           Start the FastAPI web server.
  show-bracket    Print completed bracket results grouped by round and region.
  show-run        Show predictions and cost summary for a run.
```

---

## Analyst Agents

Each matchup goes through three phases:

| Phase | What happens |
|-------|-------------|
| **Research** | 4 analysts run in parallel, each receiving pre-scraped data relevant to their specialty |
| **Discussion** | Each analyst challenges one other; targets rebut |
| **Decision** | Manager synthesizes all reports, discussion, weighted score, and user preferences into a final pick |

### Analysts and their data

| Analyst | Data injected | Focus |
|---------|--------------|-------|
| `sports_analyst` | Team stats, efficiency (AdjOE/DE, Barthag), pace, coach record, quad records, last-10 form | Season performance, matchup style |
| `odds_analyst` | Spread, moneyline, O/U, implied win%, line movement, sharp action | Market consensus, value |
| `historical_analyst` | Seed matchup history (upset rates), SRS, SOS, luck rating, bad losses | Historical patterns, resume |
| `injury_analyst` | Player roster with injury flags, tournament schedule (rest days, venue), roster composition | Availability, fatigue, proximity |

---

## Project Structure

```
src/bracket_team/
├── agents/          # LLM analyst + manager agents, prompt templates
├── api/             # FastAPI app and route handlers
├── cli/             # Click CLI commands
├── db/              # SQLite schema, migrations, repositories
├── scraper/         # Data scrapers (SR CBB, BartTorvik, ESPN, Odds API, schedule)
└── service/         # Business logic (pipeline, run management, scoring)
static/              # Single-file web UI (index.html)
data/                # Sample bracket JSON files
docs/                # Architecture and planning docs
tests/unit/          # Unit tests (no network required)
```

---

## Running Tests

```bash
pytest                    # run unit tests
pytest -m slow            # include slow tests (real LLM calls, requires API key)
ruff check src/           # lint
```

---

## Docker

```bash
docker build -t bracket-team .
docker run -p 8000:8000 \
  -e BT_ANTHROPIC_API_KEY=sk-ant-... \
  -v $(pwd)/bracket_team.db:/app/bracket_team.db \
  bracket-team
```

> In Docker, start the server with `--host 0.0.0.0` to bind to all interfaces: `bracket-team serve --host 0.0.0.0 --port 8000`

### Docker Compose

```bash
# Copy and fill in your API key
cp .env.template .env

# Start the service (builds image on first run)
docker compose up

# Run in the background
docker compose up -d

# Tear down (keeps the named volume with your DB)
docker compose down
```

The compose file mounts a named volume at `/data` and sets `BT_DATABASE_URL=/data/bracket_team.db` so your database persists across container restarts.

---

## Development (Dev Container)

The repo ships with a VS Code dev container that gives you a fully configured Python 3.12 environment with `uv`, `ruff`, and Claude Code pre-installed.

### Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (or Docker Engine + Docker Compose)
- [VS Code](https://code.visualstudio.com/) with the [Dev Containers extension](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers)

### Getting started

1. **Open the repo in VS Code.**

2. When prompted *"Reopen in Container"*, click it. Or open the command palette (`Ctrl+Shift+P` / `Cmd+Shift+P`) and run **Dev Containers: Reopen in Container**.

3. VS Code builds the image (first time only — ~2 minutes) and drops you into the container at `/workspaces/bracket_team`.

4. The `postCreateCommand` runs `uv sync` automatically, installing all dependencies including dev extras.

5. **Copy and configure your environment:**

   ```bash
   cp .env.template .env
   # Edit .env and set BT_ANTHROPIC_API_KEY (and optionally BT_GEMINI_API_KEY, BT_ODDS_API_KEY)
   ```

6. **Run the dev server:**

   ```bash
   bracket-team serve --reload
   ```

   Open `http://localhost:8000` in your browser (VS Code auto-forwards the port).

### What's included in the dev container

| Tool | Version | Notes |
|------|---------|-------|
| Python | 3.12 | System interpreter at `/usr/local/bin/python3` |
| uv | latest | Fast package manager; used instead of pip for installs |
| ruff | latest | Linter + formatter; format-on-save is enabled |
| Claude Code | latest | `claude` CLI available globally |
| Node.js / npm | system | Required by Claude Code |

### Your Claude credentials are mounted read-only

The dev container mounts `~/.claude` and `~/.claude.json` from your host machine so the `claude` CLI inside the container is already authenticated. No extra login step needed.

### Running tests inside the container

```bash
pytest                    # unit tests (no network, ~30s)
pytest -m slow            # real LLM calls (requires API key in .env)
ruff check src/           # lint
```
