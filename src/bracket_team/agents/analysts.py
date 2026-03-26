"""Four analyst agent implementations."""

from __future__ import annotations

import logging

import aiosqlite

from bracket_team.agents.llm import AgentConfig, AgentLLM, LLMResponse
from bracket_team.agents.prompt_loader import get_prompt
from bracket_team.agents.schemas import AnalystReport
from bracket_team.db.connection import get_connection
from bracket_team.db.models import Matchup

logger = logging.getLogger(__name__)

ANALYST_ROLES = [
    "sports_analyst",
    "odds_analyst",
    "historical_analyst",
    "injury_analyst",
]


def _matchup_message(matchup: Matchup, context_block: str = "") -> str:
    base = (
        f"Analyze this NCAA tournament matchup:\n\n"
        f"Round: {matchup.round_num} | Region: {matchup.region}\n"
        f"Favorite: {matchup.favorite_name} (#{matchup.favorite_seed} seed)\n"
        f"Underdog:  {matchup.underdog_name} (#{matchup.underdog_seed} seed)\n"
    )
    if context_block:
        base += f"\n{context_block}\n"
    return base + "\nProduce your structured AnalystReport now."


async def run_analyst(
    llm: AgentLLM,
    role: str,
    model: str,
    matchup: Matchup,
    temperature: float = 0.7,
    max_tokens: int = 2048,
) -> tuple[AnalystReport, LLMResponse]:
    """Run a single analyst and return (parsed report, raw LLM response)."""
    config = AgentConfig(
        role=role,
        model=model,
        system_prompt=get_prompt(role),
        temperature=temperature,
        max_tokens=max_tokens,
    )
    context_block = await _fetch_context(matchup, role)
    response = await llm.generate(config, _matchup_message(matchup, context_block), AnalystReport)
    report = AnalystReport.model_validate_json(response.content)
    return report, response


# ---------------------------------------------------------------------------
# Context injection helpers
# ---------------------------------------------------------------------------

async def _fetch_context(matchup: Matchup, role: str) -> str:
    """Fetch pre-scraped data for this matchup/role. Returns "" on any failure."""
    try:
        async with get_connection() as conn:
            return await _build_context_block(conn, matchup, role)
    except Exception:
        return ""  # graceful degradation — analyst still runs without data


