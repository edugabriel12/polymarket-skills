"""FastAPI backend for the Polymarket copy-trader (paper mode).

Separate, self-contained flow: save public wallets, copy their buys/sells into a
$10k fake-USD paper portfolio (slippage-bounded), and report live entries + KPIs.

Paper trading simulation — not financial advice. Real trading involves risk of loss.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import traceback

from fastapi import FastAPI, Form, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import copy_engine as ce  # noqa: E402
import db  # noqa: E402
import deps  # noqa: E402
import poller  # noqa: E402
import results as res  # noqa: E402

COPY_POLL_SEC = int(os.environ.get("COPY_POLL_SEC", "60"))
AUTO_POLL = os.environ.get("AUTO_POLL", "1") not in ("0", "", "false", "False")

app = FastAPI(title="Polymarket Copy-Trader API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])

db.init_db()  # idempotent — ensure tables exist even before the startup event fires


@app.exception_handler(Exception)
async def _log_unhandled(request, exc):  # noqa: ANN001
    traceback.print_exc()
    return JSONResponse(status_code=500,
                        content={"detail": f"{type(exc).__name__}: {exc}"})


# ---------------------------------------------------------------------------
# Background poll loop
# ---------------------------------------------------------------------------
async def _poll_loop() -> None:
    if not AUTO_POLL:
        print("[copy-trader] AUTO_POLL=0 — background polling disabled", file=sys.stderr)
        return
    print(f"[copy-trader] polling every {COPY_POLL_SEC}s", file=sys.stderr)
    while True:
        try:
            await asyncio.to_thread(poller.run_once)
        except Exception as e:  # noqa: BLE001
            print(f"[copy-trader] poll cycle error: {e}", file=sys.stderr)
        await asyncio.sleep(COPY_POLL_SEC)


@app.on_event("startup")
async def _startup() -> None:
    db.init_db()
    asyncio.create_task(_poll_loop())


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "auto_poll": AUTO_POLL, "poll_sec": COPY_POLL_SEC}


@app.get("/api/wallets")
def list_wallets() -> dict:
    return {"wallets": res.all_wallet_stats()}


@app.post("/api/wallets")
def add_wallet(name: str = Form(...), address: str = Form(...)) -> dict:
    name = (name or "").strip()
    address = (address or "").strip()
    if not name:
        return JSONResponse(status_code=400, content={"error": "name is required"})
    if not deps.is_address(address):
        return JSONResponse(status_code=400,
                            content={"error": "invalid wallet address (expected 0x + 40 hex)"})
    if db.get_wallet_by_address(address):
        return JSONResponse(status_code=409,
                            content={"error": "wallet already saved"})
    # Baseline = latest existing trade ts, so only trades AFTER adding are copied.
    baseline = poller.latest_trade_ts(address)
    if baseline <= 0:
        baseline = time.time()
    wallet = db.add_wallet(name, address, baseline_ts=baseline)
    return {"wallet": wallet}


@app.patch("/api/wallets/{wallet_id}")
def toggle_wallet(wallet_id: int, active: bool = Form(...)) -> dict:
    if not db.get_wallet(wallet_id):
        return JSONResponse(status_code=404, content={"error": "wallet not found"})
    db.set_wallet_active(wallet_id, active)
    return {"wallet": db.get_wallet(wallet_id)}


@app.delete("/api/wallets/{wallet_id}")
def delete_wallet(wallet_id: int) -> dict:
    db.delete_wallet(wallet_id)
    return {"deleted": True}


@app.get("/api/entries")
def list_entries(wallet_id: int | None = Query(None),
                 status: str | None = Query(None),
                 page: int = Query(1, ge=1),
                 page_size: int = Query(20, ge=1, le=100)) -> dict:
    return db.list_entries(wallet_id=wallet_id, status=status, page=page,
                           page_size=page_size)


@app.get("/api/wallets/{wallet_id}/entries")
def wallet_entries(wallet_id: int,
                   page: int = Query(1, ge=1),
                   page_size: int = Query(20, ge=1, le=100)) -> dict:
    if not db.get_wallet(wallet_id):
        return JSONResponse(status_code=404, content={"error": "wallet not found"})
    payload = db.list_entries(wallet_id=wallet_id, page=page, page_size=page_size)
    payload["stats"] = res.wallet_stats(wallet_id)
    return payload


@app.get("/api/results")
def results_route() -> dict:
    return {
        "wallets": res.all_wallet_stats(),
        "portfolio": res.portfolio_summary(refresh_prices=False),
    }


@app.get("/api/portfolio")
def portfolio_route(refresh: bool = Query(True)) -> dict:
    return res.portfolio_summary(refresh_prices=refresh)


@app.post("/api/poll")
def poll_now() -> dict:
    recorded = poller.run_once()
    return {"recorded": recorded}


@app.post("/api/portfolio/reset")
def reset_route() -> dict:
    db.reset_paper()
    return {"reset": True, "portfolio": res.portfolio_summary(refresh_prices=False)}


# Constants surfaced for the UI (slippage cap, size bounds, starting balance).
@app.get("/api/config")
def config_route() -> dict:
    return {
        "slippage_cap": ce.SLIPPAGE_CAP,
        "max_usd": ce.MAX_USD,
        "min_usd": ce.MIN_USD,
        "starting_balance": db.STARTING_BALANCE,
        "disclaimer": "Paper trading simulation — not financial advice. "
                      "Real trading involves risk of loss.",
    }
