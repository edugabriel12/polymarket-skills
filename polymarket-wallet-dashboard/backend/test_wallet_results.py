#!/usr/bin/env python3
"""Offline tests for Phase-2 merge (CSV snapshot + live settled bets)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wallet_results as wres  # noqa: E402


def _bet(cat, sub, conf, pnl, pos, status):
    return {"category": cat, "subcategory": sub, "confidence": conf, "pnl": pnl,
            "total_position": pos, "status": status, "side": "OVER"}


class TestBetToRecord(unittest.TestCase):
    def test_won_lost_void(self):
        self.assertEqual(wres.bet_to_record(_bet("Soccer", "x", "Alta", 50, 100, "WON"))["won"], True)
        self.assertEqual(wres.bet_to_record(_bet("Soccer", "x", "Alta", -100, 100, "LOST"))["won"], False)
        self.assertIsNone(wres.bet_to_record(_bet("Soccer", "x", "Alta", 0, 100, "VOID"))["won"])
        r = wres.bet_to_record(_bet("Soccer", "x", "Alta", 50, 100, "WON"))
        self.assertEqual(r["invested"], 100.0)
        self.assertEqual(r["total_pnl"], 50.0)


class TestLiveResults(unittest.TestCase):
    def test_only_settled_live_bets_count_no_csv(self):
        live = [
            _bet("Soccer", "Over/Under gols", "Alta", 80.0, 100.0, "WON"),
            _bet("Soccer", "Over/Under gols", "Alta", -50.0, 50.0, "LOST"),
            _bet("Baseball", "Moneyline", "Média", 20.0, 40.0, "WON"),
            _bet("Soccer", "Over/Under gols", "Alta", 0.0, 999.0, "OPEN"),   # excluded from figures
        ]
        m = wres.live_results(live)
        self.assertEqual(m["live_settled"], 3)
        self.assertEqual(m["live_open"], 1)
        ov = m["overall"]
        self.assertEqual(ov["markets"], 3)                      # 3 settled only — CSV never added
        self.assertEqual(ov["wins"], 2)
        self.assertEqual(ov["losses"], 1)
        self.assertAlmostEqual(ov["total_pnl"], 50.0)           # 80 - 50 + 20
        self.assertAlmostEqual(ov["invested"], 190.0)           # 100 + 50 + 40
        cats = {c["category"] for c in m["by_category"]}
        self.assertEqual(cats, {"Soccer", "Baseball"})
        soccer = next(c for c in m["by_category"] if c["category"] == "Soccer")
        self.assertTrue(soccer["by_confidence"])

    def test_empty_until_settled(self):
        m = wres.live_results([_bet("Soccer", "Moneyline (1X2)", "Alta", 0.0, 50.0, "OPEN")])
        self.assertEqual(m["overall"]["markets"], 0)            # no settled bets yet
        self.assertEqual(m["live_open"], 1)
        self.assertEqual(m["by_category"], [])

    def test_none(self):
        m = wres.live_results([])
        self.assertEqual(m["live_settled"], 0)
        self.assertEqual(m["overall"]["markets"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
