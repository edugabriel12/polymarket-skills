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
import asyncio
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
_TENNIS_SCRIPTS = os.path.join(_REPO_ROOT, "polymarket-tennis", "scripts")
_TENNIS_SUGGEST = os.path.join(_TENNIS_SCRIPTS, "suggest_tennis.py")

for _d in (_MLB_SCRIPTS, _SOCCER_SCRIPTS, _TENNIS_SCRIPTS):
    if _d not in sys.path:
        sys.path.append(_d)

import requests                       # noqa: E402
import predictions_db as pdb          # noqa: E402  (MLB store)
import analytics                      # noqa: E402  (MLB analytics)
import settlement                     # noqa: E402  (MLB settlement)
import seed_demo                      # noqa: E402  (MLB seed)
import suggest_totals                 # noqa: E402  (MLB model)
import sharp_odds                     # noqa: E402  (sharp reference + CSV loader)
import capture_close                  # noqa: E402  (sharp-close snapshot)
import clv_vs_sharp as clv_mod        # noqa: E402  (CLV scoring)
import wave_scheduler as wsch         # noqa: E402  (per-game recalc scheduler)
import soccer_predictions as spdb     # noqa: E402  (soccer store; stdlib-only, safe import)
import soccer_results                  # noqa: E402  (soccer auto-settlement; safe import)
import tennis_predictions as tdb       # noqa: E402  (tennis store; stdlib-only, safe import)
import tennis_results                  # noqa: E402  (tennis auto-settlement; safe import)


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
# Soccer sharp anchor — operator opted into ALL active leagues + BTTS (SOCCER_SHARP/_BTTS
# default ON). SOCCER_SHARP_RESERVE optionally stops the fetch once remaining Odds-API quota
# hits a floor (to leave credits for other sports), but defaults to 0 = NO RESERVE (fetch all).
# Set it to a positive floor to re-enable rationing; SOCCER_SHARP=0 disables the anchor,
# SOCCER_SHARP_LEAGUES bounds the leagues.
SOCCER_SHARP = os.environ.get("SOCCER_SHARP", "1") not in ("0", "false", "False", "no", "")
SOCCER_SHARP_LEAGUES = os.environ.get("SOCCER_SHARP_LEAGUES")   # default unset = all active
SOCCER_SHARP_BTTS = os.environ.get("SOCCER_SHARP_BTTS", "1") not in ("0", "false", "False", "no", "")
SOCCER_SHARP_RESERVE = int(os.environ.get("SOCCER_SHARP_RESERVE", "0") or 0)   # 0 = no reserve
TENNIS_DB = os.environ.get("TENNIS_PREDICTIONS_DB", tdb.DEFAULT_DB)
TENNIS_RATINGS_CSV = os.environ.get("TENNIS_RATINGS_CSV")
TENNIS_TOUR = os.environ.get("TENNIS_TOUR", "atp")
# Tennis sharp anchor: cheap (h2h only, a couple of tour keys), ON by default. TENNIS_SHARP_RESERVE
# defaults to 0 = NO RESERVE (fetch all tours); set a positive floor to ration quota. TENNIS_SHARP=0
# disables; TENNIS_SHARP_TOURS bounds the tours.
TENNIS_SHARP = os.environ.get("TENNIS_SHARP", "1") not in ("0", "false", "False", "no", "")
TENNIS_SHARP_TOURS = os.environ.get("TENNIS_SHARP_TOURS")
TENNIS_SHARP_RESERVE = int(os.environ.get("TENNIS_SHARP_RESERVE", "0") or 0)   # 0 = no reserve

SPORTS = ("mlb", "soccer", "tennis")

