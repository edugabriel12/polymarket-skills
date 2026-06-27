#!/usr/bin/env python3
"""FastAPI backend for the Polymarket Wallet Analyzer dashboard.

Analyzes any PUBLIC wallet by address and returns Win rate, number of bets,
P&L and ROI — overall, per category (Futebol, Tênis, Baseball, LoL, CS2, …)
and per sub-category (Ambas Marcam, Over/Under, Moneyline, Vencedor de mapa, …).

Read-only: it uses the public Data API via the polymarket-wallet-analyzer
engine and never needs a private key. Market text is untrusted and only
pattern-matched (CLAUDE.md rule #5).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_BACKEND_DIR, "..", ".."))


def _load_dotenv() -> list[str]:
    """Load KEY=VALUE from backend/.env, ../.env, or repo .env (real env wins).

    Mirrors the Sports backend. Must run BEFORE `import brain` (brain -> model_runner
    reads SOCCER_PREDICTIONS_DB etc. at import time) and before the soccer/tennis
    subprocesses inherit the environment at runtime (ODDS_API_KEY, APIFOOTBALL_KEY,
    FOOTBALL_DATA_TOKEN).
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
                    if key.strip():
                        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
            loaded.append(path)
        except OSError:
            pass
    return loaded


_DOTENV_FILES = _load_dotenv()

import wallet_report as wr  # noqa: E402
import demo as demo_mod  # noqa: E402
import csv_parser  # noqa: E402
import confidence_model as cm  # noqa: E402
import wallets_store as ws  # noqa: E402
import brain  # noqa: E402

# Scheduler config. The brain runs the models at the top of every hour (Brasília by
# default) and polls the watched wallets every WATCH_POLL_SEC; both push to Sports.
AUTO_BRAIN = os.environ.get("AUTO_BRAIN", "1") not in ("0", "false", "False", "no", "")
WATCH_POLL_SEC = int(os.environ.get("WATCH_POLL_SEC", "45"))
RECALC_TZ = os.environ.get("RECALC_TZ", "America/Sao_Paulo")
try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo(RECALC_TZ)
except Exception:  # noqa: BLE001
    LOCAL_TZ = timezone(timedelta(hours=-3))

