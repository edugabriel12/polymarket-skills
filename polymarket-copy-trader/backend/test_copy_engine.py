"""Offline unit tests for the copy-trade core — synthetic order books, temp DB.

Run: `python test_copy_engine.py`  (also works under pytest).
"""
from __future__ import annotations

import os
import tempfile

import copy_engine as ce
import db


def _fresh_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    db.init_db(path)
    return path


def _wallet(path: str) -> int:
    return db.add_wallet("Test", "0x" + "a" * 40, baseline_ts=0.0, db_path=path)["id"]


def _buy_trade(cond="0xc1", token="123", price=0.52, size=100.0):
    return {"conditionId": cond, "asset": token, "price": price, "size": size,
            "title": "Will it rain in NYC?", "side": "BUY", "eventSlug": "rain-nyc",
            "outcome": "YES", "timestamp": 1000}


def _sell_trade(cond="0xc1", token="123", price=0.60, size=100.0):
    t = _buy_trade(cond, token, price, size)
    t["side"] = "SELL"
    return t


# ---------------------------------------------------------------------------
# BUY sizing
# ---------------------------------------------------------------------------
def test_buy_caps_at_100():
    path = _fresh_db()
    wid = _wallet(path)
    # Deep book at/near best -> slippage-max would be >$100, so we cap at $100.
    ob = {"best_ask": 0.50, "best_bid": 0.49,
          "asks": [{"price": 0.50, "size": 1000}, {"price": 0.55, "size": 1000}],
          "bids": [{"price": 0.49, "size": 1000}]}
    entry = ce.process_buy(wid, _buy_trade(), ob, volume_24h=50000.0, db_path=path)
    assert entry["status"] == "EXECUTED", entry
    assert entry["executed_usd"] <= ce.MAX_USD + 1e-6, entry["executed_usd"]
    assert entry["executed_usd"] >= ce.MAX_USD - 0.5, entry["executed_usd"]
    assert entry["slippage_pct"] <= ce.SLIPPAGE_CAP + 1e-9
    # cash reduced from 10k by ~cost
    assert db.get_cash(path) < db.STARTING_BALANCE
    pos = db.get_paper_position(wid, "0xc1", path)
    assert pos and pos["shares"] > 0
    print("ok test_buy_caps_at_100")


def test_buy_below_floor_skips():
    path = _fresh_db()
    wid = _wallet(path)
    # Tiny top level then a jump beyond the 20% cap -> max sizable < $5.
    ob = {"best_ask": 0.50, "best_bid": 0.49,
          "asks": [{"price": 0.50, "size": 5}, {"price": 0.90, "size": 1000}],
          "bids": [{"price": 0.49, "size": 1000}]}
    entry = ce.process_buy(wid, _buy_trade(), ob, volume_24h=100.0, db_path=path)
    assert entry["status"] == "SKIPPED", entry
    assert "floor" in entry["skip_reason"], entry["skip_reason"]
    assert db.get_cash(path) == db.STARTING_BALANCE  # untouched
    print("ok test_buy_below_floor_skips")


def test_buy_respects_slippage_cap_size():
    path = _fresh_db()
    wid = _wallet(path)
    # best 0.50, cap 0.60. Second level 0.70 is beyond cap; only ~ first levels fill.
    ob = {"best_ask": 0.50, "best_bid": 0.49,
          "asks": [{"price": 0.50, "size": 100}, {"price": 0.55, "size": 100},
                   {"price": 0.70, "size": 1000}],
          "bids": [{"price": 0.49, "size": 1000}]}
    entry = ce.process_buy(wid, _buy_trade(), ob, volume_24h=100.0, db_path=path)
    assert entry["status"] == "EXECUTED", entry
    assert entry["slippage_pct"] <= ce.SLIPPAGE_CAP + 1e-9, entry["slippage_pct"]
    print("ok test_buy_respects_slippage_cap_size")


