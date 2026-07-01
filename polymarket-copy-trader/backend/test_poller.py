"""End-to-end poller test with a STUBBED datafeed (no network).

Feeds a wallet a BUY then a partial SELL, then resolves the market, and asserts the
entries, tracked-wallet holdings, paper position, cursor, and settlement P&L.
Run: `python test_poller.py`.
"""
from __future__ import annotations

import os
import tempfile

import db
import deps
import poller
import results as res

TOKEN = "1" * 40  # arbitrary; the engine doesn't validate token ids
COND = "0xcond1"

# One book that serves both sides: BUY walks asks (best 0.50), SELL walks bids (best 0.70).
BOOK = {
    "best_ask": 0.50, "best_bid": 0.70,
    "asks": [{"price": 0.50, "size": 1000}, {"price": 0.55, "size": 1000}],
    "bids": [{"price": 0.70, "size": 100000}],
}

TRADES = [
    {"conditionId": COND, "asset": TOKEN, "side": "BUY", "price": 0.50, "size": 200.0,
     "title": "Rain in NYC", "eventSlug": "rain-nyc", "outcome": "YES", "timestamp": 100},
    {"conditionId": COND, "asset": TOKEN, "side": "SELL", "price": 0.70, "size": 100.0,
     "title": "Rain in NYC", "eventSlug": "rain-nyc", "outcome": "YES", "timestamp": 200},
]


def test_buy_then_partial_sell_then_settle():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    db.init_db(path)
    wid = db.add_wallet("Whale", "0x" + "f" * 40, baseline_ts=0.0, db_path=path)["id"]
    wallet = db.get_wallet(wid, path)

    saved = {k: getattr(deps, k) for k in
             ("fetch_trades", "fetch_orderbook", "market_volume", "fetch_midpoint")}
    deps.fetch_trades = lambda api, addr, n: TRADES
    deps.fetch_orderbook = lambda token, depth=100: BOOK
    deps.market_volume = lambda token: 25000.0
    deps.fetch_midpoint = lambda token: 0.55  # still open during the poll
    try:
        recorded = poller.poll_wallet(wallet, api=object(), db_path=path)
        assert recorded == 2, recorded

        # Tracked-wallet holdings: bought 200, sold 100 -> 100 left.
        h = db.get_holding(wid, COND, path)
        assert abs(h["shares"] - 100.0) < 1e-6, h

        entries = db.list_entries(wallet_id=wid, db_path=path)["entries"]
        actions = sorted((e["copy_action"], e["status"]) for e in entries)
        assert ("BUY", "EXECUTED") in actions, actions
        assert ("SELL", "EXECUTED") in actions, actions

        assert db.get_wallet(wid, path)["cursor_ts"] >= 200  # cursor advanced

        pos = db.get_paper_position(wid, COND, path)
        assert pos["shares"] > 0 and int(pos["closed"]) == 0, pos

        # Market resolves YES -> settle_wallet closes the position and marks WIN.
        deps.fetch_midpoint = lambda token: 0.99
        poller.settle_wallet(wid, path)
        pos2 = db.get_paper_position(wid, COND, path)
        assert int(pos2["closed"]) == 1, pos2
        buy_entry = [e for e in db.list_entries(wallet_id=wid, db_path=path)["entries"]
                     if e["copy_action"] == "BUY"][0]
        assert buy_entry["result_status"] == "WIN", buy_entry

        summary = res.portfolio_summary(refresh_prices=False, db_path=path)
        assert summary["num_open_positions"] == 0, summary
        stats = res.wallet_stats(wid, path)
        assert stats["n_executed"] == 2 and stats["n_wins"] >= 1, stats
        print("ok test_buy_then_partial_sell_then_settle "
              f"(realized={summary['realized_pnl']}, cash={summary['cash_balance']})")
    finally:
        for k, v in saved.items():
            setattr(deps, k, v)


if __name__ == "__main__":
    test_buy_then_partial_sell_then_settle()
    print("\nPOLLER INTEGRATION TEST PASSED")
