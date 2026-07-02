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
             ("fetch_trades", "fetch_orderbook", "market_volume", "fetch_midpoint",
              "lookup_market")}
    deps.fetch_trades = lambda api, addr, n: TRADES
    deps.fetch_orderbook = lambda token, depth=100: BOOK
    deps.market_volume = lambda token: 25000.0
    deps.fetch_midpoint = lambda token: 0.55  # still open during the poll
    deps.lookup_market = lambda token: {"closed": False}  # not resolved yet
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

        # Price spikes to 0.99 but the market has NOT resolved yet -> the position
        # must stay OPEN and pay out NOTHING (the premature-settlement guard).
        deps.fetch_midpoint = lambda token: 0.99
        deps.lookup_market = lambda token: {"closed": False}
        cash_before = db.get_cash(path)
        poller.settle_wallet(wid, path)
        pos_open = db.get_paper_position(wid, COND, path)
        assert int(pos_open["closed"]) == 0, pos_open  # still open, no payout
        assert db.get_cash(path) == cash_before, "cash must not change pre-resolution"

        # Now the market genuinely resolves YES -> settle closes it and marks WIN.
        deps.lookup_market = lambda token: {"closed": True}
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


def test_non_weather_trade_ignored():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    db.init_db(path)
    wid = db.add_wallet("Whale", "0x" + "e" * 40, baseline_ts=0.0, db_path=path)["id"]
    wallet = db.get_wallet(wid, path)

    soccer = [{"conditionId": "0xsoccer", "asset": TOKEN, "side": "BUY", "price": 0.50,
               "size": 200.0, "title": "Arsenal vs Chelsea", "eventSlug": "epl-ars-che",
               "outcome": "YES", "timestamp": 100}]
    saved = {k: getattr(deps, k) for k in
             ("fetch_trades", "fetch_orderbook", "market_volume", "fetch_event_tags")}
    deps.fetch_trades = lambda api, addr, n: soccer
    deps.fetch_orderbook = lambda token, depth=100: BOOK
    deps.market_volume = lambda token: 25000.0
    deps.fetch_event_tags = lambda api, slug: ["soccer", "epl"]  # not weather
    try:
        recorded = poller.poll_wallet(wallet, api=object(), db_path=path)
        assert recorded == 0, recorded
        assert db.list_entries(wallet_id=wid, db_path=path)["total"] == 0
        assert db.get_paper_position(wid, "0xsoccer", path) is None
        assert db.get_cash(path) == db.STARTING_BALANCE  # untouched
        # Cursor still advances so the ignored trade isn't reprocessed forever.
        assert db.get_wallet(wid, path)["cursor_ts"] >= 100
        print("ok test_non_weather_trade_ignored")
    finally:
        for k, v in saved.items():
            setattr(deps, k, v)


def test_foreign_trade_not_copied():
    """A trade owned by a DIFFERENT wallet must never be copied (the FuuUuUu bug)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    db.init_db(path)
    addr = "0x" + "a" * 40
    wid = db.add_wallet("Weather", addr, baseline_ts=0.0, db_path=path)["id"]
    wallet = db.get_wallet(wid, path)

    # Feed carries a weather trade that belongs to ANOTHER wallet (proxyWallet).
    foreign = [{"conditionId": COND, "asset": TOKEN, "side": "BUY", "price": 0.50,
                "size": 200.0, "title": "Rain in NYC", "eventSlug": "rain-nyc",
                "outcome": "YES", "timestamp": 100, "proxyWallet": "0x" + "b" * 40}]
    saved = {k: getattr(deps, k) for k in
             ("fetch_trades", "fetch_orderbook", "market_volume")}
    deps.fetch_trades = lambda api, a, n: foreign
    deps.fetch_orderbook = lambda token, depth=100: BOOK
    deps.market_volume = lambda token: 25000.0
    try:
        recorded = poller.poll_wallet(wallet, api=object(), db_path=path)
        assert recorded == 0, recorded
        assert db.list_entries(wallet_id=wid, db_path=path)["total"] == 0
        assert db.get_cash(path) == db.STARTING_BALANCE  # untouched
        assert db.get_wallet(wid, path)["cursor_ts"] >= 100  # cursor still advances
        print("ok test_foreign_trade_not_copied")
    finally:
        for k, v in saved.items():
            setattr(deps, k, v)


def test_extreme_price_without_resolution_pays_nothing():
    """A favorite merely TRADING at 0.99 (not resolved) must not pay out $1/share."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    db.init_db(path)
    wid = db.add_wallet("Whale", "0x" + "d" * 40, baseline_ts=0.0, db_path=path)["id"]
    wallet = db.get_wallet(wid, path)

    buy = [{"conditionId": COND, "asset": TOKEN, "side": "BUY", "price": 0.02, "size": 100.0,
            "title": "Rain in NYC", "eventSlug": "rain-nyc", "outcome": "YES", "timestamp": 100}]
    saved = {k: getattr(deps, k) for k in
             ("fetch_trades", "fetch_orderbook", "market_volume", "fetch_midpoint",
              "lookup_market")}
    # Cheap book so $100 buys many shares (the inflation vector).
    cheap_book = {"best_ask": 0.02, "best_bid": 0.02,
                  "asks": [{"price": 0.02, "size": 1_000_000}],
                  "bids": [{"price": 0.02, "size": 1_000_000}]}
    deps.fetch_trades = lambda api, addr, n: buy
    deps.fetch_orderbook = lambda token, depth=100: cheap_book
    deps.market_volume = lambda token: 25000.0
    deps.fetch_midpoint = lambda token: 0.99          # price pinned high...
    deps.lookup_market = lambda token: {"closed": False}  # ...but NOT resolved
    try:
        poller.poll_wallet(wallet, api=object(), db_path=path)
        cash_after_buy = db.get_cash(path)
        assert cash_after_buy < db.STARTING_BALANCE, cash_after_buy  # spent on the buy
        pos = db.get_paper_position(wid, COND, path)
        assert pos and int(pos["closed"]) == 0, pos     # held open, no premature payout
        # No phantom $1/share windfall: cash is still ~ starting minus the $100 stake,
        # nowhere near the 50x ($5,000) a premature settlement would have credited.
        assert cash_after_buy < db.STARTING_BALANCE + 1.0, cash_after_buy
        print("ok test_extreme_price_without_resolution_pays_nothing")
    finally:
        for k, v in saved.items():
            setattr(deps, k, v)


if __name__ == "__main__":
    test_buy_then_partial_sell_then_settle()
    test_non_weather_trade_ignored()
    test_foreign_trade_not_copied()
    test_extreme_price_without_resolution_pays_nothing()
    print("\nPOLLER INTEGRATION TESTS PASSED")
