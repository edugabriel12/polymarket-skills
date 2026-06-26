#!/usr/bin/env python3
"""FastAPI backend for the Polymarket sports dashboard (Soccer + Tennis).

The Analises tab serves a per-sport+date cache that an in-process scheduler refreshes
at the top of every hour in the dashboard zone (Brasília by default — 01:00, 02:00, …);
the Resultados tab settles pending predictions then returns ROI/P&L/win-rate analytics.
Read/analysis only — it never places live trades.

Both sports run their model via subprocess (each skill defines its own `data_inputs`
module, so importing them in one process would collide); the stdlib-only prediction
STORES (`soccer_predictions`, `tennis_predictions`) are imported directly for the
results/seed/pending paths.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_BACKEND_DIR, "..", ".."))
# The shared `calibration` core lives in polymarket-forecasting and is reused by every sport's
# calibration report, so its scripts dir is on sys.path.
_SHARED_SCRIPTS = os.path.join(_REPO_ROOT, "polymarket-forecasting", "scripts")
_SOCCER_SCRIPTS = os.path.join(_REPO_ROOT, "polymarket-soccer-goals", "scripts")
_SOCCER_SUGGEST = os.path.join(_SOCCER_SCRIPTS, "suggest_soccer.py")
_TENNIS_SCRIPTS = os.path.join(_REPO_ROOT, "polymarket-tennis", "scripts")
_TENNIS_SUGGEST = os.path.join(_TENNIS_SCRIPTS, "suggest_tennis.py")

for _d in (_SHARED_SCRIPTS, _SOCCER_SCRIPTS, _TENNIS_SCRIPTS):
    if _d not in sys.path:
        sys.path.append(_d)

import requests                       # noqa: E402
import soccer_predictions as spdb     # noqa: E402  (soccer store; stdlib-only, safe import)
import soccer_results                 # noqa: E402  (soccer auto-settlement; safe import)
import tennis_predictions as tdb      # noqa: E402  (tennis store; stdlib-only, safe import)
import tennis_results                 # noqa: E402  (tennis auto-settlement; safe import)


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
# Sharp bookmaker priority chain (both sharp): Pinnacle, then Betfair Exchange for markets
# Pinnacle doesn't cover (e.g. lower-league BTTS). Bet365 etc. are NOT sharp — don't add them.
SOCCER_SHARP_BOOK = os.environ.get("SOCCER_SHARP_BOOK", "pinnacle,betfair_ex_eu,matchbook")
TENNIS_DB = os.environ.get("TENNIS_PREDICTIONS_DB", tdb.DEFAULT_DB)
TENNIS_RATINGS_CSV = os.environ.get("TENNIS_RATINGS_CSV")
TENNIS_TOUR = os.environ.get("TENNIS_TOUR", "atp")
# Tennis sharp anchor: cheap (h2h only, a couple of tour keys), ON by default. TENNIS_SHARP_RESERVE
# defaults to 0 = NO RESERVE (fetch all tours); set a positive floor to ration quota. TENNIS_SHARP=0
# disables; TENNIS_SHARP_TOURS bounds the tours.
TENNIS_SHARP = os.environ.get("TENNIS_SHARP", "1") not in ("0", "false", "False", "no", "")
TENNIS_SHARP_TOURS = os.environ.get("TENNIS_SHARP_TOURS")
TENNIS_SHARP_RESERVE = int(os.environ.get("TENNIS_SHARP_RESERVE", "0") or 0)   # 0 = no reserve

SPORTS = ("soccer", "tennis")

# The day-cache (one heavy model run per sport+date) lives in its own SQLite file so it isn't
# tied to any one sport's predictions DB. Override with DASHBOARD_CACHE_DB.
CACHE_DB = os.environ.get("DASHBOARD_CACHE_DB") or \
    os.path.join(os.path.dirname(SOCCER_DB) or ".", "dashboard_cache.db")

# Hourly auto-recalc: at the top of every hour (01:00, 02:00, …) every sport's model is
# recomputed for the current day and its cache refreshed. ON by default; AUTO_RECALC=0 disables.
AUTO_RECALC = os.environ.get("AUTO_RECALC", "1") not in ("0", "false", "False", "no", "")

# The dashboard's wall clock — "today", the hourly tick, and timestamps all follow this zone.
# Defaults to Brasília. Falls back to a fixed UTC-3 if the tz database isn't installed (Brazil
# has no DST since 2019, so the fixed offset is currently exact).
RECALC_TZ = os.environ.get("RECALC_TZ", "America/Sao_Paulo")
try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo(RECALC_TZ)
except Exception:  # noqa: BLE001 - missing tzdata / bad name -> fixed Brasília offset
    LOCAL_TZ = timezone(timedelta(hours=-3))


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
    return datetime.now(LOCAL_TZ).date().isoformat()


def _now() -> str:
    return datetime.now(LOCAL_TZ).isoformat()


def _norm_sport(sport: str | None) -> str:
    s = (sport or "soccer").lower()
    return s if s in SPORTS else "soccer"


def _db_for(sport: str) -> str:
    return TENNIS_DB if sport == "tennis" else SOCCER_DB


# ---------------------------------------------------------------------------
# Cache (keyed by sport + date), stored in a dedicated dashboard cache DB
# ---------------------------------------------------------------------------


def _cache_connect() -> sqlite3.Connection:
    con = sqlite3.connect(CACHE_DB)
    con.row_factory = sqlite3.Row
    return con


def _ensure_cache_table() -> None:
    con = _cache_connect()
    try:
        with con:
            con.execute("CREATE TABLE IF NOT EXISTS analysis_cache ("
                        "sport TEXT NOT NULL, date TEXT NOT NULL, payload TEXT NOT NULL, "
                        "computed_at TEXT NOT NULL, PRIMARY KEY(sport, date))")
    finally:
        con.close()


def _cache_get(sport: str, date: str) -> dict | None:
    _ensure_cache_table()
    con = _cache_connect()
    try:
        row = con.execute("SELECT payload FROM analysis_cache WHERE sport=? AND date=?",
                          (sport, date)).fetchone()
        return json.loads(row["payload"]) if row else None
    finally:
        con.close()


def _cache_put(sport: str, date: str, payload: dict) -> None:
    _ensure_cache_table()
    con = _cache_connect()
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


def _run_soccer(date: str) -> dict:
    """Run the soccer Dixon-Coles model in a subprocess (avoids module collision).

    Verbose (no --quiet) so the model's per-game logs — discovery, baseline
    calibration, λ/P(over)/P(btts), edges, skips — are emitted; its stderr is
    forwarded to the backend output.
    """
    cmd = [sys.executable, _SOCCER_SUGGEST, "--date", date, "--output", "json",
           "--predictions-db", SOCCER_DB]  # best-line-only: one bet per game (avoids correlated lines)
    if SOCCER_RATINGS_CSV:
        cmd += ["--ratings-csv", SOCCER_RATINGS_CSV]
    if SOCCER_SHARP:   # sharp anchor (all leagues + BTTS by default); reserve 0 = no quota limit
        cmd += ["--sharp-min-reserve", str(SOCCER_SHARP_RESERVE)]
        if SOCCER_SHARP_BOOK:
            cmd += ["--sharp-book", SOCCER_SHARP_BOOK]
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
    store = tdb if sport == "tennis" else spdb
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
        if sport == "tennis":
            rows = tdb.get_predictions(TENNIS_DB, status="PENDENTE", match_date=target)
            slug_key, default_market = "match_slug", "MATCH"
        else:
            rows = spdb.get_predictions(SOCCER_DB, status="PENDENTE", game_date=target)
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
    """Dedupe identity. prediction_id is the reliable one (a recorded suggestion and its
    PENDENTE DB row share it); fall back to game/market/side/line only when it's absent.

    The field tuple alone is NOT enough: tennis suggestions carry no top-level line and a
    moneyline market, so prediction_id is what reliably collapses a suggestion and its DB row.
    """
    pid = s.get("prediction_id")
    if pid is not None:
        return ("id", pid)
    return ("f", s.get("game"), (s.get("market") or "").upper(),
            (s.get("side") or "").upper(), s.get("line"))


def _with_pending(payload: dict, sport: str, target: str) -> dict:
    """A COPY of the payload with open PENDENTE positions merged into `suggestions`.

    Deduped against what the recompute already surfaced (by prediction_id / field tuple), so a
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
    return {"status": "ok", "sports": list(SPORTS), "soccer_db": SOCCER_DB,
            "tennis_db": TENNIS_DB, "cache_db": CACHE_DB, "dotenv_loaded": _DOTENV_FILES,
            "football_data_token": bool(FOOTBALL_DATA_TOKEN),
            "soccer_ratings_csv": bool(SOCCER_RATINGS_CSV),
            "tennis_ratings_csv": bool(TENNIS_RATINGS_CSV), "tennis_tour": TENNIS_TOUR,
            "time": _now()}


