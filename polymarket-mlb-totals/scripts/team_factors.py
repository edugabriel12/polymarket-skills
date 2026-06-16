#!/usr/bin/env python3
"""Automatic team run-environment factors from the MLB Stats API.

A single `standings` request yields every team's season runs scored/allowed, turned
into the STRONG model inputs `*_off` / `*_sp` (1.0 = league average):

  off_factor   = team runs scored / game   ÷ league avg   (>1 -> scores more)
  pitch_factor = team runs allowed / game  ÷ league avg   (>1 -> allows more)

These are exactly the inputs `adjust_mu` consumes (home_runs = base/2 · home_off ·
away_sp, etc.), so the model's mu finally discriminates between matchups instead of
sitting at the league baseline. Same data source already used for settlement, so no
new ToS surface. Best-effort: any failure returns {} and the pipeline degrades to
the market-implied anchor (no fabricated edge). Starter-level SIERA/xFIP (the #1
input per the research) is a documented next step on top of this team baseline.

Pure parsing is isolated for offline tests.
"""

from __future__ import annotations

STATSAPI = "https://statsapi.mlb.com/api/v1"

# Stable MLB Stats API team id -> Polymarket/ballparks canonical abbreviation.
TEAM_ID_ABBR: dict[int, str] = {
    108: "laa", 109: "ari", 110: "bal", 111: "bos", 112: "chc", 113: "cin",
    114: "cle", 115: "col", 116: "det", 117: "hou", 118: "kc", 119: "lad",
    120: "wsh", 121: "nym", 133: "oak", 134: "pit", 135: "sd", 136: "sea",
    137: "sf", 138: "stl", 139: "tb", 140: "tex", 141: "tor", 142: "min",
    143: "phi", 144: "atl", 145: "cws", 146: "mia", 147: "nyy", 158: "mil",
}


def parse_standings_factors(payload: dict) -> dict[str, dict]:
    """{abbr: {off_factor, pitch_factor}} from an MLB Stats API /standings payload."""
    rows = []
    for rec in (payload or {}).get("records", []):
        for tr in rec.get("teamRecords", []):
            tid = (tr.get("team") or {}).get("id")
            abbr = TEAM_ID_ABBR.get(tid)
            rs, ra = tr.get("runsScored"), tr.get("runsAllowed")
            gp = tr.get("gamesPlayed")
            if gp in (None, 0):
                gp = (tr.get("wins") or 0) + (tr.get("losses") or 0)
            if abbr and rs is not None and ra is not None and gp:
                rows.append((abbr, float(rs) / gp, float(ra) / gp))
    if not rows:
        return {}
    league_rpg = sum(r[1] for r in rows) / len(rows)  # avg runs/team/game
    if league_rpg <= 0:
        return {}
    return {abbr: {"off_factor": rspg / league_rpg, "pitch_factor": rapg / league_rpg}
            for (abbr, rspg, rapg) in rows}


def fetch_run_factors(api, season: int, timeout: int = 8) -> dict[str, dict]:
    """Best-effort team factors for a season ({} on any failure)."""
    if not season:
        return {}
    try:
        data = api.get(f"{STATSAPI}/standings",
                       params={"leagueId": "103,104", "season": season,
                               "standingsTypes": "regularSeason"})
    except Exception:  # noqa: BLE001
        return {}
    return parse_standings_factors(data)
