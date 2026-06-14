#!/usr/bin/env python3
"""Automatic league-baseline calibration from a results feed (football-data.org).

The Dixon-Coles model anchors every game on its league's average total goals per
game (`leagues.LEAGUE_BASELINES`). Those are best-effort static snapshots; a wrong
baseline biases *every* game in that league the same way (systematic fake edge).

This module recalculates each league's baseline from the current season's FINISHED
matches, so the anchor tracks the live scoring environment. It mirrors
`soccer_results.py`: free football-data.org key via `X-Auth-Token`, lazy `requests`,
fully best-effort — no key / offline / too few matches all fall back to the static
baseline (never fabricates a number). Pure parsing is isolated for offline tests.
"""

from __future__ import annotations

import os

import leagues
import apifootball_source as apif

FOOTBALL_DATA_API = "https://api.football-data.org/v4"

# Min FINISHED matches before a calibrated average is trusted (else: too noisy ->
# keep the static baseline). The World Cup plays far fewer games, so it gets a
# lower floor; mid-tournament its baseline is noisy regardless.
MIN_MATCHES_DEFAULT = 30
MIN_MATCHES_BY_PREFIX: dict[str, int] = {
    "fifwc": 16, "world-cup": 16, "wc": 16, "euro": 12, "eur": 12,
}

# Our league prefix -> football-data.org competition code (free-tier coverage).
LEAGUE_FD_CODE: dict[str, str] = {
    "epl": "PL", "premier-league": "PL",
    "laliga": "PD", "la-liga": "PD",
    "seriea": "SA", "serie-a": "SA",
    "bundesliga": "BL1",
    "ligue1": "FL1", "ligue-1": "FL1",
    "eredivisie": "DED",
    "primeira": "PPL", "liga-portugal": "PPL",
    "ucl": "CL", "champions-league": "CL",
    "fifwc": "WC", "world-cup": "WC", "wc": "WC",
    "brasileirao": "BSA", "brasil": "BSA",
    # football-data.org free tier also exposes ELC (Championship) and EC (Euro).
    "euro": "EC", "eur": "EC",
}


def fd_code(prefix: str | None) -> str | None:
    return LEAGUE_FD_CODE.get((prefix or "").strip().lower())


def min_matches(prefix: str | None) -> int:
    return MIN_MATCHES_BY_PREFIX.get((prefix or "").strip().lower(), MIN_MATCHES_DEFAULT)


# ---------------------------------------------------------------------------
# Pure: average total goals from a football-data.org /matches payload
# ---------------------------------------------------------------------------


def avg_goals_from_matches(data: dict) -> tuple[float | None, int]:
    """(avg_total_goals, n_finished) from a /competitions/{code}/matches payload.

    Counts only FINISHED/AWARDED games with both full-time scores present.
    """
    total, n = 0.0, 0
    for m in (data or {}).get("matches", []):
        if (m.get("status") or "").upper() not in ("FINISHED", "AWARDED"):
            continue
        ft = (m.get("score", {}) or {}).get("fullTime", {}) or {}
        hg, ag = ft.get("home"), ft.get("away")
        if hg is None or ag is None:
            continue
        total += float(hg) + float(ag)
        n += 1
    return (total / n if n else None), n


# ---------------------------------------------------------------------------
# Fetch (network; best-effort)
# ---------------------------------------------------------------------------


def fetch_league_baseline(prefix: str | None, token: str | None, *,
                          season: int | None = None, timeout: int = 8,
                          min_n: int | None = None) -> float | None:
    """Calibrated average total goals for a league, or None to keep the static value."""
    code = fd_code(prefix)
    if not code or not token:
        return None
    params = {"status": "FINISHED"}
    if season is not None:
        params["season"] = season
    try:
        import requests  # lazy
        resp = requests.get(f"{FOOTBALL_DATA_API}/competitions/{code}/matches",
                            params=params, headers={"X-Auth-Token": token}, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception:  # noqa: BLE001
        return None
    avg, n = avg_goals_from_matches(data)
    floor = min_n if min_n is not None else min_matches(prefix)
    if avg is None or n < floor:
        return None
    return round(avg, 3)


def calibrate_baselines(prefixes, token: str | None = None, *, season: int | None = None,
                        date: str | None = None, timeout: int = 8,
                        debug: bool = False) -> dict[str, float]:
    """Map {league_prefix -> calibrated baseline} for the prefixes that succeed.

    Per league: try football-data.org (current-season finished matches); for leagues
    it doesn't cover (e.g. Série B) fall back to API-Football's league table. Prefixes
    that neither source resolves are omitted (caller uses leagues.league_baseline).
    """
    token = token or os.environ.get("FOOTBALL_DATA_TOKEN")
    out: dict[str, float] = {}
    seen_codes: dict[str, float] = {}
    for p in {(x or "").strip().lower() for x in prefixes if x}:
        code, val, src = fd_code(p), None, None
        if code and token:
            if code in seen_codes:  # alias of an already-fetched competition
                out[p] = seen_codes[code]
                continue
            val = fetch_league_baseline(p, token, season=season, timeout=timeout)
            if val is not None:
                src = "football-data"
        if val is None:  # football-data.org gap -> API-Football league table
            val = apif.league_baseline(p, date)
            if val is not None:
                src = "api-football"
        if val is not None:
            out[p] = val
            if code:
                seen_codes[code] = val
            if debug:
                print(f"  [baseline] {p}: calibrated {val:.2f} via {src} "
                      f"(static {leagues.LEAGUE_BASELINES.get(p, leagues.DEFAULT_BASELINE):.2f})")
    return out


def baseline_for(slug: str, calibrated: dict[str, float] | None) -> float:
    """Calibrated baseline for a slug's league if available, else the static one."""
    if calibrated:
        p = leagues.league_prefix(slug)
        if p in calibrated:
            return calibrated[p]
    return leagues.league_baseline(slug)
