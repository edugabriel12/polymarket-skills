#!/usr/bin/env python3
"""External data adapters for the MLB total-runs model (full pipeline).

This is the ONLY module that reaches beyond Polymarket. Every layer is
best-effort and degrades independently: if a source is unavailable (the sandbox
blocks egress) the corresponding factor is simply omitted. If NOTHING resolves,
`get_game_inputs` returns an empty dict and the pipeline falls back to the
market-implied mean (zero edge) — it never fabricates a signal.

Layers (per the approved plan):
  - MLB Stats API (statsapi.mlb.com) — schedule + probable pitchers (free, no auth)
  - Projections / season retrospect — team run-rate + pitcher factors, via a
    ToS-clean CSV (`--projections-csv`); see references/data-sources.md for schema
  - Weather (api.weather.gov) — temperature + wind for US parks
  - Home/away — small home-field run delta (only when a real game is matched)

INCLUDED features: home/away (park + home_field) and season retrospect (run rates).
EXCLUDED by design: short-term recent form and head-to-head team records (noise).

ToS: MLB Stats API / Statcast data is MLBAM-copyrighted and restricts commercial/
bulk use; a betting model is plausibly commercial. Prefer a licensed feed for
production. See references/data-sources.md.
"""

from __future__ import annotations

import csv
import os
import sqlite3

import park_factors as pf
import ballparks

STATSAPI = "https://statsapi.mlb.com/api/v1"
NWS_API = "https://api.weather.gov"

LEAGUE_RPG = 4.25  # league-average runs per game per team, for rate->factor conversion
HOME_FIELD_DELTA = 0.10  # small home run-environment nudge when a real game is matched
NON_NWS_PARKS = {"tor"}  # parks outside the US NWS coverage (skip weather there)


# ---------------------------------------------------------------------------
# First-trade detection (for the 1% new-strategy cap)
# ---------------------------------------------------------------------------


def is_first_trade(strategy: str, portfolio_db: str | None) -> bool:
    """True if this strategy has no prior trades in the paper DB (or no DB given).

    Conservative by default: with no DB to consult, assume first trade -> 1% cap.
    """
    if not portfolio_db or not os.path.exists(portfolio_db):
        return True
    try:
        con = sqlite3.connect(portfolio_db)
        try:
            cur = con.execute(
                "SELECT COUNT(*) FROM trades WHERE reasoning LIKE ?",
                (f"%{strategy}%",),
            )
            (count,) = cur.fetchone()
            return count == 0
        finally:
            con.close()
    except sqlite3.Error:
        return True


# ---------------------------------------------------------------------------
# Projections CSV (ToS-clean run-rate source) — offline-capable
# ---------------------------------------------------------------------------


def load_projection_factors(csv_path: str) -> dict[str, dict]:
    """Load per-team {off_factor, pitch_factor} from a CSV (1.0 = league average).

    Accepted columns (case-insensitive); team key is 'team' or 'abbr':
      - off_factor / pitch_factor   (already relative to league, 1.0 neutral), OR
      - rs_per_game / ra_per_game   (raw rates; converted with LEAGUE_RPG)
    Returns {abbr: {"off_factor": f, "pitch_factor": f}}.
    """
    table: dict[str, dict] = {}
    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            r = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            abbr = r.get("team") or r.get("abbr")
            if not abbr:
                continue
            off = _factor_from_row(r, "off_factor", "rs_per_game")
            pit = _factor_from_row(r, "pitch_factor", "ra_per_game")
            if off is None and pit is None:
                continue
            table[abbr.strip().lower()] = {
                "off_factor": off if off is not None else 1.0,
                "pitch_factor": pit if pit is not None else 1.0,
            }
    return table


def _factor_from_row(row, factor_key, rate_key):
    if row.get(factor_key):
        try:
            return float(row[factor_key])
        except ValueError:
            return None
    if row.get(rate_key):
        try:
            return float(row[rate_key]) / LEAGUE_RPG
        except (ValueError, ZeroDivisionError):
            return None
    return None


# ---------------------------------------------------------------------------
# MLB Stats API + weather (network; best-effort, isolated)
# ---------------------------------------------------------------------------


def fetch_probables(api, target_date: str, debug: bool = False) -> list[dict]:
    """Schedule + probable pitchers for a date. [] on any failure (e.g. sandbox)."""
    try:
        data = api.get(f"{STATSAPI}/schedule",
                       params={"sportId": 1, "date": target_date,
                               "hydrate": "probablePitcher,team"})
    except Exception:  # noqa: BLE001 - network blocked -> graceful empty
        return []
    games = []
    for d in (data or {}).get("dates", []):
        games.extend(d.get("games", []))
    return games


def fetch_weather(api, lat: float, lon: float, debug: bool = False) -> dict:
    """Temperature (F) and wind speed (mph) for a park. {} on failure."""
    try:
        point = api.get(f"{NWS_API}/points/{lat},{lon}")
        url = point["properties"]["forecastHourly"]
        fc = api.get(url)
        period = fc["properties"]["periods"][0]
        temp_f = float(period.get("temperature")) if period.get("temperatureUnit") == "F" else None
        wind = period.get("windSpeed", "")  # e.g. "10 mph"
        wind_mph = float(wind.split()[0]) if wind and wind.split()[0].isdigit() else None
        out = {}
        if temp_f is not None:
            out["temp_f"] = temp_f
        if wind_mph is not None:
            # Direction-agnostic without park orientation; treat as out only at modest weight.
            out["wind_out_mph"] = wind_mph
        return out
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------
# Assemble per-game factors
# ---------------------------------------------------------------------------


def get_game_inputs(api, event_slug: str, target_date: str, *,
                    projections_csv: str | None = None, debug: bool = False) -> dict:
    """Return adjust_mu kwargs for a game, or {} to trigger the zero-edge fallback.

    Keys (only those resolved): home_off, away_off, home_sp, away_sp, home_field,
    temp_f, wind_out_mph.
    """
    away, home = pf.parse_slug_teams(event_slug)
    inputs: dict = {}

    # Season retrospect via projections CSV (offline-capable, ToS-clean).
    if projections_csv and away and home:
        try:
            table = load_projection_factors(projections_csv)
        except OSError:
            table = {}
        a = table.get(_canon(away))
        h = table.get(_canon(home))
        if h:
            inputs["home_off"] = h["off_factor"]
            inputs["home_sp"] = h["pitch_factor"]
        if a:
            inputs["away_off"] = a["off_factor"]
            inputs["away_sp"] = a["pitch_factor"]
        if h or a:
            inputs["home_field"] = HOME_FIELD_DELTA  # casa/fora nudge

    # Weather (network; best-effort). NWS only covers US parks, so skip parks
    # outside its coverage (e.g. Toronto / Rogers Centre) to avoid 404s.
    coords = pf.home_coords_for_slug(event_slug)
    if coords and _canon(home or "") not in NON_NWS_PARKS:
        wx = fetch_weather(api, coords[0], coords[1], debug=debug)
        inputs.update(wx)

    # MLB Stats API probables are fetched to confirm the game/starters exist;
    # starter-quality factors require a projections source, so probables alone
    # do not add a factor here (documented). The call still validates matchup.
    if debug:
        _ = fetch_probables(api, target_date, debug=debug)

    return inputs


def _canon(abbr: str) -> str:
    return ballparks.ALIASES.get((abbr or "").lower(), (abbr or "").lower())
