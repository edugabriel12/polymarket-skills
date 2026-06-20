#!/usr/bin/env python3
"""Automatic surface-aware Elo ratings, computed from Jeff Sackmann's match data.

The deterministic core — `build_elo_from_matches` — walks the match history forward
in time and maintains an overall Elo plus per-surface Elo for every player, using the
engine in elo.py (FiveThirtyEight dynamic K-factor). It is pure and offline-testable.

The network layer fetches Sackmann's CSVs from GitHub (`tennis_atp`/`tennis_wta`,
CC BY-NC-SA — non-commercial; see references/deep-research.md) with a lazy `requests`
import; offline/blocked it returns empty and the pipeline falls back to market-implied.

Result shape: {normalized_name: {"elo", "hard", "clay", "grass"}} — directly consumable
by ratings.resolve / elo.blended_elo.
"""

from __future__ import annotations

import json
import os
import time

import elo as _elo
from ratings import normalize

RAW_BASE = "https://raw.githubusercontent.com/JeffSackmann"
_CACHE_DIR = os.path.expanduser("~/.polymarket-tennis")
_SURFACE_MAP = {"hard": "hard", "clay": "clay", "grass": "grass", "carpet": "hard"}


def parse_match_row(row: dict) -> dict | None:
    """Pick (date, surface, winner, loser) from a Sackmann match CSV row."""
    w, l = row.get("winner_name"), row.get("loser_name")
    if not w or not l:
        return None
    surf = _SURFACE_MAP.get((row.get("surface") or "").strip().lower())
    return {"date": (row.get("tourney_date") or "").strip(), "surface": surf,
            "winner": w.strip(), "loser": l.strip()}


def build_elo_from_matches(matches: list[dict]) -> dict:
    """Walk-forward Elo: overall + per-surface ratings per player. Pure/deterministic.

    `matches`: dicts with date, surface ('hard'/'clay'/'grass'/None), winner, loser.
    Processed in the given order (caller sorts chronologically). No look-ahead: every
    rating used in a match's expectation predates that match's update.
    """
    elo_all: dict[str, float] = {}
    n_all: dict[str, int] = {}
    elo_surf: dict[str, dict[str, float]] = {s: {} for s in _elo.SURFACES}
    n_surf: dict[str, dict[str, int]] = {s: {} for s in _elo.SURFACES}

    def _step(table_e, table_n, w, l):
        ew = table_e.get(w, _elo.START_ELO)
        el = table_e.get(l, _elo.START_ELO)
        exp_w = _elo.expected(ew, el)
        table_e[w] = _elo.update(ew, _elo.k_factor(table_n.get(w, 0)), 1.0, exp_w)
        table_e[l] = _elo.update(el, _elo.k_factor(table_n.get(l, 0)), 0.0, 1.0 - exp_w)
        table_n[w] = table_n.get(w, 0) + 1
        table_n[l] = table_n.get(l, 0) + 1

    for m in matches:
        w, l = normalize(m["winner"]), normalize(m["loser"])
        if not w or not l:
            continue
        _step(elo_all, n_all, w, l)
        s = m.get("surface")
        if s in _elo.SURFACES:
            _step(elo_surf[s], n_surf[s], w, l)

    out: dict[str, dict] = {}
    for name, e in elo_all.items():
        rating = {"elo": round(e, 1)}
        for s in _elo.SURFACES:
            rating[s] = round(elo_surf[s][name], 1) if name in elo_surf[s] else None
        out[name] = rating
    return out


# ---------------------------------------------------------------------------
# Network layer (best-effort)
# ---------------------------------------------------------------------------


def fetch_sackmann_matches(tour: str, years: list[int], timeout: int = 10) -> list[dict]:
    """Fetch + parse Sackmann match CSVs for the given years. [] on any failure."""
    try:
        import csv as _csv
        import io
        import requests  # lazy
    except Exception:  # noqa: BLE001
        return []
    repo = "tennis_wta" if tour == "wta" else "tennis_atp"
    prefix = "wta" if tour == "wta" else "atp"
    rows: list[dict] = []
    for y in years:
        url = f"{RAW_BASE}/{repo}/master/{prefix}_matches_{y}.csv"
        try:
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
            for r in _csv.DictReader(io.StringIO(resp.text)):
                pm = parse_match_row(r)
                if pm:
                    rows.append(pm)
        except Exception:  # noqa: BLE001
            continue
    rows.sort(key=lambda m: m["date"])     # chronological -> no look-ahead
    return rows


def auto_ratings(tour: str = "atp", years: list[int] | None = None,
                 cache_hours: float = 24.0, debug: bool = False) -> dict:
    """Load auto Elo ratings for a tour, computing from Sackmann data (cached to disk).

    Returns {} when offline/blocked (pipeline then falls back to market-implied).
    """
    from datetime import datetime, timezone
    if years is None:
        y = datetime.now(timezone.utc).year
        years = [y - 2, y - 1, y]          # recent form window
    os.makedirs(_CACHE_DIR, exist_ok=True)
    cache = os.path.join(_CACHE_DIR, f"elo_{tour}.json")
    if os.path.exists(cache) and (time.time() - os.path.getmtime(cache)) < cache_hours * 3600:
        try:
            with open(cache, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:  # noqa: BLE001
            pass
    matches = fetch_sackmann_matches(tour, years)
    if not matches:
        if debug:
            print(f"[ratings_source] no matches fetched for {tour} {years}")
        return {}
    ratings = build_elo_from_matches(matches)
    try:
        with open(cache, "w", encoding="utf-8") as fh:
            json.dump(ratings, fh)
    except Exception:  # noqa: BLE001
        pass
    return ratings
