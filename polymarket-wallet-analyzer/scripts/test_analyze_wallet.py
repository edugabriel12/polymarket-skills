#!/usr/bin/env python3
"""Offline tests for the wallet-analyzer engine (no network)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import analyze_wallet as wa  # noqa: E402


class TestEndInPast(unittest.TestCase):
    def test_naive_timestamp_does_not_crash(self):
        # Polymarket's endDate can arrive WITHOUT a timezone. Comparing a naive datetime to
        # an aware now() used to raise "can't compare offset-naive and offset-aware datetimes".
        self.assertTrue(wa._end_in_past("2020-01-01T00:00:00"))     # naive, past
        self.assertFalse(wa._end_in_past("2090-01-01T00:00:00"))    # naive, future

    def test_aware_timestamps(self):
        self.assertTrue(wa._end_in_past("2020-01-01T00:00:00Z"))
        self.assertFalse(wa._end_in_past("2090-01-01T00:00:00+00:00"))

    def test_empty_and_garbage(self):
        self.assertFalse(wa._end_in_past(None))
        self.assertFalse(wa._end_in_past(""))
        self.assertFalse(wa._end_in_past("not-a-date"))


class TestBuildMarketRecords(unittest.TestCase):
    def test_naive_enddate_position_settles_without_crash(self):
        positions = [{"conditionId": "0x1", "endDate": "2020-01-01T00:00:00",
                      "curPrice": 1.0, "cashPnl": 5.0, "realizedPnl": 5.0,
                      "initialValue": 10.0, "title": "Some market"}]
        recs = wa.build_market_records(positions, {}, None)
        self.assertEqual(len(recs), 1)
        self.assertTrue(recs[0]["resolved"])          # end date in the past -> resolved
        self.assertTrue(recs[0]["won"])               # cashPnl > 0


class TestReconstructTradePnl(unittest.TestCase):
    def test_average_cost_realized(self):
        # Buy 10@0.4, buy 10@0.6 (avg 0.5), sell 10@0.7 -> realized (0.7-0.5)*10 = 2.0
        trades = [
            {"conditionId": "c", "asset": "t", "side": "BUY", "price": 0.4, "size": 10},
            {"conditionId": "c", "asset": "t", "side": "BUY", "price": 0.6, "size": 10},
            {"conditionId": "c", "asset": "t", "side": "SELL", "price": 0.7, "size": 10},
        ]
        out = wa.reconstruct_trade_pnl(trades)
        self.assertAlmostEqual(out["c"]["realized"], 2.0, places=6)
        self.assertAlmostEqual(out["c"]["open_shares"], 10.0, places=6)


class TestTradeOwnership(unittest.TestCase):
    ADDR = "0x2d44274747466c0936c3e01d5a5ad6c260d97023"
    OTHER = "0x000000000000000000000000000000000000dead"

    def test_trade_owner_reads_known_fields(self):
        self.assertEqual(wa.trade_owner({"proxyWallet": self.ADDR.upper()}), self.ADDR.lower())
        self.assertEqual(wa.trade_owner({"user": self.ADDR}), self.ADDR.lower())
        self.assertEqual(wa.trade_owner({"maker": self.ADDR}), self.ADDR.lower())
        self.assertIsNone(wa.trade_owner({"title": "no owner here"}))

    def test_owned_trades_drops_foreign_trades(self):
        # The exact FuuUuUu symptom: the feed carries another wallet's non-weather
        # trades. They must be dropped, keeping only the wallet's own weather trade.
        feed = [
            {"proxyWallet": self.ADDR, "title": "Highest temperature in Tokyo?"},
            {"proxyWallet": self.OTHER, "title": "Arsenal vs Chelsea"},
            {"proxyWallet": self.OTHER, "title": "Bitcoin above $100k?"},
        ]
        owned = wa.owned_trades(feed, self.ADDR)
        self.assertEqual(len(owned), 1)
        self.assertIn("Tokyo", owned[0]["title"])

    def test_owned_trades_all_foreign_returns_empty(self):
        # No trade belongs to the wallet -> copy/analyze nothing (never the global feed).
        feed = [{"proxyWallet": self.OTHER, "title": "CS2 Major"}]
        self.assertEqual(wa.owned_trades(feed, self.ADDR), [])

    def test_owned_trades_unknown_shape_falls_back(self):
        # No recognizable owner field anywhere -> cannot verify; keep raw feed.
        feed = [{"title": "mystery"}, {"conditionId": "0xabc"}]
        self.assertEqual(wa.owned_trades(feed, self.ADDR), feed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