async def _build_context_block(
    conn: aiosqlite.Connection, matchup: Matchup, role: str
) -> str:
    """Assemble a role-specific context block from cached DB data."""
    parts: list[str] = []

    if role == "sports_analyst":
        fav_stats = await _get_team_stats(conn, matchup.bracket_id, matchup.favorite_name)
        dog_stats = await _get_team_stats(conn, matchup.bracket_id, matchup.underdog_name)
        if fav_stats or dog_stats:
            parts.append("=== PRE-SCRAPED SPORTS DATA ===")
            if fav_stats:
                parts.append(_format_sports_stats(matchup.favorite_name, fav_stats))
            if dog_stats:
                parts.append(_format_sports_stats(matchup.underdog_name, dog_stats))

    elif role == "odds_analyst":
        odds = await _get_team_odds(conn, matchup.id)
        seed_hist = await _get_seed_history(conn, matchup.favorite_seed, matchup.underdog_seed)
        if odds or seed_hist:
            parts.append("=== PRE-SCRAPED ODDS DATA ===")
            if odds:
                parts.append(_format_odds(odds))
            if seed_hist:
                parts.append(_format_seed_history(seed_hist))

    elif role == "historical_analyst":
        seed_hist = await _get_seed_history(conn, matchup.favorite_seed, matchup.underdog_seed)
        fav_stats = await _get_team_stats(conn, matchup.bracket_id, matchup.favorite_name)
        dog_stats = await _get_team_stats(conn, matchup.bracket_id, matchup.underdog_name)
        if seed_hist or fav_stats or dog_stats:
            parts.append("=== PRE-SCRAPED HISTORICAL DATA ===")
            if seed_hist:
                parts.append(_format_seed_history(seed_hist))
            for name, s in [
                (matchup.favorite_name, fav_stats),
                (matchup.underdog_name, dog_stats),
            ]:
                if not s:
                    continue
                line_parts = [f"{name}:"]
                if s.get("srs") is not None:
                    line_parts.append(f"SRS {s['srs']:.1f}")
                if s.get("sos_rank"):
                    line_parts.append(f"SOS #{s['sos_rank']}")
                if s.get("barthag") is not None:
                    line_parts.append(f"Barthag {s['barthag']:.3f}")
                q1w = s.get("quad1_wins")
                q1l = s.get("quad1_losses")
                if q1w is not None and q1l is not None and (q1w > 0 or q1l > 0):
                    line_parts.append(f"Q1 {q1w}-{q1l}")
                if s.get("bart_luck") is not None:
                    line_parts.append(f"Luck {s['bart_luck']:+.3f}")
                q3l = s.get("quad3_losses") or 0
                q4l = s.get("quad4_losses") or 0
                if q3l + q4l > 0:
                    line_parts.append(f"Bad losses (Q3+Q4): {q3l + q4l}")
                parts.append(" | ".join(line_parts))

    elif role == "injury_analyst":
        from bracket_team.scraper.tournament_schedule import format_schedule_context

        fav_stats = await _get_team_stats(conn, matchup.bracket_id, matchup.favorite_name)
        dog_stats = await _get_team_stats(conn, matchup.bracket_id, matchup.underdog_name)
        fav_players = await _get_player_stats(conn, matchup.bracket_id, matchup.favorite_name)
        dog_players = await _get_player_stats(conn, matchup.bracket_id, matchup.underdog_name)

        # Schedule / fatigue / venue context
        year = matchup.bracket_id  # bracket year is looked up below
        if fav_stats and fav_stats.get("bracket_id"):
            # Look up the bracket year from the brackets table
            async with conn.execute(
                "SELECT year FROM brackets WHERE id = ?", (matchup.bracket_id,)
            ) as cur:
                row = await cur.fetchone()
                year = row[0] if row else 2026
        schedule_ctx = format_schedule_context(year, matchup.region, matchup.round_num)
        if schedule_ctx:
            parts.append("=== SCHEDULE / FATIGUE / VENUE ===")
            parts.append(schedule_ctx)

        if fav_players or dog_players or fav_stats or dog_stats:
            parts.append("=== ROSTER & INJURY STATUS ===")
            if fav_players:
                parts.append(_format_player_stats_with_injuries(matchup.favorite_name, fav_players))
            if dog_players:
                parts.append(_format_player_stats_with_injuries(matchup.underdog_name, dog_players))
            if fav_stats:
                parts.append(_format_roster(matchup.favorite_name, fav_stats))
            if dog_stats:
                parts.append(_format_roster(matchup.underdog_name, dog_stats))

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# DB query helpers
# ---------------------------------------------------------------------------

async def _get_team_stats(
    conn: aiosqlite.Connection, bracket_id: int, team_name: str
) -> dict | None:
    async with conn.execute(
        "SELECT * FROM team_stats WHERE bracket_id = ? AND team_name = ?",
        (bracket_id, team_name),
    ) as cursor:
        row = await cursor.fetchone()
        return dict(row) if row else None


async def _get_team_odds(
    conn: aiosqlite.Connection, matchup_id: int
) -> dict | None:
    async with conn.execute(
        "SELECT * FROM team_odds WHERE matchup_id = ?",
        (matchup_id,),
    ) as cursor:
        row = await cursor.fetchone()
        return dict(row) if row else None


async def _get_seed_history(
    conn: aiosqlite.Connection, favorite_seed: int, underdog_seed: int
) -> dict | None:
    async with conn.execute(
        "SELECT * FROM seed_matchup_history WHERE favorite_seed = ? AND underdog_seed = ?",
        (favorite_seed, underdog_seed),
    ) as cursor:
        row = await cursor.fetchone()
        return dict(row) if row else None


