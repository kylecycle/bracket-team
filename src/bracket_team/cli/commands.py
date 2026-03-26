"""CLI commands: analyze, import-bracket, list-runs."""

from __future__ import annotations

import asyncio
import json
import sys

import click

from bracket_team.config import get_config
from bracket_team.db.connection import init_db
from bracket_team.service.bracket_service import (
    MatchupInput,
    import_bracket,
    list_brackets,
)
from bracket_team.service.run_service import (
    create_run,
    get_run,
    get_run_costs,
    get_run_predictions,
    get_total_cost,
    list_runs,
)


def _run(coro):
    return asyncio.run(coro)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.pass_context
def cli(ctx: click.Context) -> None:
    """bracket-team: Multi-agent NCAA bracket prediction."""
    ctx.ensure_object(dict)
    cfg = get_config()
    _run(init_db(cfg.database_url))


# ---------------------------------------------------------------------------
# analyze — run the pipeline for a single matchup
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--team1", required=True, help="Favorite team name")
@click.option("--team2", required=True, help="Underdog team name")
@click.option("--seed1", required=True, type=int, help="Favorite seed (lower number)")
@click.option("--seed2", required=True, type=int, help="Underdog seed (higher number)")
@click.option("--round", "round_num", default=1, show_default=True, type=int,
              help="Tournament round (1-6)")
@click.option("--region", default="East", show_default=True, help="Region name")
@click.option("--run-name", default="CLI Run", show_default=True, help="Name for this run")
@click.option("--stub", is_flag=True, default=False,
              help="Use stub LLM backend (no API key required, for testing)")
def analyze(
    team1: str,
    team2: str,
    seed1: int,
    seed2: int,
    round_num: int,
    region: str,
    run_name: str,
    stub: bool,
) -> None:
    """Analyze a single matchup using all four analysts + manager."""
    from bracket_team.agents.llm import create_backend
    from bracket_team.service.pipeline import MatchupPipeline

    cfg = get_config()

    async def _analyze() -> None:
        matchup_input = MatchupInput(
            round_num=round_num,
            region=region,
            favorite_name=team1,
            favorite_seed=seed1,
            underdog_name=team2,
            underdog_seed=seed2,
        )
        bracket, matchups = await import_bracket(
            year=2025,
            tournament_name=f"Single Matchup: {team1} vs {team2}",
            matchups=[matchup_input],
        )
        run = await create_run(bracket_id=bracket.id, name=run_name)
        matchup = matchups[0]

        mode = "STUB MODE" if stub else "LIVE"
        click.echo(f"\nAnalyzing: {team1} (#{seed1}) vs {team2} (#{seed2})  [{mode}]")
        click.echo(f"Round {round_num} | {region} region | Run ID: {run.id}\n")

        llm = create_backend(cfg, override_provider="stub" if stub else None)
        pipeline = MatchupPipeline(llm, cfg)
        prediction = await pipeline.run(run.id, matchup)
        total_cost = await get_total_cost(run.id)

        _print_prediction(prediction, total_cost)

    _run(_analyze())


def _print_prediction(prediction, total_cost: float) -> None:
    click.echo("=" * 60)
    click.echo("PREDICTION")
    click.echo("=" * 60)
    click.echo(f"  Winner:      {prediction.predicted_winner}")
    click.echo(f"  Outcome:     {prediction.outcome_type.upper()}")
    click.echo(f"  Confidence:  {prediction.confidence.upper()}")
    click.echo(f"  Score:       {prediction.weighted_score:.3f}")
    click.echo(f"  Model:       {prediction.manager_model}")
    click.echo()
    click.echo("SYNTHESIS:")
    click.echo(f"  {prediction.synthesis}")
    click.echo()
    click.echo(f"  Total cost:  ${total_cost:.4f}")
    click.echo("=" * 60)


# ---------------------------------------------------------------------------
# run-bracket — analyze all matchups in an imported bracket
# ---------------------------------------------------------------------------

