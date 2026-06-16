#!/usr/bin/env python3
"""FastAPI backend for the Polymarket sports dashboard (MLB + Soccer).

The Analises tab runs the heavy model calc once/day (cached per sport+date); the
Resultados tab settles pending predictions then returns ROI/P&L/win-rate analytics.
Read/analysis only — it never places live trades.

MLB runs in-process. Soccer runs via subprocess (the two skills both define a
`data_inputs` module, so importing both in one process would collide); the soccer
predictions STORE (`soccer_predictions`, stdlib-only, unique name) is imported
directly for results/seed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_MLB_SCRIPTS = os.path.normpath(os.path.join(_BACKEND_DIR, "..", "..", "scripts"))
_REPO_ROOT = os.path.normpath(os.path.join(_BACKEND_DIR, "..", "..", ".."))
_SOCCER_SCRIPTS = os.path.join(_REPO_ROOT, "polymarket-soccer-goals", "scripts")
_SOCCER_SUGGEST = os.path.join(_SOCCER_SCRIPTS, "suggest_soccer.py")

for _d in (_MLB_SCRIPTS, _SOCCER_SCRIPTS):
    if _d not in sys.path:
        sys.path.append(_d)

import requests                       # noqa: E402
import predictions_db as pdb          # noqa: E402  (MLB store)
import analytics                      # noqa: E402  (MLB analytics)
import settlement                     # noqa: E402  (MLB settlement)
import seed_demo                      # noqa: E402  (MLB seed)
import suggest_totals                 # noqa: E402  (MLB model)
import soccer_predictions as spdb     # noqa: E402  (soccer store; stdlib-only, safe import)
import soccer_results                  # noqa: E402  (soccer auto-settlement; safe import)


def _load_dotenv() -> list[str]:
    """Load KEY=VALUE lines from a .env file (backend/, webapp/, or repo root).

    Real environment variables take precedence (setdefault), so a shell/IntelliJ
    var overrides the file. No external dependency. Returns the files loaded.
    """
    loaded = []
    for path in (os.path.join(_BACKEND_DIR, ".env"),
                 os.path.normpath(os.path.join(_BACKEND_DIR, "..", ".env")),
                 os.path.join(_REPO_ROOT, ".env")):
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    key = key.strip()
                    if key:
                        os.environ.setdefault(key, val.strip().strip('"').strip("'"))
            loaded.append(path)
        except OSError:
            pass
    return loaded


_DOTENV_FILES = _load_dotenv()

MLB_DB = os.environ.get("PREDICTIONS_DB", pdb.DEFAULT_DB)
SOCCER_DB = os.environ.get("SOCCER_PREDICTIONS_DB", spdb.DEFAULT_DB)
SOCCER_RATINGS_CSV = os.environ.get("SOCCER_RATINGS_CSV")
FOOTBALL_DATA_TOKEN = os.environ.get("FOOTBALL_DATA_TOKEN")

SPORTS = ("mlb", "soccer")


class _QuickAPI:
    """Single-attempt HTTP client so settlement never stalls the dashboard."""
    def __init__(self, timeout: int = 8):
        self.timeout = timeout
        self._s = requests.Session()
        self._s.headers["User-Agent"] = "polymarket-dashboard"

    def get(self, url: str, params: dict | None = None):
        r = self._s.get(url, params=params or {}, timeout=self.timeout)
        r.raise_for_status()
        return r.json()


app = FastAPI(title="Polymarket Sports Dashboard API", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_sport(sport: str | None) -> str:
    s = (sport or "mlb").lower()
    return s if s in SPORTS else "mlb"


def _db_for(sport: str) -> str:
    return SOCCER_DB if sport == "soccer" else MLB_DB


# ---------------------------------------------------------------------------
# Cache (keyed by sport + date), stored in the MLB predictions DB
# ---------------------------------------------------------------------------


def _ensure_cache_table() -> None:
    con = pdb.connect(MLB_DB)
    try:
        with con:
            cols = {r["name"] for r in con.execute("PRAGMA table_info(analysis_cache)")}
            if cols and "sport" not in cols:
                con.execute("DROP TABLE analysis_cache")  # migrate old date-only cache
                cols = set()
            if not cols:
                con.execute("CREATE TABLE IF NOT EXISTS analysis_cache ("
                            "sport TEXT NOT NULL, date TEXT NOT NULL, payload TEXT NOT NULL, "
                            "computed_at TEXT NOT NULL, PRIMARY KEY(sport, date))")
    finally:
        con.close()


def _cache_get(sport: str, date: str) -> dict | None:
    _ensure_cache_table()
    con = pdb.connect(MLB_DB)
    try:
        row = con.execute("SELECT payload FROM analysis_cache WHERE sport=? AND date=?",
                          (sport, date)).fetchone()
        return json.loads(row["payload"]) if row else None
    finally:
        con.close()


def _cache_put(sport: str, date: str, payload: dict) -> None:
    _ensure_cache_table()
    con = pdb.connect(MLB_DB)
    try:
        with con:
            con.execute("INSERT INTO analysis_cache(sport, date, payload, computed_at) "
                        "VALUES(?,?,?,?) ON CONFLICT(sport, date) DO UPDATE SET "
                        "payload=excluded.payload, computed_at=excluded.computed_at",
                        (sport, date, json.dumps(payload, default=str), payload["computed_at"]))
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Model runners
# ---------------------------------------------------------------------------


def _mlb_args(date: str) -> argparse.Namespace:
    return argparse.Namespace(
        date=date, min_volume=1000.0, min_edge=0.05, min_hours=0.0, best_line_only=True,
        odds_min=1.50, odds_max=3.00, dispersion=2.0, league_baseline=8.5, league_prefix="mlb-",
        fee_rate=0.0, use_external=True, projections_csv=None, refresh_prices=False,
        portfolio_value=10000.0, portfolio_db=None, record=True, predictions_db=MLB_DB,
        paper=False, paper_execute=False, output="json", rate_limit=100, verbose=True, debug=False)


def _run_mlb(date: str) -> dict:
    result = suggest_totals.run(_mlb_args(date))
    result.pop("_texts", None)
    return result


def _run_soccer(date: str) -> dict:
    """Run the soccer Dixon-Coles model in a subprocess (avoids module collision).

    Verbose (no --quiet) so the model's per-game logs — discovery, baseline
    calibration, λ/P(over)/P(btts), edges, skips — are emitted; its stderr is
    forwarded to the backend output for parity with the in-process MLB logs.
    """
    cmd = [sys.executable, _SOCCER_SUGGEST, "--date", date, "--output", "json",
           "--predictions-db", SOCCER_DB]  # best-line-only: one bet per game (avoids correlated lines)
    if SOCCER_RATINGS_CSV:
        cmd += ["--ratings-csv", SOCCER_RATINGS_CSV]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
    except Exception as e:  # noqa: BLE001
        return {"error": f"soccer model failed: {e}", "counts": {}, "suggestions": [], "skipped": []}
    if proc.stderr:  # surface the soccer model's logs in the backend terminal
        print(proc.stderr, end="" if proc.stderr.endswith("\n") else "\n",
              file=sys.stderr, flush=True)
    if proc.returncode != 0:
        return {"error": (proc.stderr or "")[-500:], "counts": {}, "suggestions": [], "skipped": []}
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {"error": "bad model output", "counts": {}, "suggestions": [], "skipped": []}


def _enrich(sport: str, suggestions: list[dict], date: str) -> list[dict]:
    store = spdb if sport == "soccer" else pdb
    by_id = {r["id"]: r for r in store.get_predictions(_db_for(sport), game_date=date)}
    for s in suggestions:
        row = by_id.get(s.get("prediction_id"))
        if row:
            try:
                s["stats"] = json.loads(row["stats_log"]) if row.get("stats_log") else {}
            except (TypeError, ValueError):
                s["stats"] = {}
            s["market_url"] = row.get("market_url")
            s["status"] = row.get("status")
            s["question"] = row.get("market_question")
    return suggestions


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "sports": list(SPORTS), "mlb_db": MLB_DB, "soccer_db": SOCCER_DB,
            "dotenv_loaded": _DOTENV_FILES,
            "football_data_token": bool(FOOTBALL_DATA_TOKEN),
            "soccer_ratings_csv": bool(SOCCER_RATINGS_CSV), "time": _now()}


@app.get("/api/analyses")
def analyses(sport: str = Query("mlb"), date: str | None = Query(None),
             force: bool = Query(False)) -> dict:
    sport = _norm_sport(sport)
    target = date or _today()
    if not force:
        cached = _cache_get(sport, target)
        if cached is not None:
            cached["cached"] = True
            return cached

    result = _run_soccer(target) if sport == "soccer" else _run_mlb(target)
    payload = {
        "sport": sport, "date": target, "computed_at": _now(), "cached": False,
        "counts": result.get("counts", {}),
        "suggestions": _enrich(sport, result.get("suggestions", []), target),
        "skipped": result.get("skipped", []),
        "disclaimer": result.get("disclaimer", ""),
        "error": result.get("error"),
    }
    _cache_put(sport, target, payload)
    return payload


@app.get("/api/results")
def results(sport: str = Query("mlb")) -> dict:
    sport = _norm_sport(sport)
    db = _db_for(sport)
    if sport == "soccer":
        try:
            settled = soccer_results.settle_pending(db, token=FOOTBALL_DATA_TOKEN)
        except Exception as e:  # noqa: BLE001
            settled = {"checked": 0, "settled": [], "error": str(e)}
        perf, series, recent = spdb.performance(db), spdb.pnl_by_day(db), spdb.get_predictions(db)[:50]
    else:
        try:
            settled = settlement.settle_pending(_QuickAPI(), db)
        except Exception as e:  # noqa: BLE001
            settled = {"checked": 0, "settled": [], "error": str(e)}
        perf, series, recent = analytics.performance(db), analytics.pnl_by_day(db), pdb.get_predictions(db)[:50]
    print(f"[results] {sport} settlement: checked={settled.get('checked', 0)} "
          f"finals_found={settled.get('finals_found', '-')} "
          f"settled={len(settled.get('settled', []))} "
          f"backfilled_urls={settled.get('backfilled_urls', 0)}"
          + (f" error={settled['error']}" if settled.get('error') else ""),
          file=sys.stderr, flush=True)
    return {"sport": sport, "settlement": settled, "performance": perf,
            "pnl_by_day": series, "recent": recent, "generated_at": _now()}


@app.get("/api/predictions")
def predictions(sport: str = Query("mlb"), status: str | None = Query(None),
                date: str | None = Query(None)) -> dict:
    sport = _norm_sport(sport)
    store = spdb if sport == "soccer" else pdb
    return {"sport": sport, "predictions": store.get_predictions(_db_for(sport), status=status, game_date=date)}


@app.post("/api/cache/clear")
def clear_cache(sport: str | None = Query(None), date: str | None = Query(None)) -> dict:
    _ensure_cache_table()
    con = pdb.connect(MLB_DB)
    try:
        clauses, params = [], []
        if sport:
            clauses.append("sport=?"); params.append(_norm_sport(sport))
        if date:
            clauses.append("date=?"); params.append(date)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with con:
            n = con.execute("DELETE FROM analysis_cache" + where, params).rowcount
        return {"cleared_rows": n, "sport": sport or "all", "date": date or "all"}
    finally:
        con.close()


@app.post("/api/seed-demo")
def seed_demo_route(sport: str = Query("mlb"), reset: bool = Query(False)) -> dict:
    sport = _norm_sport(sport)
    n = spdb.seed(SOCCER_DB, reset=reset) if sport == "soccer" else seed_demo.seed(MLB_DB, reset=reset)
    _ensure_cache_table()
    con = pdb.connect(MLB_DB)
    with con:
        con.execute("DELETE FROM analysis_cache WHERE sport=?", (sport,))
    con.close()
    return {"sport": sport, "seeded": n, "db": _db_for(sport)}