async def _get_player_stats(
    conn: aiosqlite.Connection, bracket_id: int, team_name: str
) -> list[dict]:
    async with conn.execute(
        "SELECT * FROM team_player_stats WHERE bracket_id = ? AND team_name = ? ORDER BY mpg DESC LIMIT 5",
        (bracket_id, team_name),
    ) as cursor:
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def _format_sports_stats(team_name: str, s: dict) -> str:
    lines = [f"{team_name}:"]
    record = ""
    if s.get("season_wins") is not None and s.get("season_losses") is not None:
        record = f"{s['season_wins']}-{s['season_losses']}"
        if s.get("conf_wins") is not None:
            record += f" ({s['conf_wins']}-{s['conf_losses']} conf)"
    if record:
        lines.append(f"  Record: {record}")
    if s.get("conference"):
        lines.append(f"  Conference: {s['conference']}")
    if s.get("ap_rank") is not None:
        lines.append(f"  AP Rank: #{s['ap_rank']}")
    if s.get("srs") is not None:
        lines.append(f"  SRS: {s['srs']:.1f}")
    if s.get("sos_rank"):
        lines.append(f"  SOS rank: #{s['sos_rank']}")
    if s.get("adj_off_eff") is not None:
        lines.append(f"  ORtg: {s['adj_off_eff']:.1f}")
    if s.get("adj_def_eff") is not None:
        lines.append(f"  DRtg: {s['adj_def_eff']:.1f}")
    if s.get("ppg") is not None:
        scoring = f"  Scoring: {s['ppg']:.1f} PPG"
        if s.get("opp_ppg") is not None:
            scoring += f", {s['opp_ppg']:.1f} opp PPG"
        lines.append(scoring)
    if s.get("pace") is not None:
        lines.append(f"  Pace: {s['pace']:.1f}")
    if s.get("fg_pct") is not None:
        shooting = f"  Shooting: {s['fg_pct']:.1%} FG"
        if s.get("three_pt_pct") is not None:
            shooting += f", {s['three_pt_pct']:.1%} 3P"
        if s.get("ft_pct") is not None:
            shooting += f", {s['ft_pct']:.1%} FT"
        lines.append(shooting)
    if s.get("opp_fg_pct") is not None:
        def_shooting = f"  Opp shooting: {s['opp_fg_pct']:.1%} FG"
        if s.get("opp_three_pt_pct") is not None:
            def_shooting += f", {s['opp_three_pt_pct']:.1%} 3P"
        lines.append(def_shooting)
    if s.get("orb_per_g") is not None or s.get("drb_per_g") is not None:
        reb_parts = []
        if s.get("orb_per_g") is not None:
            reb_parts.append(f"{s['orb_per_g']:.1f} ORB")
        if s.get("drb_per_g") is not None:
            reb_parts.append(f"{s['drb_per_g']:.1f} DRB")
        lines.append(f"  Rebounds/g: {', '.join(reb_parts)}")
    if s.get("ast_per_g") is not None:
        lines.append(f"  Assists/g: {s['ast_per_g']:.1f}")
    if s.get("stl_per_g") is not None or s.get("blk_per_g") is not None:
        dis_parts = []
        if s.get("stl_per_g") is not None:
            dis_parts.append(f"{s['stl_per_g']:.1f} STL")
        if s.get("blk_per_g") is not None:
            dis_parts.append(f"{s['blk_per_g']:.1f} BLK")
        if s.get("opp_tov_per_g") is not None:
            dis_parts.append(f"{s['opp_tov_per_g']:.1f} forced TO")
        lines.append(f"  Disruption/g: {', '.join(dis_parts)}")
    if s.get("head_coach"):
        coach_line = f"  Head Coach: {s['head_coach']}"
        if s.get("coach_tourney_record"):
            coach_line += f" ({s['coach_tourney_record']})"
        elif s.get("coach_tourney_appearances"):
            coach_line += f" ({s['coach_tourney_appearances']} NCAA tournament apps)"
        lines.append(coach_line)
    ct = ""
    if s.get("conf_tourney_wins") or s.get("conf_tourney_losses"):
        ct = f"{s.get('conf_tourney_wins', 0)}-{s.get('conf_tourney_losses', 0)}"
        lines.append(f"  Conf. Tourney: {ct}")
    if s.get("bart_adj_oe") is not None:
        lines.append(f"  BartTorvik AdjOE: {s['bart_adj_oe']:.1f} | AdjDE: {s.get('bart_adj_de', 0):.1f}")
    if s.get("barthag") is not None:
        lines.append(f"  Barthag (win prob): {s['barthag']:.3f}")
    if s.get("bart_tempo") is not None:
        lines.append(f"  Adj. Tempo: {s['bart_tempo']:.1f}")
    if s.get("bart_luck") is not None:
        lines.append(f"  Luck: {s['bart_luck']:+.3f}")
    if s.get("bart_wab") is not None:
        lines.append(f"  WAB: {s['bart_wab']:+.1f}")
    q1_w = s.get("quad1_wins")
    q1_l = s.get("quad1_losses")
    if q1_w is not None and q1_l is not None and (q1_w > 0 or q1_l > 0):
        lines.append(f"  Quad 1 record: {q1_w}-{q1_l}")
    q2_w = s.get("quad2_wins")
    q2_l = s.get("quad2_losses")
    if q2_w is not None and q2_l is not None and (q2_w > 0 or q2_l > 0):
        lines.append(f"  Quad 2 record: {q2_w}-{q2_l}")
    q3_w = s.get("quad3_wins")
    q3_l = s.get("quad3_losses")
    if q3_w is not None and q3_l is not None and (q3_w > 0 or q3_l > 0):
        lines.append(f"  Quad 3 record: {q3_w}-{q3_l}")
    q4_w = s.get("quad4_wins")
    q4_l = s.get("quad4_losses")
    if q4_w is not None and q4_l is not None and (q4_w > 0 or q4_l > 0):
        lines.append(f"  Quad 4 record: {q4_w}-{q4_l}")
    # Flag bad losses (Q3/Q4 losses are strong upset predictors)
    q3_losses = q3_l or 0
    q4_losses = q4_l or 0
    if q3_losses + q4_losses > 0:
        lines.append(f"  ** Bad losses (Q3+Q4): {q3_losses + q4_losses} **")
    if s.get("ft_rate") is not None:
        lines.append(f"  FT Rate (FTA/FGA): {s['ft_rate']:.3f}")
    if s.get("tov_pct") is not None:
        lines.append(f"  TOV%: {s['tov_pct']:.1f}%")
    nw = s.get("neutral_wins", 0)
    nl = s.get("neutral_losses", 0)
    if nw or nl:
        lines.append(f"  Neutral site: {nw}-{nl}")
    l10w = s.get("last10_wins")
    l10l = s.get("last10_losses")
    if l10w is not None:
        lines.append(f"  Last 10 games: {l10w}-{l10l}")
    return "\n".join(lines)