@cli.command("run-bracket")
@click.option("--bracket-id", required=True, type=int, help="Bracket ID to analyze")
@click.option("--run-name", default="Bracket Run", show_default=True)
@click.option("--stub", is_flag=True, default=False,
              help="Use stub LLM backend (no API key required, for testing)")
def run_bracket_cmd(bracket_id: int, run_name: str, stub: bool) -> None:
    """Analyze every round of a bracket, propagating winners between rounds."""
    from bracket_team.agents.llm import create_backend
    from bracket_team.service.pipeline import analyze_bracket

    cfg = get_config()

    async def _run_bracket() -> None:
        run = await create_run(bracket_id=bracket_id, name=run_name)
        mode = "STUB MODE" if stub else "LIVE"
        click.echo(f"\nRunning bracket ID={bracket_id}  [{mode}]  Run ID={run.id}")

        llm = create_backend(cfg, override_provider="stub" if stub else None)

        completed = 0

        def _progress(round_num, total_rounds, done, total):
            nonlocal completed
            completed += 1
            click.echo(
                f"  Round {round_num}: {done}/{total} matchups complete", nl=True
            )

        predictions = await analyze_bracket(run.id, llm, cfg, progress_callback=_progress)

        click.echo(f"\n{len(predictions)} matchup(s) analyzed.\n")
        click.echo("FINAL BRACKET RESULTS:")
        click.echo("=" * 60)
        for p in predictions:
            confidence_marker = {"high": "***", "medium": "** ", "low": "*  "}.get(
                p.confidence, "   "
            )
            upset_marker = " [UPSET]" if p.outcome_type == "upset" else ""
            click.echo(
                f"  {confidence_marker} Matchup {p.matchup_id:>3}: "
                f"{p.predicted_winner}{upset_marker}"
            )

        total_cost = await get_total_cost(run.id)
        click.echo(f"\n  Total cost: ${total_cost:.4f}")
        click.echo("=" * 60)

    _run(_run_bracket())


# ---------------------------------------------------------------------------
# list-brackets — show all imported brackets
# ---------------------------------------------------------------------------

@cli.command("list-brackets")
def list_brackets_cmd() -> None:
    """List all imported brackets."""
    async def _list():
        brackets = await list_brackets()
        if not brackets:
            click.echo("No brackets found.")
            return
        for b in brackets:
            click.echo(f"  Bracket {b.id}: {b.tournament_name} ({b.year}) — {b.created_at}")

    _run(_list())


# ---------------------------------------------------------------------------
# import-bracket — load a bracket from a JSON file
# ---------------------------------------------------------------------------

@cli.command("import-bracket")
@click.argument("filepath", type=click.Path(exists=True))
@click.option("--year", required=True, type=int)
@click.option("--name", "tournament_name", required=True)
def import_bracket_cmd(filepath: str, year: int, tournament_name: str) -> None:
    """Import bracket matchups from a JSON file.

    JSON format: list of objects with keys:
    round_num, region, favorite_name, favorite_seed, underdog_name, underdog_seed
    """
    with open(filepath) as f:
        data = json.load(f)

    matchups = [
        MatchupInput(
            round_num=m["round_num"],
            region=m["region"],
            favorite_name=m["favorite_name"],
            favorite_seed=m["favorite_seed"],
            underdog_name=m["underdog_name"],
            underdog_seed=m["underdog_seed"],
        )
        for m in data
    ]

    async def _import():
        bracket, created = await import_bracket(year, tournament_name, matchups)
        click.echo(f"Imported bracket ID={bracket.id}: {tournament_name} ({year})")
        click.echo(f"  {len(created)} matchups created.")

    _run(_import())


# ---------------------------------------------------------------------------
# show-bracket — print completed bracket results round-by-round
# ---------------------------------------------------------------------------

_ROUND_NAMES = {
    1: "First Round",
    2: "Second Round",
    3: "Sweet Sixteen",
    4: "Elite Eight",
    5: "Final Four",
    6: "Championship",
}

_CONFIDENCE_MARKER = {"high": "***", "medium": "** ", "low": "*  "}


