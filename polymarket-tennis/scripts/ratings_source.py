#!/usr/bin/env python3
"""Automatic surface-aware Elo ratings, computed from public match data.

The deterministic core — `build_elo_from_matches` — walks the match history forward
in time and maintains an overall Elo plus per-surface Elo for every player, using the
engine in elo.py (FiveThirtyEight dynamic K-factor). It is pure and offline-testable.

The match feed comes from a SOURCE CHAIN (`auto_ratings` -> `source_order()`), tried in
order until one yields matches: 'sackmann' (Jeff Sackmann's CSVs on the GitHub mirrors,
here) and 'tennisdata' (tennis-data.co.uk .xlsx in `tennis_data_source.py`, a wholly
separate host reachable where GitHub egress is blocked). Each fetcher imports `requests`
lazily; when every source is empty the pipeline falls back to market-implied.

Result shape: {normalized_name: {"elo", "hard", "clay", "grass"}}, plus unambiguous surname
aliases (`index_by_surname`) — directly consumable by ratings.resolve / elo.blended_elo. Both
feeds are non-commercial (CC BY-NC-SA / personal-use). See references/deep-research.md.
"""

from __future__ import annotations

import json
import os
import time

import elo as _elo
from ratings import normalize

GITHUB_BRANCHES = ("master", "main")     # tennis_atp uses master; try main as a fallback
# Hosts that serve the SAME Sackmann CSVs. raw.githubusercontent.com is blocked on some
# networks (returns a synthetic 404); cdn.jsdelivr.net and cdn.statically.io are independent
# CDNs on different domains that mirror the repo. Our per-year match CSVs are small (~1-3 MB),
# well under jsDelivr's 20 MB/file cap. See references/deep-research.md.
_SACKMANN_HOSTS = (
    "https://raw.githubusercontent.com/JeffSackmann/{repo}/{branch}/{file}",
    "https://cdn.jsdelivr.net/gh/JeffSackmann/{repo}@{branch}/{file}",
    "https://cdn.statically.io/gh/JeffSackmann/{repo}/{branch}/{file}",
)
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


def _surname_key(normalized_name: str) -> str:
    """The token the resolver matches on: the last surname token, dropping a trailing initial.

    'rafael nadal' -> 'nadal'; 'alcaraz c' (tennis-data 'Surname I.') -> 'alcaraz';
    'bautista agut r' -> 'agut'. Mirrors `ratings.resolve`, which keys full-name labels by
    their last token, so both name styles land on the same surname key.
    """
    toks = [t for t in normalized_name.split(" ") if t]
    if not toks:
        return ""
    if len(toks) >= 2 and len(toks[-1]) == 1:        # trailing single char = initial -> drop
        toks = toks[:-1]
    return toks[-1]


def index_by_surname(ratings: dict) -> dict:
    """Add unambiguous surname keys so surname slugs / full-name labels resolve.

    `build_elo_from_matches` keys players by their FULL display name (e.g. 'rafael nadal' or
    tennis-data's 'alcaraz c'), which a bare surname or 'Surname I.' query won't hit directly.
    This adds a surname alias for every player whose surname maps to exactly ONE player — a
    shared surname is left ambiguous (resolves to None -> market-implied), never guessed.
    """
    counts: dict[str, int] = {}
    last_map: dict[str, dict] = {}
    for name, rating in ratings.items():
        sk = _surname_key(name)
        if not sk:
            continue
        counts[sk] = counts.get(sk, 0) + 1
        last_map[sk] = rating
    out = dict(ratings)
    for sk, n in counts.items():
        if n == 1 and sk not in out:                 # unambiguous surname only
            out[sk] = last_map[sk]
    return out


# ---------------------------------------------------------------------------
# Network layer (best-effort)
# ---------------------------------------------------------------------------


def _read_year_csv(repo: str, prefix: str, year: int, data_dir: str | None,
                   timeout: int) -> tuple[str | None, str | None]:
    """Return (csv_text, error) for one season, from a local dir or GitHub.

    Order: a local `{data_dir}/{prefix}_matches_{year}.csv` (clone the Sackmann repo or
    drop the CSVs there), then GitHub raw on each branch. `error` is a short reason string
    when nothing was read, so the caller can surface the REAL cause (SSL/proxy/404/etc.).
    """
    if data_dir:
        # Accept the CSV directly in data_dir, or inside a cloned repo subfolder
        # (tennis_atp/ or tennis_wta/), so one TENNIS_DATA_DIR can hold both clones.
        fname = f"{prefix}_matches_{year}.csv"
        for cand in (os.path.join(data_dir, fname),
                     os.path.join(data_dir, repo, fname)):
            if os.path.isfile(cand):
                try:
                    with open(cand, encoding="utf-8") as fh:
                        return fh.read(), None
                except Exception as e:  # noqa: BLE001
                    return None, f"local read failed: {e}"
    try:
        import requests  # lazy
    except Exception as e:  # noqa: BLE001
        return None, f"'requests' not importable: {e}"
    last = "no response"
    fname = f"{prefix}_matches_{year}.csv"
    # Try each branch across each mirror host; first 200 wins. raw is freshest when
    # reachable, the CDNs are the fallback when raw.githubusercontent.com is blocked.
    for branch in GITHUB_BRANCHES:
        for tmpl in _SACKMANN_HOSTS:
            url = tmpl.format(repo=repo, branch=branch, file=fname)
            host = url.split("/")[2]
            try:
                resp = requests.get(url, timeout=timeout)
                if resp.status_code == 200 and resp.text:
                    return resp.text, None
                last = f"HTTP {resp.status_code} @ {host}/{branch}"
            except Exception as e:  # noqa: BLE001
                last = f"{type(e).__name__} @ {host}"
    return None, last