app = FastAPI(title="Polymarket Wallet Analyzer API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(Exception)
async def _log_unhandled(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all for ANY unhandled error in any endpoint: full traceback to stderr +
    error class/message in the 500 body, so both the server log and the browser console
    pinpoint exactly which flow broke."""
    traceback.print_exc(file=sys.stderr)
    print(f"[api] ERROR {request.method} {request.url.path}: {type(exc).__name__}: {exc}",
          file=sys.stderr, flush=True)
    return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})


@app.middleware("http")
async def _log_requests(request: Request, call_next):
    """One line per request; flags non-2xx so a failing flow is obvious in the log."""
    resp = await call_next(request)
    if resp.status_code >= 400:
        print(f"[api] {resp.status_code} {request.method} {request.url.path}",
              file=sys.stderr, flush=True)
    return resp


def _today() -> str:
    return datetime.now(LOCAL_TZ).date().isoformat()


def _seconds_to_next_hour() -> float:
    now = datetime.now(LOCAL_TZ)
    nxt = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return max(1.0, (nxt - now).total_seconds())


async def _models_loop() -> None:
    while True:
        await asyncio.sleep(_seconds_to_next_hour())
        try:
            await asyncio.to_thread(brain.run_models_once, _today())
        except Exception as e:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            print(f"[brain] models cycle failed: {type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)


async def _watch_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(brain.run_watch_once)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            print(f"[brain] watch cycle failed: {type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)
        await asyncio.sleep(WATCH_POLL_SEC)


@app.on_event("startup")
async def _start_brain() -> None:
    if not AUTO_BRAIN:
        print("[brain] AUTO_BRAIN=0 — scheduler disabled", file=sys.stderr, flush=True)
        return
    print(f"[brain] ON — models at top of each hour ({RECALC_TZ}), watch every "
          f"{WATCH_POLL_SEC}s; pushing to {os.environ.get('SPORTS_INGEST_URL') or '(unset)'}",
          file=sys.stderr, flush=True)
    asyncio.create_task(_models_loop())
    asyncio.create_task(_watch_loop())


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "dotenv_loaded": _DOTENV_FILES}


@app.get("/api/csv-demo")
def csv_demo() -> dict:
    """Sample CSV-based report (offline), same shape as POST /api/wallet/csv."""
    return demo_mod.demo_csv_report()


# ---------------------------------------------------------------------------
# Watched wallets (copy-trade source list): add = name + address + CSV.
# The CSV drives the analysis AND the confidence→value-band thresholds the live
# watcher uses to size each tier (Alta=1U / Média=0.5U / Baixa=0.25U).
# ---------------------------------------------------------------------------


def _clean_filters(raw: str, tree: dict) -> dict | None:
    """Parse the `filters` form field (JSON {category:{subcategory:[confidences]}}) and keep
    only real combos present in the CSV's filter_tree, normalizing each confidence label and
    preserving the tree's canonical order.

    Semantics:
      • blank/absent field → None  (unconfigured → forward everything)
      • the FULL tree selected → None  (no restriction; also forwards live categories the CSV
        never had, so "keep all" behaves like today)
      • a strict subset → that dict  (forward only those combos)
      • explicit empty {} → {}  (forward NOTHING)
    Raises ValueError on malformed JSON / wrong shape."""
    if not raw or not raw.strip():
        return None
    parsed = json.loads(raw)                                   # may raise ValueError
    if not isinstance(parsed, dict):
        raise ValueError("esperado um objeto categoria→sub-categoria→confianças")
    clean: dict = {}
    for cat, subs in parsed.items():
        if cat not in tree or not isinstance(subs, dict):
            continue
        for sub, confs in subs.items():
            allowed = tree[cat].get(sub)
            if not allowed or not isinstance(confs, (list, tuple)):
                continue
            picked = {csv_parser._norm_conf(str(x)) for x in confs}
            ordered = [c for c in allowed if c in picked]      # keep canonical order
            if ordered:
                clean.setdefault(cat, {})[sub] = ordered
    full = {c: {s: list(cs) for s, cs in sv.items()} for c, sv in tree.items()}
    return None if clean == full else clean                   # all selected == no restriction


def _filter_tree_from_analysis(analysis: dict) -> dict:
    """The {category:{subcategory:[confidences]}} OPTIONS for the filter UI. Prefer the
    persisted filter_tree (wallets added after this feature); else rebuild from the stored
    by_category subcategories, offering the tiers seen there or all of cm.TIERS (legacy)."""
    tree = analysis.get("filter_tree")
    if tree:
        return tree
    out: dict = {}
    for cat in analysis.get("by_category", []):
        name = cat.get("category")
        if not name:
            continue
        subs = {}
        for sub in cat.get("subcategories", []):
            sname = sub.get("subcategory")
            if not sname:
                continue
            seen = [b.get("confidence") for b in sub.get("by_confidence", []) if b.get("confidence")]
            subs[sname] = seen or list(cm.TIERS)
        out[name] = subs
    return out


@app.post("/api/wallets")
async def add_wallet(name: str = Form(...), address: str = Form(...),
                     file: UploadFile = File(...), filters: str = Form("")) -> dict:
    addr = (address or "").strip()
    if not wr.wa.is_address(addr):
        return {"error": "endereço inválido — esperado 0x + 40 hex"}
    try:
        records = csv_parser.parse_csv(await file.read())
    except Exception as e:  # noqa: BLE001
        return {"error": f"falha ao processar o CSV: {e}"}
    if not records:
        return {"error": "CSV vazio ou sem linhas reconhecidas"}
    analysis = wr.rollup_csv(records)      # CSV profile (kept for reference; NOT shown as results)
    thresholds = cm.derive_thresholds(records)
    try:
        clean_filters = _clean_filters(filters, analysis.get("filter_tree") or {})
    except ValueError as e:
        return {"error": f"filtros inválidos: {e}"}
    wid = ws.add_wallet(name, addr, analysis, thresholds, file.filename, filters=clean_filters)
    return _wallet_with_live(wid)


@app.get("/api/wallets")
def list_wallets() -> dict:
    return {"wallets": ws.list_wallets()}


def _wallet_with_live(wallet_id: int) -> dict:
    """Wallet record whose `analysis` is its LIVE results only (settled bets after add).
    The CSV is never shown as results — it only fed the confidence/unit bands. We surface
    `filter_tree` (the selectable options, from the stored CSV analysis) BEFORE overwriting
    `analysis` with the live rollup, so the edit UI can render + pre-check the boxes."""
    import wallet_results as wres
    w = ws.get_wallet(wallet_id)
    if not w:
        return {"error": "carteira não encontrada"}
    w["filter_tree"] = _filter_tree_from_analysis(w.get("analysis") or {})
    w["analysis"] = wres.live_results(ws.list_bets(wallet_id))
    return w


@app.get("/api/wallets/{wallet_id}")
def get_wallet(wallet_id: int) -> dict:
    return _wallet_with_live(wallet_id)


@app.delete("/api/wallets/{wallet_id}")
def delete_wallet(wallet_id: int) -> dict:
    return {"deleted": ws.delete_wallet(wallet_id)}


@app.patch("/api/wallets/{wallet_id}")
async def update_wallet_filters(wallet_id: int, filters: str = Form("")) -> dict:
    """Edit a wallet's forwarding filters in place — preserves analysis/thresholds/live history
    (unlike DELETE, which cascades). `filters` is JSON {category:{subcategory:[confidences]}};
    blank clears the filter (= forward everything)."""
    w = ws.get_wallet(wallet_id)
    if not w:
        return {"error": "carteira não encontrada"}
    try:
        clean_filters = _clean_filters(filters, _filter_tree_from_analysis(w.get("analysis") or {}))
    except ValueError as e:
        return {"error": f"filtros inválidos: {e}"}
    ws.update_filters(wallet_id, clean_filters)
    return _wallet_with_live(wallet_id)


@app.get("/api/model-results")
def model_results_route() -> dict:
    """The Modelo entity's performance for the separated Resultados (by category only)."""
    import model_results
    return model_results.model_results()


@app.get("/api/wallets/{wallet_id}/bets")
def wallet_bets_route(wallet_id: int, category: str | None = Query(None),
                      page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)) -> dict:
    """Paginated settled live bets of a wallet (optionally by category)."""
    offset = (page - 1) * page_size
    return {"total": ws.count_settled_bets(wallet_id, category), "page": page,
            "page_size": page_size,
            "bets": ws.list_settled_bets(wallet_id, category, offset, page_size)}


@app.get("/api/model-bets")
def model_bets_route(category: str = Query(...), page: int = Query(1, ge=1),
                     page_size: int = Query(20, ge=1, le=100)) -> dict:
    """Paginated settled model predictions of a category (Futebol/Tênis)."""
    import model_results
    offset = (page - 1) * page_size
    out = model_results.model_bets(category, offset, page_size)
    return {"total": out["total"], "page": page, "page_size": page_size, "bets": out["bets"]}


@app.get("/api/wallets/{wallet_id}/open-bets")
def wallet_open_bets_route(wallet_id: int, category: str | None = Query(None),
                           page: int = Query(1, ge=1),
                           page_size: int = Query(20, ge=1, le=100)) -> dict:
    """Paginated OPEN (unsettled) live bets of a wallet — the pending tab."""
    offset = (page - 1) * page_size
    return {"total": ws.count_open_bets(wallet_id, category), "page": page,
            "page_size": page_size,
            "bets": ws.list_open_bets(wallet_id, category, offset, page_size)}


@app.get("/api/model-open-bets")
def model_open_bets_route(category: str | None = Query(None), page: int = Query(1, ge=1),
                          page_size: int = Query(20, ge=1, le=100)) -> dict:
    """Paginated OPEN (PENDENTE) model predictions; category=None merges Futebol + Tênis."""
    import model_results
    offset = (page - 1) * page_size
    out = model_results.model_open_bets(category, offset, page_size)
    return {"total": out["total"], "page": page, "page_size": page_size, "bets": out["bets"]}


@app.post("/api/wallet/csv")
async def wallet_csv(file: UploadFile = File(...)) -> dict:
    """Analyze an uploaded bet-history CSV (Data;Evento;Aposta;Conf.;Odd;Investido;ROI%;Lucro).

    Returns the same indicators as the address flow — Win rate, nº de apostas, P&L, ROI —
    overall, por categoria/sub-categoria AND split by confidence level (Alta/Média/Baixa).
    """
    try:
        data = await file.read()
        report = wr.analyze_csv(data)
    except Exception as e:  # noqa: BLE001
        print(f"[csv] parse failed for {file.filename!r}: {e}", file=sys.stderr, flush=True)
        return {"error": f"falha ao processar o CSV: {e}", "filename": file.filename}
    if report["n_markets"] == 0:
        return {"error": "CSV vazio ou sem linhas reconhecidas (cabeçalho esperado: "
                "Data;Evento;Aposta;Conf.;Odd;Investido;ROI%;Lucro)",
                "filename": file.filename}
    report["filename"] = file.filename
    return report


@app.get("/api/wallet")
def wallet(address: str = Query(...), trade_limit: int = Query(2000, ge=1, le=20000),
           enrich_tags: bool = Query(False), debug: bool = Query(False)) -> dict:
    """Live analysis for a wallet address. `address=demo` serves sample data."""
    addr = (address or "").strip()
    if addr.lower() == "demo":
        return demo_mod.demo_report()
    if not wr.wa.is_address(addr):
        return {"error": "invalid address — expected a 0x-prefixed 40-hex wallet",
                "address": addr}
    try:
        return wr.analyze(addr, trade_limit=trade_limit, enrich_tags=enrich_tags, debug=debug)
    except Exception as e:  # noqa: BLE001 - surface the failure to the UI, don't 500
        print(f"[wallet] analysis failed for {addr}: {e}", file=sys.stderr, flush=True)
        return {"error": f"analysis failed: {e}", "address": addr}
