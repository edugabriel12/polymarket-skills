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

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

import wallet_report as wr
import demo as demo_mod

app = FastAPI(title="Polymarket Wallet Analyzer API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


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
