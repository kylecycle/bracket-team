"""Tournament schedule: dates, venues, and rest-day calculations.

Provides per-round game dates and venue cities for each region, enabling
the injury/fatigue analyst to assess rest days between games and proximity
advantage.

The schedule is year-specific. Add new years as they're announced.
"""

from __future__ import annotations

# Each year maps to a dict of:
#   "rounds": { round_num: {"dates": [...], "label": str} }
#   "venues": { region: { round_range: "City, ST" } }
#   "final_four_venue": "City, ST"
#
# round_range keys: "r64_r32" (rounds 1-2), "s16_e8" (rounds 3-4)

TOURNAMENT_SCHEDULE: dict[int, dict] = {
    2026: {
        "rounds": {
            1: {"dates": ["2026-03-19", "2026-03-20"], "label": "Round of 64"},
            2: {"dates": ["2026-03-21", "2026-03-22"], "label": "Round of 32"},
            3: {"dates": ["2026-03-26", "2026-03-27"], "label": "Sweet 16"},
            4: {"dates": ["2026-03-28", "2026-03-29"], "label": "Elite 8"},
            5: {"dates": ["2026-04-04"], "label": "Final Four"},
            6: {"dates": ["2026-04-06"], "label": "Championship"},
        },
        "venues": {
            "East": {"r64_r32": "Newark, NJ", "s16_e8": "Boston, MA"},
            "West": {"r64_r32": "San Diego, CA", "s16_e8": "San Francisco, CA"},
            "Midwest": {"r64_r32": "Indianapolis, IN", "s16_e8": "Detroit, MI"},
            "South": {"r64_r32": "Dallas, TX", "s16_e8": "Atlanta, GA"},
        },
        "final_four_venue": "San Antonio, TX",
    },
}


def get_venue(year: int, region: str, round_num: int) -> str | None:
    """Return the venue city for a given region and round."""
    sched = TOURNAMENT_SCHEDULE.get(year)
    if not sched:
        return None
    if round_num >= 5:
        return sched.get("final_four_venue")
    venues = sched.get("venues", {}).get(region, {})
    if round_num <= 2:
        return venues.get("r64_r32")
    return venues.get("s16_e8")


def get_rest_days(year: int, round_num: int) -> int | None:
    """Return the minimum days of rest between the previous round and this one.

    Returns None for round 1 (no prior game) or if schedule is unavailable.
    """
    sched = TOURNAMENT_SCHEDULE.get(year)
    if not sched or round_num <= 1:
        return None
    rounds = sched.get("rounds", {})
    prev = rounds.get(round_num - 1)
    curr = rounds.get(round_num)
    if not prev or not curr:
        return None
    # Use last date of previous round → first date of current round
    prev_last = prev["dates"][-1]
    curr_first = curr["dates"][0]
    from datetime import date

    d1 = date.fromisoformat(prev_last)
    d2 = date.fromisoformat(curr_first)
    return (d2 - d1).days


def format_schedule_context(year: int, region: str, round_num: int) -> str:
    """Build a human-readable schedule/fatigue context block for the analyst."""
    lines: list[str] = []
    sched = TOURNAMENT_SCHEDULE.get(year)
    if not sched:
        return ""

    round_info = sched.get("rounds", {}).get(round_num)
    if round_info:
        lines.append(f"Round: {round_info['label']} ({', '.join(round_info['dates'])})")

    venue = get_venue(year, region, round_num)
    if venue:
        lines.append(f"Venue: {venue}")

    rest = get_rest_days(year, round_num)
    if rest is not None:
        if rest <= 1:
            lines.append(f"Rest days since last round: {rest} (BACK-TO-BACK — significant fatigue factor)")
        elif rest <= 2:
            lines.append(f"Rest days since last round: {rest} (short rest)")
        else:
            lines.append(f"Rest days since last round: {rest} (adequate rest)")

    return "\n".join(lines)
