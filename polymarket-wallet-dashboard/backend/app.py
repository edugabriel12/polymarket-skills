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
import os
import sys
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, File, Form, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware

import wallet_report as wr
import demo as demo_mod
import csv_parser
import confidence_model as cm
import wallets_store as ws
import brain

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
            print(f"[brain] models cycle failed: {e}", file=sys.stderr, flush=True)


async def _watch_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(brain.run_watch_once)
        except Exception as e:  # noqa: BLE001
            print(f"[brain] watch cycle failed: {e}", file=sys.stderr, flush=True)
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
    return {"status": "ok"}


@app.get("/api/csv-demo")
def csv_demo() -> dict:
    """Sample CSV-based report (offline), same shape as POST /api/wallet/csv."""
    return demo_mod.demo_csv_report()


# ---------------------------------------------------------------------------
# Watched wallets (copy-trade source list): add = name + address + CSV.
# The CSV drives the analysis AND the confidence→value-band thresholds the live
# watcher uses to size each tier (Alta=1U / Média=0.5U / Baixa=0.25U).
# ---------------------------------------------------------------------------


@app.post("/api/wallets")
async def add_wallet(name: str = Form(...), address: str = Form(...),
                     file: UploadFile = File(...)) -> dict:
    addr = (address or "").strip()
    if not wr.wa.is_address(addr):
        return {"error": "endereço inválido — esperado 0x + 40 hex"}
    try:
        records = csv_parser.parse_csv(await file.read())
    except Exception as e:  # noqa: BLE001
        return {"error": f"falha ao processar o CSV: {e}"}
    if not records:
        return {"error": "CSV vazio ou sem linhas reconhecidas"}
    analysis = wr.rollup_csv(records)
    analysis["records"] = records          # kept for Phase-2 merging with live bets
    thresholds = cm.derive_thresholds(records)
    wid = ws.add_wallet(name, addr, analysis, thresholds, file.filename)
    return _wallet_with_live(wid)


@app.get("/api/wallets")
def list_wallets() -> dict:
    return {"wallets": ws.list_wallets()}


def _wallet_with_live(wallet_id: int) -> dict:
    """Wallet record whose `analysis` is the CSV snapshot MERGED with live settled bets."""
    import wallet_results as wres
    w = ws.get_wallet(wallet_id)
    if not w:
        return {"error": "carteira não encontrada"}
    records = (w.get("analysis") or {}).get("records", [])
    w["analysis"] = wres.merged_analysis(records, ws.list_bets(wallet_id))
    return w


@app.get("/api/wallets/{wallet_id}")
def get_wallet(wallet_id: int) -> dict:
    return _wallet_with_live(wallet_id)


@app.delete("/api/wallets/{wallet_id}")
def delete_wallet(wallet_id: int) -> dict:
    return {"deleted": ws.delete_wallet(wallet_id)}


@app.get("/api/model-results")
def model_results_route() -> dict:
    """The Modelo entity's performance for the separated Resultados (by category only)."""
    import model_results
    return model_results.model_results()


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
