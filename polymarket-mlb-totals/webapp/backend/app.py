#!/usr/bin/env python3
"""FastAPI backend for the MLB total-runs dashboard.

Wraps the Python model: the Analises tab runs the heavy calc once/day (cached in
the predictions DB); the Resultados tab triggers cross-source settlement on every
request, then returns ROI/P&L/win-rate analytics. Read/analysis only — it never
places live trades.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

# Wire the skill's scripts/ onto sys.path (same pattern as the skill's _bootstrap).
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.normpath(os.path.join(_BACKEND_DIR, "..", "..", "scripts"))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import requests                       # noqa: E402
import predictions_db as pdb          # noqa: E402
import analytics                      # noqa: E402
import settlement                     # noqa: E402
import seed_demo                      # noqa: E402
import suggest_totals                 # noqa: E402


class _QuickAPI:
    """Single-attempt HTTP client (duck-typed .get) for snappy settlement.

    Unlike the skill's retrying APIClient, this fails fast on a forbidden/offline
    host so the Resultados endpoint never stalls; settlement treats errors as
    "not yet resolved" and leaves rows PENDENTE.
    """

    def __init__(self, timeout: int = 8):
        self.timeout = timeout
        self._s = requests.Session()
        self._s.headers["User-Agent"] = "polymarket-mlb-totals/dashboard"

    def get(self, url: str, params: dict | None = None):
        resp = self._s.get(url, params=params or {}, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

DB_PATH = os.environ.get("PREDICTIONS_DB", pdb.DEFAULT_DB)

app = FastAPI(title="MLB Totals Dashboard API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # dev only; tighten for any real deployment
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _analysis_args(target_date: str) -> argparse.Namespace:
    """A settings namespace for suggest_totals.run() (paper-off, recording on)."""
    return argparse.Namespace(
        date=target_date, min_volume=10000.0, min_edge=0.05,
        odds_min=1.60, odds_max=3.00, dispersion=2.0, league_baseline=8.5,
        league_prefix="mlb-",
        fee_rate=0.0, use_external=True, projections_csv=None,
        refresh_prices=False, portfolio_value=10000.0, portfolio_db=None,
        record=True, predictions_db=DB_PATH, paper=False, paper_execute=False,
        output="json", rate_limit=100, verbose=True, debug=False)


def _ensure_cache_table() -> None:
    con = pdb.connect(DB_PATH)
    with con:
        con.execute("CREATE TABLE IF NOT EXISTS analysis_cache ("
                    "date TEXT PRIMARY KEY, payload TEXT NOT NULL, computed_at TEXT NOT NULL)")
    con.close()


def _cache_get(target_date: str) -> dict | None:
    _ensure_cache_table()
    con = pdb.connect(DB_PATH)
    try:
        row = con.execute("SELECT payload FROM analysis_cache WHERE date=?",
                          (target_date,)).fetchone()
        return json.loads(row["payload"]) if row else None
    finally:
        con.close()


def _cache_put(target_date: str, payload: dict) -> None:
    _ensure_cache_table()
    con = pdb.connect(DB_PATH)
    with con:
        con.execute("INSERT INTO analysis_cache(date, payload, computed_at) VALUES(?,?,?) "
                    "ON CONFLICT(date) DO UPDATE SET payload=excluded.payload, "
                    "computed_at=excluded.computed_at",
                    (target_date, json.dumps(payload, default=str), payload["computed_at"]))
    con.close()


def _enrich_with_stats(suggestions: list[dict], target_date: str) -> list[dict]:
    """Attach each prediction's stored stats_log + market_url + status."""
    by_id = {r["id"]: r for r in pdb.get_predictions(DB_PATH, game_date=target_date)}
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
    return {"status": "ok", "db": DB_PATH, "time": _now()}


@app.get("/api/analyses")
def analyses(date: str | None = Query(None), force: bool = Query(False)) -> dict:
    """Day's predictions. Heavy model calc runs once/day, cached until day end."""
    target = date or _today()
    if not force:
        cached = _cache_get(target)
        if cached is not None:
            cached["cached"] = True
            return cached

    result = suggest_totals.run(_analysis_args(target))
    result.pop("_texts", None)
    payload = {
        "date": target,
        "computed_at": _now(),
        "cached": False,
        "counts": result.get("counts", {}),
        "suggestions": _enrich_with_stats(result.get("suggestions", []), target),
        "skipped": result.get("skipped", []),
        "disclaimer": result.get("disclaimer", ""),
    }
    _cache_put(target, payload)
    return payload


@app.get("/api/results")
def results() -> dict:
    """Settle pending predictions (cross-source), then return performance."""
    try:
        settled = settlement.settle_pending(_QuickAPI(), DB_PATH)
    except Exception as e:  # noqa: BLE001 - settlement must never break the dashboard
        settled = {"checked": 0, "settled": [], "error": str(e)}
    return {
        "settlement": settled,
        "performance": analytics.performance(DB_PATH),
        "pnl_by_day": analytics.pnl_by_day(DB_PATH),
        "recent": pdb.get_predictions(DB_PATH)[:50],
        "generated_at": _now(),
    }


@app.get("/api/predictions")
def predictions(status: str | None = Query(None), date: str | None = Query(None)) -> dict:
    return {"predictions": pdb.get_predictions(DB_PATH, status=status, game_date=date)}


@app.post("/api/cache/clear")
def clear_cache(date: str | None = Query(None)) -> dict:
    """Delete the analyses cache (all, or one date) so the next call recomputes."""
    _ensure_cache_table()
    con = pdb.connect(DB_PATH)
    try:
        with con:
            if date:
                n = con.execute("DELETE FROM analysis_cache WHERE date=?", (date,)).rowcount
            else:
                n = con.execute("DELETE FROM analysis_cache").rowcount
        return {"cleared_rows": n, "date": date or "all"}
    finally:
        con.close()


@app.post("/api/seed-demo")
def seed_demo_route(reset: bool = Query(False)) -> dict:
    """Populate sample predictions so the UI is usable offline (demo only)."""
    n = seed_demo.seed(DB_PATH, reset=reset)
    # Invalidate the analysis cache so the Analises tab reflects new data if reused.
    con = sqlite3.connect(DB_PATH)
    with con:
        con.execute("DROP TABLE IF EXISTS analysis_cache")
    con.close()
    return {"seeded": n, "db": DB_PATH}