# --- Per-game recalc + sharp-close capture (MLB) ---------------------------------------
# OFF by default: MLB is recomputed on demand via the dashboard's "Recalcular" button (a
# force recompute that overwrites the day cache), same as soccer/tennis. The optional
# per-game "wave" loop — recompute ~WAVE_LEAD_MIN min before each game and snapshot the
# sharp line into the close CSV for CLV — is opt-in with AUTO_RECALC=1 (one Odds-API fetch
# per start-block; needs ODDS_API_KEY).
ODDS_API_KEY = os.environ.get("ODDS_API_KEY")
AUTO_RECALC = os.environ.get("AUTO_RECALC", "0") not in ("0", "false", "False", "no", "")
WAVE_LEAD_MIN = int(os.environ.get("WAVE_LEAD_MIN", "60"))      # recompute this many min before first pitch
WAVE_BUCKET_MIN = int(os.environ.get("WAVE_BUCKET_MIN", "10"))  # merge starts within this window into one wave
WAVE_POLL_SEC = int(os.environ.get("WAVE_POLL_SEC", "300"))     # how often the loop checks for a due wave
SHARP_CLOSE_CSV = os.environ.get(
    "SHARP_CLOSE_CSV", os.path.join(os.path.dirname(MLB_DB) or ".", "sharp_close.csv"))

_capture_state: dict = {"runs": 0, "last_run": None, "last_suggestions": None,
                        "last_rows": 0, "total_rows": 0, "last_error": None,
                        "enabled": AUTO_RECALC, "has_key": bool(ODDS_API_KEY),
                        "lead_min": WAVE_LEAD_MIN, "csv": SHARP_CLOSE_CSV, "started": False,
                        "next_wave": None, "waves_today": []}


def _on_wave_update(info: dict) -> None:
    """Mirror the wave loop's live schedule into the health state (for the UI)."""
    _capture_state.update(next_wave=info.get("next_wave"),
                          waves_today=info.get("waves", []))


def _sched_vlog(*a) -> None:
    print(*a, file=sys.stderr, flush=True)


def _do_capture_sync(date: str) -> tuple[list, int]:
    """Snapshot the sharp line into the close CSV only (manual /api/capture-close)."""
    new, total = capture_close.capture(ODDS_API_KEY, date, SHARP_CLOSE_CSV, vlog=_sched_vlog)
    _capture_state.update(runs=_capture_state["runs"] + 1, last_run=_now(),
                          last_rows=len(new), total_rows=total, last_error=None)
    return new, total


def _do_wave_sync(date: str) -> dict:
    """One recompute wave: fetch the sharp ONCE, snapshot it to the close CSV, then run the
    model reusing that CSV (zero extra Odds-API calls) and refresh the analysis cache."""
    lookup = sharp_odds.fetch_sharp(ODDS_API_KEY, date, vlog=_sched_vlog)   # the only API call
    rows = capture_close.lookup_to_rows(lookup, date)
    total = capture_close.write_csv(
        SHARP_CLOSE_CSV, capture_close.merge_rows(capture_close.read_csv(SHARP_CLOSE_CSV), rows))
    result = suggest_totals.run(_mlb_args(date, sharp_csv=SHARP_CLOSE_CSV))
    result.pop("_texts", None)
    payload = _payload("mlb", date, result)
    _cache_put("mlb", date, payload)
    _capture_state.update(runs=_capture_state["runs"] + 1, last_run=_now(),
                          last_suggestions=len(payload.get("suggestions", [])),
                          last_rows=len(rows), total_rows=total, last_error=None)
    return payload


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


@app.on_event("startup")
async def _start_auto_recalc() -> None:
    """Launch the per-game recalc wave loop (no-op if disabled or no key)."""
    if not AUTO_RECALC:
        print("[waves] auto-recalc disabled (AUTO_RECALC=0)", file=sys.stderr, flush=True)
        return
    if not ODDS_API_KEY:
        print("[waves] no ODDS_API_KEY -> auto-recalc NOT started (set it to enable)",
              file=sys.stderr, flush=True)
        return

    async def _get_commences():
        return await asyncio.to_thread(
            sharp_odds.fetch_commence_times, ODDS_API_KEY, "pinnacle", 10, _sched_vlog)

    async def _do_wave():
        await asyncio.to_thread(_do_wave_sync, _today())

    _capture_state["started"] = True
    print(f"[waves] auto-recalc ON: {WAVE_LEAD_MIN}min before each game "
          f"(bucket {WAVE_BUCKET_MIN}min, poll {WAVE_POLL_SEC}s) -> {SHARP_CLOSE_CSV}",
          file=sys.stderr, flush=True)
    asyncio.create_task(wsch.run_wave_loop(
        _today, _get_commences, _do_wave, _sched_vlog,
        lead_min=WAVE_LEAD_MIN, bucket_min=WAVE_BUCKET_MIN, poll_sec=WAVE_POLL_SEC,
        on_update=_on_wave_update))


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_sport(sport: str | None) -> str:
    s = (sport or "mlb").lower()
    return s if s in SPORTS else "mlb"


