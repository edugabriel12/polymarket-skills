#!/usr/bin/env python3
"""Automatic starting-pitcher run-prevention factors from the MLB Stats API.

The starter is the #1 input for a game total (deep research §2), and the team
season average can't see *who pitches today*. This adds it, ToS-clean and free:

  1. /schedule?hydrate=probablePitcher  -> each team's probable starter for the date
  2. /people/{id}/stats?group=pitching   -> the starter's season HR/BB/K/IP
  3. FIP = (13·HR + 3·BB − 2·K)/IP + c   -> run-prevention factor = FIP / league FIP

SIERA/xFIP (research: SIERA/xFIP > FIP > ERA) are FanGraphs-proprietary; FIP is
computable from the raw MLB Stats API numbers and is a solid, ToS-clean proxy. The
factor maps onto the model's `*_sp` input (>1 = allows more runs); data_inputs
blends it with the team's season pitching (bullpen) factor. Best-effort: any failure
or a too-small sample returns None and the pipeline keeps the team/market value.

Pure math (parse_ip, fip_from_stat, pitcher_factor, parse_probables) is isolated for
offline tests.
"""

from __future__ import annotations

import team_factors

STATSAPI = "https://statsapi.mlb.com/api/v1"

# League-average FIP and the FIP constant (tunable; chosen so an average starter ~1.0).
LEAGUE_FIP = 4.20
FIP_CONSTANT = 3.10
MIN_IP = 10.0                 # below this the season FIP is too noisy to trust
SP_FACTOR_LO, SP_FACTOR_HI = 0.65, 1.50

# Caches so a day's analysis costs one schedule call + one call per distinct pitcher.
_PROBABLES_CACHE: dict[str, dict] = {}
_PITCHER_CACHE: dict[tuple, float | None] = {}


def parse_ip(value) -> float | None:
    """MLB 'inningsPitched' ('120.1' = 120 + 1/3) -> float innings."""
    if value is None:
        return None
    try:
        whole, _, frac = str(value).partition(".")
        outs = int(frac[0]) if frac else 0
        return float(int(whole)) + (outs / 3.0 if outs in (0, 1, 2) else 0.0)
    except (ValueError, TypeError):
        return None


def fip_from_stat(stat: dict, fip_constant: float = FIP_CONSTANT) -> float | None:
    """FIP from a pitching stat split, or None if the sample is too small."""
    try:
        hr = float(stat.get("homeRuns"))
        bb = float(stat.get("baseOnBalls"))
        k = float(stat.get("strikeOuts"))
    except (TypeError, ValueError):
        return None
    ip = parse_ip(stat.get("inningsPitched"))
    if not ip or ip < MIN_IP:
        return None
    return (13.0 * hr + 3.0 * bb - 2.0 * k) / ip + fip_constant


def pitcher_factor(fip: float | None, league_fip: float = LEAGUE_FIP) -> float | None:
    """Run-prevention factor (FIP / league FIP), clamped. <1 = better than average."""
    if fip is None or league_fip <= 0:
        return None
    return max(SP_FACTOR_LO, min(fip / league_fip, SP_FACTOR_HI))


def parse_probables(schedule: dict) -> dict[str, int]:
    """{team_abbr: probable_pitcher_id} from a /schedule?hydrate=probablePitcher payload."""
    out: dict[str, int] = {}
    for d in (schedule or {}).get("dates", []):
        for g in d.get("games", []):
            for side in ("home", "away"):
                t = (g.get("teams", {}) or {}).get(side, {}) or {}
                tid = (t.get("team") or {}).get("id")
                pid = (t.get("probablePitcher") or {}).get("id")
                abbr = team_factors.TEAM_ID_ABBR.get(tid)
                if abbr and pid:
                    out[abbr] = pid
    return out


# ---------------------------------------------------------------------------
# Network (best-effort, cached)
# ---------------------------------------------------------------------------


def _probables(api, date: str) -> dict[str, int]:
    if date in _PROBABLES_CACHE:
        return _PROBABLES_CACHE[date]
    out: dict[str, int] = {}
    try:
        data = api.get(f"{STATSAPI}/schedule",
                       params={"sportId": 1, "date": date, "hydrate": "probablePitcher"})
        out = parse_probables(data)
    except Exception:  # noqa: BLE001
        out = {}
    _PROBABLES_CACHE[date] = out
    return out


def _pitcher_fip(api, pid: int, season: int) -> float | None:
    key = (pid, season)
    if key in _PITCHER_CACHE:
        return _PITCHER_CACHE[key]
    fip = None
    try:
        data = api.get(f"{STATSAPI}/people/{pid}/stats",
                       params={"stats": "season", "group": "pitching", "season": season})
        splits = ((data or {}).get("stats") or [{}])[0].get("splits") or []
        if splits:
            fip = fip_from_stat(splits[0].get("stat") or {})
    except Exception:  # noqa: BLE001
        fip = None
    _PITCHER_CACHE[key] = fip
    return fip


def starter_factor(api, abbr: str | None, date: str, season: int) -> float | None:
    """Run-prevention factor for a team's probable starter on a date, or None."""
    if not abbr or not date or not season:
        return None
    pid = _probables(api, date).get(abbr)
    if not pid:
        return None
    return pitcher_factor(_pitcher_fip(api, pid, season))
