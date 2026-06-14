#!/usr/bin/env python3
"""API-Football (api-sports.io) adapter: automatic attack/defense from league data.

Covers leagues that Club Elo / football-data.org don't (e.g. Brasileirão Série B).
A single `standings` request per league returns every team's season goals for/against,
which we turn into expected goals + home supremacy for the Dixon-Coles model:

  league_avg = goals per team per game (league-wide)
  att = team goals-for/game  / league_avg     (1.0 = average attack)
  def = team goals-against/game / league_avg   (1.0 = average defense)
  exp_home = league_avg * att_home * def_away * home_tilt
  exp_away = league_avg * att_away * def_home / home_tilt
  -> total = exp_home + exp_away ; supremacy = exp_home - exp_away

Best-effort and offline-safe: no APIFOOTBALL_KEY, network failure, an uncovered
league, or unresolved team names all return {} / None, so the pipeline degrades to
Elo / market-implied (never fabricates an edge). Pure math is split out for tests.
"""

from __future__ import annotations

import os
import unicodedata
from datetime import datetime, timezone

import leagues  # noqa: F401  (kept for symmetry / future league lookups)

APIFOOTBALL_API = "https://v3.football.api-sports.io"

# Home tilt: equal teams -> ~+0.25 goal home edge at a 2.5 total (f - 1/f ≈ 0.21).
HOME_TILT_DEFAULT = 1.105

# Min matches played per team before the league table is trusted (else too noisy).
MIN_PLAYED_DEFAULT = 5

# Our league prefix -> API-Football league id.
LEAGUE_API_ID: dict[str, int] = {
    "bra2": 72, "serie-b": 72,                      # Brasileirão Série B
    "brasileirao": 71, "brasil": 71,                # Série A
    "epl": 39, "premier-league": 39,
    "laliga": 140, "la-liga": 140,
    "seriea": 135, "serie-a": 135,
    "bundesliga": 78,
    "ligue1": 61, "ligue-1": 61,
    "eredivisie": 88,
    "primeira": 94, "liga-portugal": 94,
    "mls": 253,
}

# Cross-year (European) leagues: season label is the starting year.
EURO_CROSS_YEAR = {
    "epl", "premier-league", "laliga", "la-liga", "seriea", "serie-a",
    "bundesliga", "ligue1", "ligue-1", "eredivisie", "primeira", "liga-portugal",
}

# Module-level cache so multiple games in one league cost one request per run.
_STANDINGS_CACHE: dict[tuple, list] = {}


def api_league_id(prefix: str | None) -> int | None:
    return LEAGUE_API_ID.get((prefix or "").strip().lower())


def season_for(prefix: str | None, date: str | None) -> int:
    """API-Football season label for a league + game date (calendar vs cross-year)."""
    try:
        year = int((date or "")[:4]) if (date or "")[:4].isdigit() else None
    except ValueError:
        year = None
    if year is None:
        year = datetime.now(timezone.utc).year
    if (prefix or "").strip().lower() in EURO_CROSS_YEAR:
        try:
            month = int((date or "")[5:7])
        except (ValueError, IndexError):
            month = 1
        return year if month >= 7 else year - 1
    return year


# ---------------------------------------------------------------------------
# Team-name resolution (Polymarket abbr -> API-Football team name)
# ---------------------------------------------------------------------------


def _norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(c for c in s.lower() if c.isalnum())


def _acronym(name: str) -> str:
    return _norm("".join(w[0] for w in (name or "").split() if w))


def match_team(code: str | None, names) -> str | None:
    """Resolve a Polymarket 3-letter code to a team name, or None if ambiguous.

    Tries, in order: unique normalized-name prefix, unique acronym, unique
    substring. Anything ambiguous returns None (caller falls back — no wrong data).
    """
    c = _norm(code)
    if not c:
        return None
    names = list(names)
    pref = [n for n in names if _norm(n).startswith(c)]
    if len(pref) == 1:
        return pref[0]
    acr = [n for n in names if _acronym(n) == c]
    if len(acr) == 1:
        return acr[0]
    if len(pref) > 1:           # ambiguous prefix -> don't guess
        return None
    cont = [n for n in names if c in _norm(n)]
    if len(cont) == 1:
        return cont[0]
    return None


# ---------------------------------------------------------------------------
# Pure: parse standings -> per-team table + league average
# ---------------------------------------------------------------------------


