"""Aggregation for the Resultados tab and the paper portfolio banner.

Per-wallet KPIs are derived purely from the `entries` table (no network). The
portfolio banner optionally refreshes open-position prices from the CLOB.
"""
from __future__ import annotations

import db
import deps


def _safe_div(a: float, b: float) -> float:
    return a / b if b else 0.0


def wallet_stats(wallet_id: int, db_path: str = db.DEFAULT_DB) -> dict:
    """P&L, ROI, win rate, avg slippage, executed/failed % for one wallet."""
    conn = db.connect(db_path)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM entries WHERE wallet_id = ?", (wallet_id,)
        ).fetchall()]
    finally:
        conn.close()

    n = len(rows)
    executed = [e for e in rows if e["status"] == "EXECUTED"]
    skipped = [e for e in rows if e["status"] == "SKIPPED"]
    realized = [e for e in executed if e.get("realized_pnl") is not None]
    wins = [e for e in realized if e["result_status"] == "WIN"]
    losses = [e for e in realized if e["result_status"] == "LOSS"]
    slippages = [e["slippage_pct"] for e in executed if e.get("slippage_pct") is not None]
    invested = sum(e["executed_usd"] or 0.0
                   for e in executed if e["copy_action"] == "BUY")
    total_pnl = sum(e["realized_pnl"] or 0.0 for e in realized)

    return {
        "wallet_id": wallet_id,
        "n_entries": n,
        "n_executed": len(executed),
        "n_skipped": len(skipped),
        "pct_executed": round(_safe_div(len(executed), n), 4),
        "pct_failed": round(_safe_div(len(skipped), n), 4),
        "invested": round(invested, 2),
        "total_pnl": round(total_pnl, 2),
        "roi": round(_safe_div(total_pnl, invested), 4),
        "win_rate": round(_safe_div(len(wins), len(wins) + len(losses)), 4),
        "n_wins": len(wins),
        "n_losses": len(losses),
        "avg_slippage": round(_safe_div(sum(slippages), len(slippages)), 4),
    }


def all_wallet_stats(db_path: str = db.DEFAULT_DB) -> list[dict]:
    out = []
    for w in db.list_wallets(db_path=db_path):
        s = wallet_stats(w["id"], db_path)
        s.update({"name": w["name"], "address": w["address"], "active": w["active"]})
        out.append(s)
    return out


def portfolio_summary(refresh_prices: bool = True,
                      db_path: str = db.DEFAULT_DB) -> dict:
    """Paper mock wallet: cash, open positions valued live, realized + unrealized P&L."""
    state = db.get_paper_state(db_path)
    cash = float(state.get("cash_balance", 0.0))
    starting = float(state.get("starting_balance", db.STARTING_BALANCE))

    positions = []
    positions_value = 0.0
    unrealized = 0.0
    for p in db.list_open_paper_positions(db_path):
        shares = float(p["shares"] or 0.0)
        avg = float(p["avg_entry"] or 0.0)
        price = avg
        if refresh_prices and p.get("token_id"):
            try:
                price = deps.fetch_midpoint(p["token_id"])
            except Exception:  # noqa: BLE001 — fall back to entry cost
                price = avg
        value = shares * price
        positions_value += value
        unrealized += shares * (price - avg)
        positions.append({
            "wallet_id": p["wallet_id"],
            "wallet_name": p.get("wallet_name"),
            "condition_id": p["condition_id"],
            "market_question": p.get("market_question"),
            "market_url": p.get("market_url"),
            "side": p.get("side"),
            "shares": round(shares, 4),
            "avg_entry": round(avg, 6),
            "current_price": round(price, 6),
            "value": round(value, 2),
            "unrealized_pnl": round(shares * (price - avg), 2),
        })

    realized = _realized_total(db_path)
    total_value = cash + positions_value
    return {
        "starting_balance": round(starting, 2),
        "cash_balance": round(cash, 2),
        "positions_value": round(positions_value, 2),
        "total_value": round(total_value, 2),
        "realized_pnl": round(realized, 2),
        "unrealized_pnl": round(unrealized, 2),
        "total_pnl": round(total_value - starting, 2),
        "total_pnl_pct": round(_safe_div(total_value - starting, starting), 4),
        "num_open_positions": len(positions),
        "open_positions": positions,
    }


def _realized_total(db_path: str) -> float:
    conn = db.connect(db_path)
    try:
        r = conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) FROM entries "
            "WHERE realized_pnl IS NOT NULL"
        ).fetchone()
        return float(r[0] or 0.0)
    finally:
        conn.close()