@app.get("/api/analyses")
def analyses(sport: str = Query("soccer"), date: str | None = Query(None),
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

    result = {"soccer": _run_soccer, "tennis": _run_tennis}.get(sport, _run_soccer)(target)
    payload = _payload(sport, target, result)
    _cache_put(sport, target, payload)        # cache the model-only payload (pending merged on serve)
    return _with_pending(payload, sport, target)


@app.get("/api/results")
def results(sport: str = Query("soccer")) -> dict:
    sport = _norm_sport(sport)
    db = _db_for(sport)
    if sport == "tennis":
        try:
            settled = tennis_results.settle_pending(db, tour=TENNIS_TOUR, api=_QuickAPI())
        except Exception as e:  # noqa: BLE001
            settled = {"checked": 0, "settled": [], "error": str(e)}
        perf, series = tdb.performance(db), tdb.pnl_by_day(db)
        # Normalize moneyline rows to the shared PredictionRow keys (game_slug/market/line).
        recent = [dict(r, game_slug=r.get("match_slug"), game_date=r.get("match_date"),
                       market="MATCH", line=None) for r in tdb.get_predictions(db)]
    else:
        try:
            settled = soccer_results.settle_pending(
                db, token=FOOTBALL_DATA_TOKEN,
                vlog=lambda m: print(m, file=sys.stderr, flush=True))
        except Exception as e:  # noqa: BLE001
            settled = {"checked": 0, "settled": [], "error": str(e)}
        perf, series, recent = spdb.performance(db), spdb.pnl_by_day(db), spdb.get_predictions(db)
    # Best-effort: snapshot reference-side closing prices for CLV (sets each shadow
    # row's close_price once; accumulates as games approach kickoff over repeat visits).
    captured = 0
    try:
        if sport == "tennis":
            captured = tennis_results.capture_close_prices(db)
        else:
            captured = soccer_results.capture_close_prices(db)
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
def calibration_report(sport: str = Query("soccer")) -> dict:
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
        if sport == "tennis":
            settled_feed = tennis_results.settle_model_log_from_feed(db, tour=TENNIS_TOUR)
        else:
            settled_feed = soccer_results.settle_model_log_from_feed(db, token=FOOTBALL_DATA_TOKEN)
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
def predictions(sport: str = Query("soccer"), status: str | None = Query(None),
                date: str | None = Query(None)) -> dict:
    sport = _norm_sport(sport)
    if sport == "tennis":
        return {"sport": sport, "predictions": tdb.get_predictions(_db_for(sport),
                status=status, match_date=date)}
    return {"sport": sport, "predictions": spdb.get_predictions(_db_for(sport),
            status=status, game_date=date)}


@app.post("/api/cache/clear")
def clear_cache(sport: str | None = Query(None), date: str | None = Query(None)) -> dict:
    _ensure_cache_table()
    con = _cache_connect()
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
def seed_demo_route(sport: str = Query("soccer"), reset: bool = Query(False)) -> dict:
    sport = _norm_sport(sport)
    if sport == "tennis":
        return {"sport": sport, "seeded": 0, "db": _db_for(sport),
                "note": "no demo seed for tennis"}
    n = spdb.seed(SOCCER_DB, reset=reset)
    _ensure_cache_table()
    con = _cache_connect()
    with con:
        con.execute("DELETE FROM analysis_cache WHERE sport=?", (sport,))
    con.close()
    return {"sport": sport, "seeded": n, "db": _db_for(sport)}


# ---------------------------------------------------------------------------
# Hourly auto-recalc scheduler (top of each UTC hour)
# ---------------------------------------------------------------------------

_RUNNERS = {"soccer": _run_soccer, "tennis": _run_tennis}


def _recalc_sport(sport: str, target: str) -> int:
    """Recompute one sport for `target` and refresh its day-cache. Returns suggestion count."""
    result = _RUNNERS[sport](target)
    payload = _payload(sport, target, result)
    _cache_put(sport, target, payload)
    return len(payload.get("suggestions", []))


async def _recalc_all() -> None:
    """Recompute every sport for today and refresh the cache (each in a worker thread)."""
    target = _today()
    for sport in SPORTS:
        try:
            n = await asyncio.to_thread(_recalc_sport, sport, target)
            print(f"[recalc] {sport} {target}: cached {n} suggestion(s)",
                  file=sys.stderr, flush=True)
        except Exception as e:  # noqa: BLE001 - one sport failing must not stop the loop
            print(f"[recalc] {sport} {target}: FAILED — {e}", file=sys.stderr, flush=True)


def _seconds_to_next_hour() -> float:
    """Seconds from now until the next top-of-hour in the dashboard zone. Always >= 1."""
    now = datetime.now(LOCAL_TZ)
    nxt = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return max(1.0, (nxt - now).total_seconds())


async def _hourly_loop() -> None:
    while True:
        await asyncio.sleep(_seconds_to_next_hour())
        await _recalc_all()


@app.on_event("startup")
async def _start_hourly_recalc() -> None:
    """Launch the top-of-hour recompute loop for soccer + tennis (no-op if disabled)."""
    if not AUTO_RECALC:
        print("[recalc] auto-recalc disabled (AUTO_RECALC=0)", file=sys.stderr, flush=True)
        return
    print(f"[recalc] hourly auto-recalc ON (top of each hour, {RECALC_TZ}) for "
          f"{', '.join(SPORTS)}; first run in {_seconds_to_next_hour() / 60:.1f} min",
          file=sys.stderr, flush=True)
    asyncio.create_task(_hourly_loop())