@cli.command("show-bracket")
@click.argument("run_id", type=int)
def show_bracket_cmd(run_id: int) -> None:
    """Print completed bracket results grouped by round and region."""
    from bracket_team.db.connection import get_connection
    from bracket_team.db.repositories.matchup_repo import MatchupRepository
    from bracket_team.db.repositories.prediction_repo import PredictionRepository
    from bracket_team.service.run_service import get_run, get_total_cost

    async def _show():
        run = await get_run(run_id)
        if not run:
            click.echo(f"Run {run_id} not found.", err=True)
            sys.exit(1)

        async with get_connection() as conn:
            matchups = await MatchupRepository(conn).list_by_bracket(run.bracket_id)
            predictions = await PredictionRepository(conn).list_by_run(run_id)

        pred_by_matchup = {p.matchup_id: p for p in predictions}

        # Group matchups by round
        rounds: dict[int, list] = {}
        for m in matchups:
            rounds.setdefault(m.round_num, []).append(m)

        click.echo(f"\nRun {run.id}: {run.name}")
        click.echo("=" * 64)

        for round_num in sorted(rounds):
            label = _ROUND_NAMES.get(round_num, f"Round {round_num}")
            click.echo(f"\n  {label.upper()}")
            click.echo("  " + "-" * 60)

            # Group by region within each round
            by_region: dict[str, list] = {}
            for m in rounds[round_num]:
                by_region.setdefault(m.region, []).append(m)

            for region in sorted(by_region):
                if len(by_region) > 1:
                    click.echo(f"\n    [{region}]")
                for m in by_region[region]:
                    pred = pred_by_matchup.get(m.id)
                    if pred is None:
                        click.echo(
                            f"    {'???':3}  #{m.favorite_seed:>2} {m.favorite_name} vs "
                            f"#{m.underdog_seed} {m.underdog_name}  [no prediction]"
                        )
                        continue

                    marker = _CONFIDENCE_MARKER.get(pred.confidence, "   ")
                    upset_tag = " [UPSET]" if pred.outcome_type == "upset" else ""
                    loser = (
                        m.underdog_name
                        if pred.predicted_winner == m.favorite_name
                        else m.favorite_name
                    )
                    click.echo(
                        f"    {marker}  #{m.favorite_seed:>2} {m.favorite_name} vs "
                        f"#{m.underdog_seed} {m.underdog_name}"
                    )
                    click.echo(
                        f"           -> {pred.predicted_winner} defeats {loser}"
                        f"{upset_tag}  (score={pred.weighted_score:+.2f}, {pred.confidence})"
                    )

        total_cost = await get_total_cost(run_id)
        click.echo("\n  Confidence: *** high  ** medium  * low")
        click.echo(f"  Total cost: ${total_cost:.4f}")
        click.echo("=" * 64)

    _run(_show())


# ---------------------------------------------------------------------------
# list-runs — show runs and their predictions
# ---------------------------------------------------------------------------

@cli.command("list-runs")
@click.option("--bracket-id", required=True, type=int)
def list_runs_cmd(bracket_id: int) -> None:
    """List all runs for a bracket."""
    async def _list():
        runs = await list_runs(bracket_id)
        if not runs:
            click.echo("No runs found.")
            return
        for run in runs:
            click.echo(f"  Run {run.id}: {run.name} [{run.status}] — {run.created_at}")

    _run(_list())


# ---------------------------------------------------------------------------
# show-run — detailed view of a run's predictions and costs
# ---------------------------------------------------------------------------