def _db_for(sport: str) -> str:
    if sport == "soccer":
        return SOCCER_DB
    if sport == "tennis":
        return TENNIS_DB
    return MLB_DB


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


def _mlb_args(date: str, sharp_csv: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(
        date=date, min_volume=0.0, min_edge=0.05, min_sharp_edge=0.01, min_hours=0.0, best_line_only=True,
        odds_min=1.50, odds_max=3.00, dispersion=2.0, league_baseline=8.5, league_prefix="mlb-",
        fee_rate=0.0, use_external=True, projections_csv=None, refresh_prices=False,
        portfolio_value=10000.0, portfolio_db=None, record=True, predictions_db=MLB_DB,
        paper=False, paper_execute=False, output="json", rate_limit=100, verbose=True, debug=False,
        # forecast_all off on the dashboard path: the "all games" forecast view was removed, so
        # don't pay the per-game inputs fetch for it (the CLI keeps it on by default).
        forecast_all=False,
        # Divergence detector: use the sharp slate as the authoritative game list and bet
        # ONLY on a sharp anchor. A wave passes its just-written CSV (reuse, no extra API
        # call); a manual recompute leaves it None and fetches via env ODDS_API_KEY.
        sharp_odds_csv=sharp_csv, odds_api_key=None, sharp_discovery=True, require_sharp=True)


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
    if SOCCER_SHARP:   # sharp anchor (all leagues + BTTS by default); reserve 0 = no quota limit
        cmd += ["--sharp-min-reserve", str(SOCCER_SHARP_RESERVE)]
        if SOCCER_SHARP_LEAGUES:
            cmd += ["--sharp-leagues", SOCCER_SHARP_LEAGUES]
        if not SOCCER_SHARP_BTTS:
            cmd += ["--no-sharp-btts"]
    else:              # disabled -> predictive + edge-capped
        cmd += ["--no-sharp"]
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


def _run_tennis(date: str) -> dict:
    """Run the tennis surface-Elo model in a subprocess; normalize to the frontend shape."""
    cmd = [sys.executable, _TENNIS_SUGGEST, "--date", date, "--output", "json",
           "--predictions-db", TENNIS_DB, "--tour", TENNIS_TOUR]
    if TENNIS_RATINGS_CSV:
        cmd += ["--ratings-csv", TENNIS_RATINGS_CSV]
    if TENNIS_SHARP:   # sharp anchor (h2h); reserve 0 = no quota limit
        cmd += ["--sharp-min-reserve", str(TENNIS_SHARP_RESERVE)]
        if TENNIS_SHARP_TOURS:
            cmd += ["--sharp-tours", TENNIS_SHARP_TOURS]
    else:
        cmd += ["--no-sharp"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except Exception as e:  # noqa: BLE001
        return {"error": f"tennis model failed: {e}", "counts": {}, "suggestions": [], "skipped": []}
    if proc.stderr:
        print(proc.stderr, end="" if proc.stderr.endswith("\n") else "\n",
              file=sys.stderr, flush=True)
    if proc.returncode != 0:
        return {"error": (proc.stderr or "")[-500:], "counts": {}, "suggestions": [], "skipped": []}
    try:
        raw = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {"error": "bad model output", "counts": {}, "suggestions": [], "skipped": []}
    # Map moneyline suggestions onto the shared Suggestion contract (game/market/side/line/rec).
    suggestions = []
    for s in raw.get("suggestions", []):
        suggestions.append({
            "game": s.get("match"), "market": "MATCH", "side": s.get("side"),
            "line": None, "edge": s.get("edge"), "prediction_id": s.get("prediction_id"),
            "recommendation": {
                "token_id": "", "side": s.get("side"), "size_pct": s.get("size_pct", 0.0),
                "price": s.get("price", 0.0), "confidence": min(1.0, 0.5 + (s.get("edge") or 0)),
                "reasoning": f"{s.get('side')} vs {s.get('opponent')} ({s.get('surface')})",
                "strategy": "tennis-elo-moneyline", "fee_rate": 0.0,
            },
        })
    skipped = [{"game": x.get("match"), "reason": x.get("reason"), "side": x.get("side")}
               for x in raw.get("skipped", [])]
    return {"counts": raw.get("counts", {}), "suggestions": suggestions, "skipped": skipped,
            "disclaimer": raw.get("disclaimer", ""), "error": raw.get("error")}


def _enrich(sport: str, suggestions: list[dict], date: str) -> list[dict]:
    store = {"soccer": spdb, "tennis": tdb}.get(sport, pdb)
    date_kw = {"match_date": date} if sport == "tennis" else {"game_date": date}
    by_id = {r["id"]: r for r in store.get_predictions(_db_for(sport), **date_kw)}
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


def _pending_suggestions(sport: str, target: str) -> list[dict]:
    """Open PENDENTE predictions for the day, normalized to the Suggestion shape.

    These are positions already recorded that a later recompute may no longer surface (the
    game started, the edge faded, or it wasn't rediscovered). They must ALWAYS show in the
    panel, so the operator never loses sight of an open position. Best-effort: returns [] on
    any DB error rather than breaking the analyses response.
    """
    try:
        if sport == "soccer":
            rows = spdb.get_predictions(SOCCER_DB, status="PENDENTE", game_date=target)
            slug_key, default_market = "game_slug", "TOTAL"
        elif sport == "tennis":
            rows = tdb.get_predictions(TENNIS_DB, status="PENDENTE", match_date=target)
            slug_key, default_market = "match_slug", "MATCH"
        else:
            rows = pdb.get_predictions(MLB_DB, status="PENDENTE", game_date=target)
            slug_key, default_market = "game_slug", "TOTAL"
    except Exception as e:  # noqa: BLE001
        print(f"[analyses] pending fetch failed ({sport}): {e}", file=sys.stderr, flush=True)
        return []
    out: list[dict] = []
    for r in rows:
        try:
            stats = json.loads(r.get("stats_log") or "{}")
        except Exception:  # noqa: BLE001
            stats = {}
        out.append({
            "game": r.get(slug_key), "market": r.get("market") or default_market,
            "side": r.get("side"), "line": r.get("line"), "edge": r.get("edge"),
            "prediction_id": r.get("id"), "status": r.get("status") or "PENDENTE",
            "market_url": r.get("market_url"), "question": r.get("market_question"),
            "stats": stats,
            "recommendation": {
                "token_id": r.get("token_id") or "", "side": r.get("side") or "",
                "size_pct": r.get("size_pct") or 0.0, "price": r.get("entry_price") or 0.0,
                "confidence": r.get("confidence") or 0.0,
                "reasoning": "Posição em aberto (PENDENTE), recuperada do registro.",
                "strategy": r.get("strategy") or "", "fee_rate": r.get("fee_rate") or 0.0,
            },
        })
    return out


def _suggestion_key(s: dict):
    return (s.get("game"), (s.get("market") or "").upper(),
            (s.get("side") or "").upper(), s.get("line"))


def _with_pending(payload: dict, sport: str, target: str) -> dict:
    """A COPY of the payload with open PENDENTE positions merged into `suggestions`.

    Deduped against what the recompute already surfaced (by game/market/side/line), so a
    still-predicted position isn't doubled — only positions the recompute dropped are added
    back. Applied at SERVE time (not cached) so the pending view is always current.
    """
    suggestions = list(payload.get("suggestions", []))
    seen = {_suggestion_key(s) for s in suggestions}
    added = 0
    for p in _pending_suggestions(sport, target):
        k = _suggestion_key(p)
        if k not in seen:
            seen.add(k)
            suggestions.append(p)
            added += 1
    merged = dict(payload)
    merged["suggestions"] = suggestions
    merged["counts"] = {**(payload.get("counts") or {}), "pending_shown": added}
    return merged


def _payload(sport: str, target: str, result: dict) -> dict:
    """Build the /api/analyses response (and the cached payload) from a model result."""
    return {
        "sport": sport, "date": target, "computed_at": _now(), "cached": False,
        "counts": result.get("counts", {}),
        "suggestions": _enrich(sport, result.get("suggestions", []), target),
        "forecasts": result.get("forecasts", []),   # calibrated prediction for EVERY game (MLB legacy)
        "analyses": result.get("analyses", []),      # soccer: model read for EVERY game found
        "skipped": result.get("skipped", []),
        "disclaimer": result.get("disclaimer", ""),
        "error": result.get("error"),
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "sports": list(SPORTS), "mlb_db": MLB_DB, "soccer_db": SOCCER_DB,
            "tennis_db": TENNIS_DB, "dotenv_loaded": _DOTENV_FILES,
            "football_data_token": bool(FOOTBALL_DATA_TOKEN),
            "soccer_ratings_csv": bool(SOCCER_RATINGS_CSV),
            "tennis_ratings_csv": bool(TENNIS_RATINGS_CSV), "tennis_tour": TENNIS_TOUR,
            "odds_api_key": bool(ODDS_API_KEY), "sharp_close": _capture_state,
            "time": _now()}


@app.get("/api/clv")
def clv(sport: str = Query("mlb")) -> dict:
    """CLV of recorded MLB entries vs the captured sharp close (the validated edge metric).

    Scores against the season-long CSV the scheduler accumulates. avg_CLV > 0 and
    beat_close > 50% = real edge (you bought cheaper than the sharp closed).
    """
    sport = _norm_sport(sport)
    if sport != "mlb":
        return {"sport": sport, "supported": False,
                "note": "CLV vs the sharp close is MLB-only for now", "generated_at": _now()}
    if not os.path.exists(SHARP_CLOSE_CSV):
        return {"sport": sport, "scored": 0, "report": {"all": {"n": 0}},
                "csv": SHARP_CLOSE_CSV, "capture": _capture_state,
                "note": "no sharp-close CSV yet — the scheduler captures it daily "
                        "(or POST /api/capture-close)", "generated_at": _now()}
    sharp = sharp_odds.load_sharp_csv(SHARP_CLOSE_CSV)
    scored = clv_mod.score(pdb.get_predictions(MLB_DB), sharp)
    return {"sport": sport, "scored": len(scored), "report": clv_mod.report(scored),
            "csv": SHARP_CLOSE_CSV, "capture": _capture_state, "generated_at": _now()}


@app.post("/api/capture-close")
async def capture_close_now(date: str | None = Query(None)) -> dict:
    """Force a sharp-close snapshot now (for testing or an ad-hoc capture)."""
    if not ODDS_API_KEY:
        return {"ok": False, "error": "no ODDS_API_KEY configured", "csv": SHARP_CLOSE_CSV}
    target = date or _today()
    try:
        new, total = await asyncio.to_thread(_do_capture_sync, target)
    except Exception as e:  # noqa: BLE001
        _capture_state.update(last_error=str(e))
        return {"ok": False, "error": str(e), "csv": SHARP_CLOSE_CSV}
    return {"ok": True, "date": target, "captured": len(new), "total_rows": total,
            "csv": SHARP_CLOSE_CSV, "capture": _capture_state}


@app.get("/api/analyses")
def analyses(sport: str = Query("mlb"), date: str | None = Query(None),
             force: bool = Query(False)) -> dict:
    sport = _norm_sport(sport)
    target = date or _today()
    if not force:
        cached = _cache_get(sport, target)
        if cached is not None:
            # Re-merge open PENDENTE at serve time so the pending view is always current
            # (a position may have settled since the model payload was cached).
            merged = _with_pending(cached, sport, target)
            merged["cached"] = True
            return merged

    result = ({"soccer": _run_soccer, "tennis": _run_tennis}.get(sport, _run_mlb))(target)
    payload = _payload(sport, target, result)
    _cache_put(sport, target, payload)        # cache the model-only payload (pending merged on serve)
    return _with_pending(payload, sport, target)


@app.get("/api/results")
def results(sport: str = Query("mlb")) -> dict:
    sport = _norm_sport(sport)
    db = _db_for(sport)
    if sport == "soccer":
        try:
            settled = soccer_results.settle_pending(
                db, token=FOOTBALL_DATA_TOKEN,
                vlog=lambda m: print(m, file=sys.stderr, flush=True))
        except Exception as e:  # noqa: BLE001
            settled = {"checked": 0, "settled": [], "error": str(e)}
        perf, series, recent = spdb.performance(db), spdb.pnl_by_day(db), spdb.get_predictions(db)
    elif sport == "tennis":
        try:
            settled = tennis_results.settle_pending(db, tour=TENNIS_TOUR)
        except Exception as e:  # noqa: BLE001
            settled = {"checked": 0, "settled": [], "error": str(e)}
        perf, series = tdb.performance(db), tdb.pnl_by_day(db)
        # Normalize moneyline rows to the shared PredictionRow keys (game_slug/market/line).
        recent = [dict(r, game_slug=r.get("match_slug"), game_date=r.get("match_date"),
                       market="MATCH", line=None) for r in tdb.get_predictions(db)]
    else:
        try:
            settled = settlement.settle_pending(_QuickAPI(), db)
        except Exception as e:  # noqa: BLE001
            settled = {"checked": 0, "settled": [], "error": str(e)}
        perf, series, recent = analytics.performance(db), analytics.pnl_by_day(db), pdb.get_predictions(db)
    # Best-effort: snapshot reference-side closing prices for CLV (sets each shadow
    # row's close_price once; accumulates as games approach kickoff over repeat visits).
    captured = 0
    try:
        if sport == "soccer":
            captured = soccer_results.capture_close_prices(db)
        elif sport == "tennis":
            captured = tennis_results.capture_close_prices(db)
        else:
            captured = settlement.capture_close_prices(_QuickAPI(), db)
    except Exception:  # noqa: BLE001 - never block results on price capture
        pass
    print(f"[results] {sport} settlement: checked={settled.get('checked', 0)} "
          f"finals_found={settled.get('finals_found', '-')} "
          f"settled={len(settled.get('settled', []))} "
          f"games_matched={settled.get('games_matched', '-')} "
          f"backfilled_urls={settled.get('backfilled_urls', 0)} close_captured={captured}"
          + (f" error={settled['error']}" if settled.get('error') else ""),
          file=sys.stderr, flush=True)
    # Surface the per-game settlement diagnostics (why each PENDENTE did/didn't settle).
    for line in settled.get("diagnostics", []):
        print(f"[results] {sport}: {line}", file=sys.stderr, flush=True)
    return {"sport": sport, "settlement": settled, "performance": perf,
            "pnl_by_day": series, "recent": recent, "generated_at": _now()}


@app.get("/api/calibration")
def calibration_report(sport: str = Query("mlb")) -> dict:
    """Model validation: Brier / log-loss / reliability + CLV over the shadow log.

    Settles shadow rows first (offline propagation from settled predictions, then the
    results feed for non-bet games), so the metrics reflect every modeled game, not
    just the ones that were bet. This is the read-out for deciding whether the model
    has real edge before scaling (or paying for data).
    """
    import calibration as calib
    sport = _norm_sport(sport)
    db = _db_for(sport)
    if not os.path.exists(db):
        return {"sport": sport, "logged": 0, "settled": 0, "settled_bet": 0,
                "all": {"n": 0}, "bet": {"n": 0}, "clv": {"n": 0},
                "note": "no predictions DB yet", "generated_at": _now()}
    settled_offline = calib.settle_from_predictions(db)
    settled_feed = 0
    try:
        if sport == "soccer":
            settled_feed = soccer_results.settle_model_log_from_feed(db, token=FOOTBALL_DATA_TOKEN)
        elif sport == "tennis":
            settled_feed = tennis_results.settle_model_log_from_feed(db, tour=TENNIS_TOUR)
        else:
            settled_feed = settlement.settle_model_log_from_feed(_QuickAPI(), db)
    except Exception:  # noqa: BLE001 - feed best-effort
        pass
    rep = calib.report(db)
    rep.update({"sport": sport, "settled_offline": settled_offline,
                "settled_feed": settled_feed, "generated_at": _now()})
    print(f"[calibration] {sport}: logged={rep['logged']} settled={rep['settled']} "
          f"(offline+{settled_offline}, feed+{settled_feed}) clv_n={rep['clv'].get('n', 0)}",
          file=sys.stderr, flush=True)
    return rep


@app.get("/api/predictions")
def predictions(sport: str = Query("mlb"), status: str | None = Query(None),
                date: str | None = Query(None)) -> dict:
    sport = _norm_sport(sport)
    if sport == "tennis":
        return {"sport": sport, "predictions": tdb.get_predictions(_db_for(sport),
                status=status, match_date=date)}
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
