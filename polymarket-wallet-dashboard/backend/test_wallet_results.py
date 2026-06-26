#!/usr/bin/env python3
"""Offline tests for Phase-2 merge (CSV snapshot + live settled bets)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wallet_results as wres  # noqa: E402


def _csv_rec(cat, sub, conf, pnl, invested, won):
    return {"category": cat, "subcategory": sub, "confidence": conf, "total_pnl": pnl,
            "realized_pnl": pnl, "unrealized_pnl": 0.0, "invested": invested,
            "current_value": 0.0, "won": won, "n_trades": 1}


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


class TestMerge(unittest.TestCase):
    def test_open_excluded_settled_merged(self):
        csv = [
            _csv_rec("Soccer", "Over/Under gols", "Alta", 100.0, 100.0, True),
            _csv_rec("Soccer", "Over/Under gols", "Alta", -50.0, 50.0, False),
        ]
        live = [
            _bet("Soccer", "Over/Under gols", "Alta", 80.0, 100.0, "WON"),   # merges
            _bet("Baseball", "Moneyline", "Média", 20.0, 40.0, "WON"),       # new category
            _bet("Soccer", "Over/Under gols", "Alta", 0.0, 999.0, "OPEN"),   # excluded
        ]
        m = wres.merged_analysis(csv, live)
        self.assertEqual(m["live_settled"], 2)                  # only the 2 settled live bets
        ov = m["overall"]
        # CSV: 2 markets (1W/1L, pnl +50, invested 150). + 2 live settled (both WON, +100, inv 140)
        self.assertEqual(ov["markets"], 4)
        self.assertEqual(ov["wins"], 3)
        self.assertEqual(ov["losses"], 1)
        self.assertAlmostEqual(ov["total_pnl"], 150.0)         # 100-50+80+20
        self.assertAlmostEqual(ov["invested"], 290.0)          # 150 + 140
        cats = {c["category"] for c in m["by_category"]}
        self.assertEqual(cats, {"Soccer", "Baseball"})
        soccer = next(c for c in m["by_category"] if c["category"] == "Soccer")
        self.assertEqual(soccer["markets"], 3)                 # 2 csv + 1 live
        # confidence axis preserved
        self.assertTrue(soccer["by_confidence"])

    def test_no_live_is_just_csv(self):
        csv = [_csv_rec("Tennis", "Vencedor da partida", "Baixa", 10.0, 25.0, True)]
        m = wres.merged_analysis(csv, [])
        self.assertEqual(m["live_settled"], 0)
        self.assertEqual(m["overall"]["markets"], 1)

    def test_only_live(self):
        m = wres.merged_analysis([], [_bet("Soccer", "Moneyline (1X2)", "Alta", 30.0, 50.0, "WON")])
        self.assertEqual(m["overall"]["markets"], 1)
        self.assertAlmostEqual(m["overall"]["total_pnl"], 30.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