@cli.command("show-run")
@click.argument("run_id", type=int)
def show_run_cmd(run_id: int) -> None:  # noqa: F811
    """Show predictions and cost summary for a run."""
    async def _show():
        run = await get_run(run_id)
        if not run:
            click.echo(f"Run {run_id} not found.", err=True)
            sys.exit(1)

        click.echo(f"\nRun {run.id}: {run.name} [{run.status}]")
        click.echo(f"  Created: {run.created_at}")

        predictions = await get_run_predictions(run_id)
        click.echo(f"\nPredictions ({len(predictions)}):")
        for p in predictions:
            click.echo(
                f"  Matchup {p.matchup_id}: {p.predicted_winner} "
                f"({p.outcome_type}, {p.confidence} confidence, score={p.weighted_score:.2f})"
            )

        total = await get_total_cost(run_id)
        costs = await get_run_costs(run_id)
        click.echo(f"\nCosts: {len(costs)} LLM calls, total ${total:.4f}")

    _run(_show())


# ---------------------------------------------------------------------------
# gather-data — scrape and cache tournament data before a run
# ---------------------------------------------------------------------------

@cli.command("gather-data")
@click.option("--bracket-id", required=True, type=int, help="Bracket ID to gather data for")
@click.option("--year", default=2025, show_default=True, type=int, help="Tournament year")
@click.option("--force", is_flag=True, default=False,
              help="Re-scrape all data even if already cached in the DB")
def gather_data_cmd(bracket_id: int, year: int, force: bool) -> None:
    """Scrape and cache tournament data (stats, injuries, odds) for all teams.

    Run this before `run-bracket` to give analysts real context.
    By default, teams/matchups that already have cached data are skipped.
    Use --force to re-scrape everything.

    The Odds API key is read from BT_ODDS_API_KEY env var (optional).
    """
    from bracket_team.scraper.coordinator import GatherCoordinator

    cfg = get_config()
    odds_api_key = (
        cfg.odds_api_key.get_secret_value() if cfg.odds_api_key else None
    )

    async def _gather() -> None:
        click.echo(f"\nGathering data for bracket ID={bracket_id} (year={year})")
        if force:
            click.echo("  Mode: FORCE — re-scraping all data")
        else:
            click.echo("  Mode: incremental — skipping already-cached data (use --force to override)")
        if odds_api_key:
            click.echo("  Odds API key: configured")
        else:
            click.echo("  Odds API key: not set (BT_ODDS_API_KEY) — odds will be skipped")
        click.echo()

        coord = GatherCoordinator(
            bracket_id=bracket_id,
            year=year,
            odds_api_key=odds_api_key,
            force=force,
        )
        summary = await coord.run(progress=click.echo)

        click.echo("\nSummary:")
        click.echo(f"  Seed history rows: {summary['seed_history_rows']}")
        click.echo(f"  Teams scraped:     {summary['teams_scraped']} new, {summary['teams_skipped']} skipped")
        click.echo(f"  Player records:    {summary['players_scraped']}")
        click.echo(f"  BartTorvik:        {summary['barttorvik_scraped']} new, {summary['barttorvik_skipped']} skipped")
        click.echo(f"  Injury records:    {summary['injuries_scraped']} new, {summary['injuries_skipped']} skipped")
        click.echo(f"  Odds records:      {summary['odds_scraped']} new, {summary['odds_skipped']} skipped")
        if summary["errors"]:
            click.echo(f"\nWarnings ({len(summary['errors'])}):")
            for err in summary["errors"][:10]:
                click.echo(f"  - {err}")
            if len(summary["errors"]) > 10:
                click.echo(f"  ... and {len(summary['errors']) - 10} more")
        click.echo()

    _run(_gather())


# ---------------------------------------------------------------------------
# serve — start the FastAPI web server
# ---------------------------------------------------------------------------

@cli.command("serve")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8000, show_default=True, type=int)
@click.option("--reload", is_flag=True, default=False, help="Auto-reload on code changes")
def serve_cmd(host: str, port: int, reload: bool) -> None:
    """Start the FastAPI web server."""
    try:
        import uvicorn
    except ImportError:
        click.echo("uvicorn not installed. Run: pip install 'bracket-team[web]'", err=True)
        raise SystemExit(1)

    click.echo(f"Starting server at http://{host}:{port}  (docs: http://{host}:{port}/docs)")
    uvicorn.run(
        "bracket_team.api.app:create_app",
        host=host,
        port=port,
        reload=reload,
        factory=True,
    )
