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


if __name__ == "__main__":
    unittest.main(verbosity=2)