def _format_odds(o: dict) -> str:
    lines = ["Betting Lines:"]
    if o.get("spread") is not None:
        lines.append(f"  Spread: {o['spread']:+.1f} (favorite)")
    if o.get("favorite_ml") is not None:
        lines.append(f"  Moneyline: {o['favorite_name']} {o['favorite_ml']:+d}")
    if o.get("underdog_ml") is not None:
        lines.append(f"  Moneyline: {o['underdog_name']} {o['underdog_ml']:+d}")
    if o.get("over_under") is not None:
        lines.append(f"  O/U: {o['over_under']:.1f}")
    if o.get("implied_fav_win_pct") is not None:
        lines.append(f"  Implied win%: {o['favorite_name']} {o['implied_fav_win_pct']:.1%}")
    if o.get("implied_dog_win_pct") is not None:
        lines.append(f"  Implied win%: {o['underdog_name']} {o['implied_dog_win_pct']:.1%}")
    return "\n".join(lines)


def _format_seed_history(h: dict) -> str:
    lines = [
        f"Seed Matchup History ({h['favorite_seed']}-seed vs {h['underdog_seed']}-seed):",
        f"  Total games: {h['total_games']}",
        f"  {h['favorite_seed']}-seed win rate: {h['favorite_win_pct']:.1f}%",
        f"  Upset rate: {h['upset_rate_pct']:.1f}%",
    ]
    if h.get("notable_pattern"):
        lines.append(f"  Pattern: {h['notable_pattern']}")
    return "\n".join(lines)