def fetch_sackmann_matches(tour: str, years: list[int], timeout: int = 10,
                           data_dir: str | None = None, debug: bool = False) -> list[dict]:
    """Fetch + parse Sackmann match CSVs for the given years. [] on any failure."""
    try:
        import csv as _csv
        import io
    except Exception:  # noqa: BLE001
        return []
    import sys
    repo = "tennis_wta" if tour == "wta" else "tennis_atp"
    prefix = "wta" if tour == "wta" else "atp"
    data_dir = data_dir or os.environ.get("TENNIS_DATA_DIR")
    rows: list[dict] = []
    errors: list[str] = []
    for y in years:
        text, err = _read_year_csv(repo, prefix, y, data_dir, timeout)
        if not text:
            errors.append(f"{y}: {err}")
            if debug:
                print(f"[ratings_source] {prefix}_matches_{y}.csv: {err}", file=sys.stderr)
            continue
        n0 = len(rows)
        for r in _csv.DictReader(io.StringIO(text)):
            pm = parse_match_row(r)
            if pm:
                rows.append(pm)
        if debug:
            print(f"[ratings_source] {prefix}_matches_{y}.csv: +{len(rows) - n0} matches",
                  file=sys.stderr)
    if not rows and errors:
        # Surface the REAL cause (SSL/proxy/404/requests-missing), not a guess.
        print(f"[ratings_source] {prefix} fetch failed -> {'; '.join(errors)}", file=sys.stderr)
    rows.sort(key=lambda m: m["date"])     # chronological -> no look-ahead
    return rows


# Match-feed sources, tried in order until one returns matches. 'sackmann' = the GitHub-hosted
# CSV mirrors; 'tennisdata' = tennis-data.co.uk .xlsx (a wholly separate host, reachable where
# GitHub egress is blocked). Override the order with $TENNIS_RATINGS_SOURCE (comma-separated).
_DEFAULT_SOURCE_ORDER = ("sackmann", "tennisdata")


def _fetch_from_source(source: str, tour: str, years: list[int], debug: bool) -> list[dict]:
    """Dispatch one named source to its fetcher. Unknown source -> []."""
    if source == "sackmann":
        return fetch_sackmann_matches(tour, years, debug=debug)
    if source == "tennisdata":
        import tennis_data_source as tds
        return tds.fetch_tennisdata_matches(tour, years, debug=debug)
    import sys
    print(f"[ratings_source] unknown ratings source {source!r} (skipped)", file=sys.stderr)
    return []


def source_order() -> list[str]:
    """Resolve the source chain from $TENNIS_RATINGS_SOURCE (else the default order)."""
    env = (os.environ.get("TENNIS_RATINGS_SOURCE") or "").strip()
    if env:
        order = [s.strip().lower() for s in env.split(",") if s.strip()]
        if order:
            return order
    return list(_DEFAULT_SOURCE_ORDER)


def fetch_matches(tour: str = "atp", years: list[int] | None = None, debug: bool = False) -> list:
    """Match rows via the SOURCE CHAIN (source_order); first non-empty source wins, [] if all dry.

    The single fetch entry point shared by ratings (auto_ratings) and settlement
    (tennis_results) — so a Sackmann 404/egress block transparently falls through to
    tennis-data.co.uk for both, not just ratings.
    """
    import sys
    from datetime import datetime, timezone
    if years is None:
        y = datetime.now(timezone.utc).year
        years = [y - 2, y - 1, y]
    for src in source_order():
        rows = _fetch_from_source(src, tour, years, debug)
        if rows:
            print(f"[ratings_source] {tour}: source '{src}' supplied {len(rows)} matches",
                  file=sys.stderr, flush=True)
            return rows
    return []


def auto_ratings(tour: str = "atp", years: list[int] | None = None,
                 cache_hours: float = 24.0, debug: bool = False) -> dict:
    """Load auto Elo ratings for a tour, computing from a match feed (cached to disk).

    Tries each source in `source_order()` (default Sackmann, then tennis-data.co.uk) until one
    yields matches — so a network/egress block on one host transparently falls through to the
    next. Surname aliases are added (`index_by_surname`) so surname slugs resolve. Returns {}
    when every source is empty (pipeline then falls back to market-implied).
    """
    import sys
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
    matches = fetch_matches(tour, years, debug)
    if not matches:
        # Each source already printed its concrete cause (SSL/proxy/404/egress block/...).
        print(f"[ratings_source] 0 matches for {tour} {years} from sources {source_order()} "
              f"-> market-implied (zero edge). Fix a cause above, set TENNIS_DATA_DIR, "
              f"or pass --ratings-csv.", file=sys.stderr)
        return {}
    ratings = index_by_surname(build_elo_from_matches(matches))
    try:
        with open(cache, "w", encoding="utf-8") as fh:
            json.dump(ratings, fh)
    except Exception:  # noqa: BLE001
        pass
    return ratings