def parse_standings(data: dict) -> list[dict]:
    """[{name, gf, ga, played}] from an API-Football /standings payload."""
    rows: list[dict] = []
    for resp in (data or {}).get("response", []):
        for group in ((resp.get("league", {}) or {}).get("standings") or []):
            for t in group:
                name = (t.get("team", {}) or {}).get("name")
                allg = t.get("all", {}) or {}
                played = allg.get("played")
                g = allg.get("goals", {}) or {}
                gf, ga = g.get("for"), g.get("against")
                if name and played and gf is not None and ga is not None and int(played) > 0:
                    rows.append({"name": name, "gf": float(gf), "ga": float(ga),
                                 "played": int(played)})
    return rows


def table_from_rows(rows: list[dict], min_played: int = MIN_PLAYED_DEFAULT):
    """(table {name: {gf_pg, ga_pg}}, league_avg_per_team_per_game) or (None, None)."""
    if not rows:
        return None, None
    total_goals = sum(r["gf"] for r in rows)
    total_played = sum(r["played"] for r in rows)
    if total_played <= 0 or (total_played / len(rows)) < min_played:
        return None, None
    league_avg = total_goals / total_played  # goals per team per game
    if league_avg <= 0:
        return None, None
    table = {r["name"]: {"gf_pg": r["gf"] / r["played"], "ga_pg": r["ga"] / r["played"]}
             for r in rows}
    return table, league_avg


def compute_inputs(table: dict, league_avg: float, home: str | None, away: str | None,
                   home_tilt: float = HOME_TILT_DEFAULT) -> dict:
    """Expected total + home supremacy from the league table, or {} if teams unresolved."""
    hm, am = match_team(home, table.keys()), match_team(away, table.keys())
    if not hm or not am or not league_avg:
        return {}
    h, a = table[hm], table[am]
    att_h, def_h = h["gf_pg"] / league_avg, h["ga_pg"] / league_avg
    att_a, def_a = a["gf_pg"] / league_avg, a["ga_pg"] / league_avg
    exp_home = league_avg * att_h * def_a * home_tilt
    exp_away = league_avg * att_a * def_h / home_tilt
    return {"total_xg": exp_home + exp_away, "supremacy_xg": exp_home - exp_away,
            "_resolved": [hm, am]}


# ---------------------------------------------------------------------------
# Fetch (network; best-effort, cached per run)
# ---------------------------------------------------------------------------


def _fetch_standings_rows(league_id: int, season: int, key: str, timeout: int = 8) -> list:
    cache_key = (league_id, season)
    if cache_key in _STANDINGS_CACHE:
        return _STANDINGS_CACHE[cache_key]
    rows: list = []
    try:
        import requests  # lazy
        resp = requests.get(f"{APIFOOTBALL_API}/standings",
                            params={"league": league_id, "season": season},
                            headers={"x-apisports-key": key}, timeout=timeout)
        resp.raise_for_status()
        rows = parse_standings(resp.json())
    except Exception:  # noqa: BLE001
        rows = []
    _STANDINGS_CACHE[cache_key] = rows
    return rows


def _resolve_table(prefix: str | None, date: str | None, key: str | None, timeout: int):
    key = key or os.environ.get("APIFOOTBALL_KEY")
    league_id = api_league_id(prefix)
    if not key or not league_id:
        return None, None
    rows = _fetch_standings_rows(league_id, season_for(prefix, date), key, timeout)
    return table_from_rows(rows)


def team_inputs(home: str | None, away: str | None, prefix: str | None, date: str | None,
                key: str | None = None, home_tilt: float = HOME_TILT_DEFAULT,
                timeout: int = 8) -> dict:
    """Model inputs (total_xg, supremacy_xg) for a match from API-Football, or {}."""
    table, league_avg = _resolve_table(prefix, date, key, timeout)
    if not table:
        return {}
    return compute_inputs(table, league_avg, home, away, home_tilt)


def league_baseline(prefix: str | None, date: str | None, key: str | None = None,
                    timeout: int = 8) -> float | None:
    """Average TOTAL goals per game for the league (2x per-team avg), or None."""
    _table, league_avg = _resolve_table(prefix, date, key, timeout)
    if not league_avg:
        return None
    return round(2.0 * league_avg, 3)
