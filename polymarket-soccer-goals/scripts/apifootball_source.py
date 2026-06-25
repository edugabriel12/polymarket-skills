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
import re
import sys
import unicodedata
from datetime import datetime, timezone

import leagues  # noqa: F401  (kept for symmetry / future league lookups)

APIFOOTBALL_API = "https://v3.football.api-sports.io"

# Diagnostic dedupe so the strength path is auditable without spam: warn once per league when
# the key is missing, log the key-present state once, and log each league's standings result once.
_KEY_WARNED: set = set()
_KEY_OK_LOGGED: list = [False]
_LEAGUE_LOGGED: set = set()


def _log(msg: str) -> None:
    print(f"  [apifootball] {msg}", file=sys.stderr, flush=True)

# Home tilt: equal teams -> ~+0.25 goal home edge at a 2.5 total (f - 1/f ≈ 0.21).
HOME_TILT_DEFAULT = 1.105

# Min matches played per team before the league table is trusted (else too noisy).
MIN_PLAYED_DEFAULT = 5

# Our league prefix -> API-Football league id (api-football.com). Keyed by every
# plausible event-slug prefix so auto attack/defense ratings resolve regardless of
# which form Polymarket uses.
LEAGUE_API_ID: dict[str, int] = {
    # England
    "epl": 39, "premier-league": 39,
    "efl": 40, "championship": 40, "elc": 40,
    "eng1": 41, "league-one": 41,
    "eng2": 42, "league-two": 42, "efl2": 42,
    # Spain
    "laliga": 140, "la-liga": 140,
    "es2": 141, "laliga2": 141, "segunda": 141,
    # Italy
    "seriea": 135, "serie-a": 135, "sea": 135, "bkseriea": 135,
    "serieb": 136, "it2": 136,
    # Germany
    "bundesliga": 78, "bund": 78, "ger": 78,
    "bundesliga2": 79, "ger2": 79,
    # France
    "ligue1": 61, "ligue-1": 61,
    "ligue2": 62, "fr2": 62,
    # Netherlands / Portugal
    "eredivisie": 88, "ned": 88,
    "primeira": 94, "liga-portugal": 94, "por": 94,
    # Americas
    "mls": 253,
    "ligamx": 262, "liga-mx": 262, "mex": 262,
    "brasileirao": 71, "brasil": 71, "bra": 71,     # Série A
    "bra2": 72, "serie-b": 72,                       # Série B
    "argentina": 128, "arg": 128,
    "colombia": 239, "col": 239,
    "chile": 265, "chi": 265,
    "bolivia": 344, "bol": 344,
    "ecuador": 242, "ecu": 242,
    "peru": 281, "per": 281,
    "uruguay": 268, "uru": 268,
    "paraguay": 250, "par": 250,
    # Rest of Europe
    "allsvenskan": 113, "swe": 113,
    "eliteserien": 103, "nor": 103,
    "superlig": 203, "tur": 203,
    "belgium": 144, "bel": 144, "jpl": 144,
    "spfl": 179, "scotland": 179, "sco": 179,
    "swiss": 207, "sui": 207,
    "austria": 218, "aut": 218,
    "denmark": 119, "den": 119,
    "greece": 197, "gre": 197,
    "rpl": 235, "russia": 235,
    "mar1": 200, "morocco": 200, "botola": 200,     # Morocco Botola Pro
    # Asia
    "csl": 169, "china": 169,
    "jleague": 98, "j1": 98, "jpn": 98,
    "kleague": 292, "k1": 292, "kor": 292,
    "saudi": 307, "spl": 307, "ksa": 307,
    # Continental clubs
    "ucl": 2, "champions-league": 2, "bkcl": 2,
    "uel": 3, "europa-league": 3,
    "uecl": 848, "conference": 848,
    "libertadores": 13, "copa-libertadores": 13,
    "sudamericana": 11, "copa-sudamericana": 11,
}

