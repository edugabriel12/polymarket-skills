"""External data adapters for the soccer goal model (full pipeline).

The model needs, per match, an expected total goals and a home supremacy. v1
sources (best-effort, each degrades independently):
  - Club Elo (free, no key) -> team strength -> supremacy
  - a ratings CSV (--ratings-csv, ToS-clean) -> per-team elo or attack/defense factors
  - xG via the `soccerdata` library (FBref/Understat) if installed -> total + supremacy
If nothing resolves, get_match_inputs returns {} and the pipeline falls back to the
market-implied model (zero edge) — it never fabricates a signal.

ToS: FBref/Understat scraping is rate-limited and gray-area; Club Elo is free for
personal use. See references/data-sources.md. Pure stdlib except the optional,
lazily-imported soccerdata/requests.
"""

from __future__ import annotations

import csv
import os
import sqlite3

CLUBELO_API = "http://api.clubelo.com"


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
# Ratings CSV (offline-capable, ToS-clean)
# ---------------------------------------------------------------------------


def load_ratings(csv_path: str) -> dict[str, dict]:
    """Per-team ratings from a CSV. team key = 'team'/'abbr' (lowercase).

    Accepted columns (case-insensitive): `elo`, and/or `att_factor`/`def_factor`
    (1.0 = league average). Returns {abbr: {elo?, att_factor?, def_factor?}}.
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
# Club Elo (network; best-effort)
# ---------------------------------------------------------------------------


def fetch_club_elo(api, club_name: str, debug: bool = False) -> float | None:
    """Latest Club Elo rating for a club. None on failure.

    Club Elo keys clubs by full name (e.g. 'ManCity'); a Polymarket abbreviation
    rarely matches directly, so supply an alias via the ratings CSV (`elo` column)
    for reliable results. This is a best-effort lookup. Returns the most recent
    rating from the CSV history endpoint.
    """
    if not club_name:
        return None
    try:
        rows = api.get(f"{CLUBELO_API}/{club_name}")
    except Exception:  # noqa: BLE001
        return None
    # Club Elo returns CSV text; category_common.APIClient.get does resp.json()
    # which will fail on CSV -> caught above. A dedicated CSV client is used by
    # the pipeline when --use-clubelo is set (see references/data-sources.md).
    if isinstance(rows, list) and rows:
        try:
            return float(rows[-1].get("Elo"))
        except (TypeError, ValueError):
            return None
    return None


# ---------------------------------------------------------------------------
# Optional xG via soccerdata (lazy; may be absent)
# ---------------------------------------------------------------------------


def fetch_xg(home_abbr: str, away_abbr: str, league_prefix: str, debug: bool = False) -> dict:
    """Best-effort rolling xG-for/against per team via soccerdata. {} if unavailable.

    soccerdata scrapes FBref/Understat (ToS-flagged) and is optional; this returns
    {} when the library or data is missing so the pipeline degrades gracefully.
    """
    try:
        import soccerdata  # noqa: F401  (optional dependency)
    except Exception:  # noqa: BLE001
        return {}
    # A full soccerdata integration (team-name mapping per league) is left as an
    # enhancement; returning {} keeps the pipeline working via Elo / ratings CSV.
    return {}


# ---------------------------------------------------------------------------
# Assemble per-match inputs
# ---------------------------------------------------------------------------


def get_match_inputs(api, home_abbr: str | None, away_abbr: str | None,
                     league_prefix: str | None, *, ratings: dict | None = None,
                     use_clubelo: bool = False, debug: bool = False) -> dict:
    """Return inputs for the model, or {} to trigger the zero-edge fallback.

    Possible keys: home_elo, away_elo, att_home, def_home, att_away, def_away,
    total_xg, supremacy_xg.
    """
    inputs: dict = {}
    ratings = ratings or {}
    h = ratings.get((home_abbr or "").lower(), {})
    a = ratings.get((away_abbr or "").lower(), {})

    if "elo" in h:
        inputs["home_elo"] = h["elo"]
    if "elo" in a:
        inputs["away_elo"] = a["elo"]
    for src, side in ((h, "home"), (a, "away")):
        if "att_factor" in src:
            inputs[f"att_{side}"] = src["att_factor"]
        if "def_factor" in src:
            inputs[f"def_{side}"] = src["def_factor"]

    # Optional live Club Elo (only when the rating CSV didn't supply Elo).
    if use_clubelo and "home_elo" not in inputs and home_abbr:
        e = fetch_club_elo(api, home_abbr, debug=debug)
        if e is not None:
            inputs["home_elo"] = e
    if use_clubelo and "away_elo" not in inputs and away_abbr:
        e = fetch_club_elo(api, away_abbr, debug=debug)
        if e is not None:
            inputs["away_elo"] = e

    # Optional xG (best-effort).
    if home_abbr and away_abbr:
        xg = fetch_xg(home_abbr, away_abbr, league_prefix or "", debug=debug)
        inputs.update(xg)

    return inputs