# ---------------------------------------------------------------------------
# SELL
# ---------------------------------------------------------------------------
def _seed_position(path, wid):
    ob = {"best_ask": 0.50, "best_bid": 0.49,
          "asks": [{"price": 0.50, "size": 1000}, {"price": 0.55, "size": 1000}],
          "bids": [{"price": 0.49, "size": 1000}]}
    ce.process_buy(wid, _buy_trade(), ob, volume_24h=50000.0, db_path=path)
    return db.get_paper_position(wid, "0xc1", path)


def test_sell_within_cap_executes_and_realizes_win():
    path = _fresh_db()
    wid = _wallet(path)
    pos = _seed_position(path, wid)
    # High, deep bid -> full close within slippage, and price above entry -> WIN.
    ob = {"best_ask": 0.61, "best_bid": 0.60,
          "asks": [{"price": 0.61, "size": 1000}],
          "bids": [{"price": 0.60, "size": 100000}]}
    entry = ce.process_sell(wid, _sell_trade(), ob, sold_shares=100.0,
                            holder_shares_before=100.0, db_path=path)
    assert entry["status"] == "EXECUTED", entry
    assert entry["result_status"] == "WIN", entry
    assert entry["realized_pnl"] > 0, entry
    closed = db.get_paper_position(wid, "0xc1", path)
    assert closed["closed"] == 1 or closed["shares"] <= 1e-6
    print("ok test_sell_within_cap_executes_and_realizes_win")


def test_sell_exceeding_slippage_skips():
    path = _fresh_db()
    wid = _wallet(path)
    _seed_position(path, wid)
    # Thin top bid then a cliff below the 20% floor -> only a few $ sellable.
    ob = {"best_ask": 0.61, "best_bid": 0.60,
          "asks": [{"price": 0.61, "size": 1000}],
          "bids": [{"price": 0.60, "size": 10}, {"price": 0.30, "size": 100000}]}
    entry = ce.process_sell(wid, _sell_trade(), ob, sold_shares=100.0,
                            holder_shares_before=100.0, db_path=path)
    assert entry["status"] == "SKIPPED", entry
    assert "slippage" in entry["skip_reason"], entry["skip_reason"]
    # position untouched
    pos = db.get_paper_position(wid, "0xc1", path)
    assert pos["shares"] > 0 and int(pos["closed"]) == 0
    print("ok test_sell_exceeding_slippage_skips")


def test_sell_proportional_fraction():
    path = _fresh_db()
    wid = _wallet(path)
    pos = _seed_position(path, wid)
    pos_shares = pos["shares"]
    ob = {"best_ask": 0.61, "best_bid": 0.60,
          "asks": [{"price": 0.61, "size": 1000}],
          "bids": [{"price": 0.60, "size": 100000}]}
    # Wallet sold half of its 200-share holding -> paper should sell ~half.
    entry = ce.process_sell(wid, _sell_trade(), ob, sold_shares=100.0,
                            holder_shares_before=200.0, db_path=path)
    assert entry["status"] == "EXECUTED", entry
    assert abs(entry["shares"] - pos_shares * 0.5) < 1e-3, (entry["shares"], pos_shares)
    remaining = db.get_paper_position(wid, "0xc1", path)
    assert abs(remaining["shares"] - pos_shares * 0.5) < 1e-2, remaining["shares"]
    print("ok test_sell_proportional_fraction")


def test_sell_without_position_skips():
    path = _fresh_db()
    wid = _wallet(path)
    ob = {"best_ask": 0.61, "best_bid": 0.60,
          "asks": [{"price": 0.61, "size": 1000}],
          "bids": [{"price": 0.60, "size": 100000}]}
    entry = ce.process_sell(wid, _sell_trade(), ob, sold_shares=50.0,
                            holder_shares_before=50.0, db_path=path)
    assert entry["status"] == "SKIPPED", entry
    assert "no paper position" in entry["skip_reason"], entry["skip_reason"]
    print("ok test_sell_without_position_skips")


def _run_all():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\nALL COPY-ENGINE TESTS PASSED")


if __name__ == "__main__":
    _run_all()
