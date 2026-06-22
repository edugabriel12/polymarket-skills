#!/usr/bin/env python3
"""Offline tests for sharp-reference anchoring (divergence detector) + CLV vs sharp.

Run: python polymarket-mlb-totals/scripts/test_sharp_clv.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sharp_odds as so  # noqa: E402
import suggest_totals as st  # noqa: E402
import clv_vs_sharp as cv  # noqa: E402


class TestSharpOdds(unittest.TestCase):
    def test_american_and_devig(self):
        self.assertAlmostEqual(so.american_to_implied(-110), 110 / 210, places=6)
        fair = so.devig(0.55, 0.55)
        self.assertAlmostEqual(fair[0], 0.5, places=6)
        self.assertIsNone(so.devig(0, 0.5))

    def test_parse_oddsapi(self):
        events = [{"home_team": "Yankees", "away_team": "Red Sox",
                   "commence_time": "2026-06-21T23:00:00Z",
                   "bookmakers": [{"key": "pinnacle", "markets": [{"key": "totals", "outcomes": [
                       {"name": "Over", "price": -105, "point": 8.5},
                       {"name": "Under", "price": -105, "point": 8.5}]}]}]}]
        d = so.parse_oddsapi(events)
        rec = next(iter(d.values()))
        self.assertEqual(rec["line"], 8.5)
        self.assertAlmostEqual(rec["over_fair"], 0.5, places=6)

    def test_csv_and_lookup(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.csv")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("date,away,home,total_line,over_odds,under_odds,close_over_odds,close_under_odds\n")
                fh.write("2026-06-21,cws,nyy,8.5,-110,-110,-120,+100\n")
            lk = so.load_sharp_csv(p)
            self.assertAlmostEqual(so.sharp_over_prob(lk, "2026-06-21", "cws", "nyy", 8.5), 0.5, places=6)
            # close devigged: over -120 (0.545), under +100 (0.5) -> fair over ~0.522
            self.assertGreater(so.sharp_over_prob(lk, "2026-06-21", "cws", "nyy", 8.5, use_close=True), 0.5)
            self.assertIsNone(so.sharp_over_prob(lk, "2026-06-21", "cws", "nyy", 11.5))  # line mismatch


class TestDivergenceDetector(unittest.TestCase):
    def test_sharp_anchor_creates_edge_from_price_gap(self):
        # Polymarket prices Over at 0.58, but the SHARP fair Over is 0.50 -> Under is value.
        line = 8.5
        m = st.model_probabilities(line, 0.58, 100.0, {}, league_baseline=8.5,
                                   dispersion=2.0, sharp_over_price=0.50)
        self.assertTrue(m["sharp_anchored"])
        # Fair P(over) tracks the SHARP price (~0.50), not the Polymarket 0.58.
        self.assertAlmostEqual(m["p_over"], 0.50, delta=0.02)
        # Edge vs the Polymarket price: Under is +EV (model 0.50 over vs 0.58 priced).
        self.assertLess(m["p_over"], 0.58)             # Over overpriced on Polymarket
        self.assertGreater(m["p_under"], 0.42)         # Under underpriced -> edge

    def test_no_sharp_is_market_implied(self):
        # Without a sharp ref, fair == Polymarket price -> ~zero edge (anti-fabrication).
        m = st.model_probabilities(8.5, 0.55, 100.0, {}, league_baseline=8.5, dispersion=2.0)
        self.assertFalse(m["sharp_anchored"])
        self.assertAlmostEqual(m["p_over"], 0.55, delta=2e-3)


class TestClvVsSharp(unittest.TestCase):
    def test_clv_for(self):
        # Bet OVER at 0.50; sharp closes Over fair at 0.56 -> +0.06 CLV (you beat the close).
        self.assertAlmostEqual(cv.clv_for("OVER", 0.50, 0.56), 0.06, places=6)
        # Bet UNDER at 0.50; sharp closes Over 0.56 -> Under fair 0.44 -> -0.06 CLV.
        self.assertAlmostEqual(cv.clv_for("UNDER", 0.50, 0.56), -0.06, places=6)
        self.assertIsNone(cv.clv_for("OVER", 0.50, None))

    def test_score_and_report(self):
        preds = [
            {"game_slug": "mlb-cws-nyy-2026-06-21", "game_date": "2026-06-21",
             "side": "UNDER", "line": 8.5, "entry_price": 0.50, "status": "ACERTO"},
        ]
        # sharp close: Over fair ~0.40 -> Under fair 0.60; we paid 0.50 -> +0.10 CLV.
        lookup = {sharp_odds_key("2026-06-21", "cws", "nyy"):
                  {"line": 8.5, "close_over_fair": 0.40, "close_under_fair": 0.60}}
        scored = cv.score(preds, lookup)
        self.assertEqual(len(scored), 1)
        self.assertAlmostEqual(scored[0]["clv"], 0.10, places=6)
        rep = cv.report(scored)
        self.assertEqual(rep["under"]["n"], 1)
        self.assertGreater(rep["all"]["avg_clv"], 0)


def sharp_odds_key(date, away, home):
    import sharp_odds
    return sharp_odds._key(date, away, home)


if __name__ == "__main__":
    unittest.main(verbosity=2)