# Cross-year (mostly European) leagues: season label is the starting year. Calendar-year
# leagues (Brazil, MLS, Nordics, Asia, internationals) are intentionally absent.
EURO_CROSS_YEAR = {
    "epl", "premier-league", "efl", "championship", "elc", "eng1", "league-one",
    "eng2", "league-two", "efl2", "laliga", "la-liga", "es2", "laliga2", "segunda",
    "seriea", "serie-a", "sea", "bkseriea", "serieb", "it2",
    "bundesliga", "bund", "ger", "bundesliga2", "ger2", "ligue1", "ligue-1", "ligue2", "fr2",
    "eredivisie", "ned", "primeira", "liga-portugal", "por",
    "superlig", "tur", "belgium", "bel", "jpl", "spfl", "scotland", "sco",
    "swiss", "sui", "austria", "aut", "denmark", "den", "greece", "gre", "rpl", "russia",
    "mar1", "morocco", "botola",
    "ucl", "champions-league", "bkcl", "uel", "europa-league", "uecl", "conference",
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


def _sig_tokens(name: str | None) -> set:
    """Distinctive word tokens (≥4 chars, accent-stripped) of a club name.

    Used to bridge abbreviation vs full-name forms that share the club's core word —
    'IR Tanger' vs 'Ittihad Tanger' both yield {'tanger'}; 'RS Berkane' vs 'Renaissance
    Berkane' both yield {'berkane'} (+others). Short connective tokens (rs, as, ir, fc) drop out.
    """
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return {t for t in re.split(r"[^a-z0-9]+", s) if len(t) >= 4}


def match_team(code: str | None, names, name: str | None = None) -> str | None:
    """Resolve a team to a standings name, or None if ambiguous.

    The full club `name` (from the market/sharp slate, already club-suffix-normalized) is the
    most reliable signal and is tried FIRST — exact, then prefix-either-way, then substring,
    each accepted only when unique. Falling back to the 3-letter `code` (unique prefix /
    acronym / substring). Anything ambiguous returns None (caller falls back — no wrong data).
    """
    names = list(names)
    q = _norm(name)
    if q:
        exact = [n for n in names if _norm(n) == q]
        if len(exact) == 1:
            return exact[0]
        pref = [n for n in names if _norm(n).startswith(q) or q.startswith(_norm(n))]
        if len(pref) == 1:
            return pref[0]
        cont = [n for n in names if q in _norm(n) or _norm(n) in q]
        if len(cont) == 1:
            return cont[0]
    c = _norm(code)
    if not c:
        return None
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
    # Last resort: a shared distinctive word between the full name and a standings name
    # (bridges 'IR Tanger' <-> 'Ittihad Tanger', 'RS Berkane' <-> 'Renaissance Berkane').
    qsig = _sig_tokens(name)
    if qsig:
        shared = [n for n in names if qsig & _sig_tokens(n)]
        if len(shared) == 1:
            return shared[0]
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
                   home_tilt: float = HOME_TILT_DEFAULT,
                   home_name: str | None = None, away_name: str | None = None) -> dict:
    """Expected total + home supremacy from the league table, or {} if teams unresolved."""
    hm = match_team(home, table.keys(), home_name)
    am = match_team(away, table.keys(), away_name)
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
    if not league_id:
        return None, None
    if not key:
        # A covered club league was requested but no key is set — the single config step
        # that gives these games a strength model. Warn once per league so external=False
        # is self-diagnosable from the log instead of silent.
        p = (prefix or "").strip().lower()
        if p and p not in _KEY_WARNED:
            _KEY_WARNED.add(p)
            _log(f"APIFOOTBALL_KEY not set — no strength model for '{p}' (league {league_id}); "
                 f"set the env var to enable it")
        return None, None
    if not _KEY_OK_LOGGED[0]:                 # confirm once that the key is configured
        _KEY_OK_LOGGED[0] = True
        _log("APIFOOTBALL_KEY is set — strength model enabled")
    season = season_for(prefix, date)
    rows = _fetch_standings_rows(league_id, season, key, timeout)
    lk = (league_id, season)
    if lk not in _LEAGUE_LOGGED:              # which leagues return a table (and how big), once each
        _LEAGUE_LOGGED.add(lk)
        _log(f"{prefix} (league {league_id}, season {season}): {len(rows)} standings row(s)"
             + ("" if rows else " — no data (off-season, or league/season not covered)"))
    return table_from_rows(rows)


def team_inputs(home: str | None, away: str | None, prefix: str | None, date: str | None,
                key: str | None = None, home_tilt: float = HOME_TILT_DEFAULT,
                timeout: int = 8, home_name: str | None = None,
                away_name: str | None = None) -> dict:
    """Model inputs (total_xg, supremacy_xg) for a match from API-Football, or {}."""
    table, league_avg = _resolve_table(prefix, date, key, timeout)
    if not table:
        return {}
    out = compute_inputs(table, league_avg, home, away, home_tilt, home_name, away_name)
    # Which games resolve strength (and to which standings names) vs which don't.
    if out.get("_resolved"):
        h, a = out["_resolved"]
        _log(f"{prefix}: {h} vs {a} -> total_xg={out['total_xg']:.2f} "
             f"sup={out['supremacy_xg']:+.2f}")
    else:
        _log(f"{prefix}: UNRESOLVED {home_name or home!r} / {away_name or away!r} "
             f"in the {len(table)}-team table -> no strength model")
    return out


def league_baseline(prefix: str | None, date: str | None, key: str | None = None,
                    timeout: int = 8) -> float | None:
    """Average TOTAL goals per game for the league (2x per-team avg), or None."""
    _table, league_avg = _resolve_table(prefix, date, key, timeout)
    if not league_avg:
        return None
    return round(2.0 * league_avg, 3)
