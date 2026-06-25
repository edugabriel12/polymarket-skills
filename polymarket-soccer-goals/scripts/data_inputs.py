"""External data adapters for the soccer goal model.

The model needs, per match, an expected total goals and a home supremacy. Inputs
are resolved with this precedence (each best-effort; failures degrade):
  1. ratings CSV (--ratings-csv, ToS-clean, explicit override)
  2. xG via the optional `soccerdata` library (FBref/Understat; ToS-flagged)
  3. Elo — NATIONAL-team Elo for international games (World Cup), Club Elo for club
     leagues (ratings_sources.py)
If nothing resolves, get_match_inputs returns {} and the pipeline falls back to the
market-implied model (zero edge — never fabricates a signal).

Pure stdlib except the optional, lazily-imported soccerdata/requests in
ratings_sources. See references/data-sources.md.
"""

from __future__ import annotations

import csv
import os
import sqlite3

import ratings_sources as rs
import apifootball_source as apif


# ---------------------------------------------------------------------------
# First-trade detection (shared predictions DB)
# ---------------------------------------------------------------------------


def is_first_trade(strategy: str, portfolio_db: str | None) -> bool:
    if not portfolio_db or not os.path.exists(portfolio_db):
        return True
    try:
        con = sqlite3.connect(portfolio_db)
        try:
            cur = con.execute("SELECT COUNT(*) FROM predictions WHERE strategy LIKE ?",
                              (f"%{strategy}%",))
            (count,) = cur.fetchone()
            return count == 0
        finally:
            con.close()
    except sqlite3.Error:
        return True


# ---------------------------------------------------------------------------
# Ratings CSV (offline-capable, ToS-clean override)
# ---------------------------------------------------------------------------


def load_ratings(csv_path: str) -> dict[str, dict]:
    """Per-team ratings from a CSV. team key = 'team'/'abbr' (lowercase).

    Columns (case-insensitive): `elo`, and/or `att_factor`/`def_factor` (1.0 =
    league average). Returns {abbr: {elo?, att_factor?, def_factor?}}.
    """
    out: dict[str, dict] = {}
    with open(csv_path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            r = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            abbr = (r.get("team") or r.get("abbr") or "").lower()
            if not abbr:
                continue
            rec = {}
            for key in ("elo", "att_factor", "def_factor"):
                if r.get(key):
                    try:
                        rec[key] = float(r[key])
                    except ValueError:
                        pass
            if rec:
                out[abbr] = rec
    return out


# ---------------------------------------------------------------------------
# Assemble per-match inputs (precedence: CSV > xG > Elo)
# ---------------------------------------------------------------------------


def get_match_inputs(api, home_abbr: str | None, away_abbr: str | None,
                     league_prefix: str | None, *, ratings: dict | None = None,
                     international: bool = False, auto: bool = True,
                     date: str | None = None, debug: bool = False,
                     home_name: str | None = None, away_name: str | None = None) -> dict:
    """Return model inputs for a match, or {} to trigger the zero-edge fallback.

    `home_name`/`away_name` are the FULL club names (from discovery), used to resolve strength
    by name across leagues — far more reliable than the 3-letter slug code alone.

    Keys: home_elo, away_elo, att_home, def_home, att_away, def_away,
    total_xg, supremacy_xg.
    """
    inputs: dict = {}
    ratings = ratings or {}
    h = ratings.get((home_abbr or "").lower(), {})
    a = ratings.get((away_abbr or "").lower(), {})

    # 1. CSV override (explicit user input wins).
    if "elo" in h:
        inputs["home_elo"] = h["elo"]
    if "elo" in a:
        inputs["away_elo"] = a["elo"]
    for src, side in ((h, "home"), (a, "away")):
        if "att_factor" in src:
            inputs[f"att_{side}"] = src["att_factor"]
        if "def_factor" in src:
            inputs[f"def_{side}"] = src["def_factor"]
    if inputs:
        return inputs

    if not auto:
        return {}

    # 2. xG (best-effort via soccerdata).
    if home_abbr and away_abbr:
        xg = rs.fetch_team_xg(home_abbr, away_abbr, league_prefix or "", debug=debug)
        if xg:
            return xg

    # 2.5 API-Football season attack/defense (club leagues; covers e.g. Série B,
    # which Club Elo lacks). Matched by full name when available. total_xg + supremacy_xg.
    if not international and home_abbr and away_abbr:
        af = apif.team_inputs(home_abbr, away_abbr, league_prefix, date,
                              home_name=home_name, away_name=away_name)
        if af:
            if not debug:
                af.pop("_resolved", None)
            return af

    # 3. Elo — national teams for international games, Club Elo for club leagues.
    if international:
        eh, ea = rs.national_elo(home_abbr), rs.national_elo(away_abbr)
    else:
        eh = rs.fetch_club_elo(home_abbr, name=home_name)
        ea = rs.fetch_club_elo(away_abbr, name=away_name)
    if eh is not None:
        inputs["home_elo"] = eh
    if ea is not None:
        inputs["away_elo"] = ea
    return inputs