def _format_player_stats(team_name: str, players: list[dict]) -> str:
    if not players:
        return ""
    lines = [f"{team_name} key players:"]
    for p in players:
        parts = [p.get("player_name", "Unknown")]
        if p.get("position"):
            parts[0] += f" ({p['position']})"
        if p.get("class_year"):
            parts[0] += f" {p['class_year']}"
        stats_parts = []
        if p.get("ppg") is not None:
            stats_parts.append(f"{p['ppg']:.1f} PPG")
        if p.get("rpg") is not None:
            stats_parts.append(f"{p['rpg']:.1f} RPG")
        if p.get("apg") is not None:
            stats_parts.append(f"{p['apg']:.1f} APG")
        if p.get("mpg") is not None:
            stats_parts.append(f"{p['mpg']:.1f} MPG")
        if p.get("ft_pct") is not None:
            stats_parts.append(f"{p['ft_pct']:.1f}% FT")
        if p.get("three_pt_pct") is not None:
            stats_parts.append(f"{p['three_pt_pct']:.1f}% 3PT")
        if p.get("usage_rate") is not None:
            stats_parts.append(f"{p['usage_rate']:.1f}% USG")
        if stats_parts:
            parts.append(": " + ", ".join(stats_parts))
        lines.append("  " + "".join(parts))
    return "\n".join(lines)


def _format_player_stats_with_injuries(team_name: str, players: list[dict]) -> str:
    """Format player stats with injury/availability flags for the injury analyst."""
    if not players:
        return ""
    lines = [f"{team_name} key players:"]
    for p in players:
        status = ""
        if p.get("injured"):
            note = p.get("injury_note") or "Unavailable"
            status = f" ** {note} **"

        parts = [p.get("player_name", "Unknown")]
        if p.get("position"):
            parts[0] += f" ({p['position']})"
        if p.get("class_year"):
            parts[0] += f" {p['class_year']}"
        stats_parts = []
        if p.get("ppg") is not None:
            stats_parts.append(f"{p['ppg']:.1f} PPG")
        if p.get("rpg") is not None:
            stats_parts.append(f"{p['rpg']:.1f} RPG")
        if p.get("apg") is not None:
            stats_parts.append(f"{p['apg']:.1f} APG")
        if p.get("mpg") is not None:
            stats_parts.append(f"{p['mpg']:.1f} MPG")
        if stats_parts:
            parts.append(": " + ", ".join(stats_parts))
        parts.append(status)
        lines.append("  " + "".join(parts))
    return "\n".join(lines)


def _format_roster(team_name: str, s: dict) -> str:
    parts = []
    if s.get("freshmen_count") is not None:
        parts.append(f"FR: {s['freshmen_count']}")
    if s.get("senior_count") is not None:
        parts.append(f"SR: {s['senior_count']}")
    if s.get("transfer_count") is not None:
        parts.append(f"transfers: {s['transfer_count']}")
    if not parts:
        return ""
    return f"{team_name} roster: {', '.join(parts)}"
