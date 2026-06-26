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

import sys

from fastapi import FastAPI, File, Form, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware

import wallet_report as wr
import demo as demo_mod
import csv_parser
import confidence_model as cm
import wallets_store as ws

app = FastAPI(title="Polymarket Wallet Analyzer API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


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
    thresholds = cm.derive_thresholds(records)
    wid = ws.add_wallet(name, addr, analysis, thresholds, file.filename)
    return ws.get_wallet(wid)


@app.get("/api/wallets")
def list_wallets() -> dict:
    return {"wallets": ws.list_wallets()}


@app.get("/api/wallets/{wallet_id}")
def get_wallet(wallet_id: int) -> dict:
    w = ws.get_wallet(wallet_id)
    return w or {"error": "carteira não encontrada"}


@app.delete("/api/wallets/{wallet_id}")
def delete_wallet(wallet_id: int) -> dict:
    return {"deleted": ws.delete_wallet(wallet_id)}


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
