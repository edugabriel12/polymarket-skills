#!/usr/bin/env python3
"""Offline tests for the MLB props feasibility classifier (no network).

Run: python polymarket-mlb-totals/scripts/test_props_scan.py
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import props_scan as ps  # noqa: E402


def _mkt(slug, question, outcomes, vol=1000.0, liq=800.0):
    return {"event_slug": slug, "slug": slug, "question": question, "outcomes": outcomes,
            "token_ids": ["t1", "t2"], "volume_24h": vol, "liquidity": liq}


class TestStatOf(unittest.TestCase):
    def test_recognizes_player_stats(self):
        self.assertEqual(ps.stat_of("Will Gerrit Cole record over 6.5 strikeouts?"), "strikeouts")
        self.assertEqual(ps.stat_of("Aaron Judge to hit a home run?"), "home_runs")
        self.assertEqual(ps.stat_of("Mookie Betts total bases Over 1.5"), "total_bases")
        self.assertEqual(ps.stat_of("Will Ohtani record a hit?"), "hits")
        self.assertIsNone(ps.stat_of("Will the Yankees beat the Red Sox?"))

    def test_commodity_hit_is_not_a_prop(self):
        # The bug from the first live run: "Will WTI hit $70" must NOT match 'hits'.
        self.assertIsNone(ps.stat_of("Will WTI Crude Oil (WTI) hit (LOW) $70 Week of June 22 2026?"))
        self.assertIsNone(ps.stat_of("Will Netflix (NFLX) hit (HIGH) $77.50?"))


class TestIsMlb(unittest.TestCase):
    def test_filters_to_mlb_prefix(self):
        self.assertTrue(ps.is_mlb({"event_slug": "mlb-tor-chc-2026-06-21"}))
        self.assertFalse(ps.is_mlb({"event_slug": "wta-lys-navarro-2026-06-21"}))   # tennis
        self.assertFalse(ps.is_mlb({"slug": "will-wti-dip-to-70-by-june-22-2026"}))  # commodity
        self.assertFalse(ps.is_mlb({"slug": "bitcoin-up-or-down-on-june-23-2026"}))


class TestClassify(unittest.TestCase):
    def test_player_prop(self):
        m = _mkt("mlb-gerrit-cole-strikeouts-2026-06-21",
                 "Gerrit Cole Over 6.5 strikeouts?", ["Over", "Under"])
        self.assertEqual(ps.classify(m), ("player_prop", "strikeouts"))

    def test_game_total(self):
        m = _mkt("mlb-cws-nyy-2026-06-21-total-8pt5", "Total runs O/U 8.5", ["Over", "Under"])
        self.assertEqual(ps.classify(m)[0], "game_total")

    def test_moneyline(self):
        m = _mkt("mlb-cws-nyy-2026-06-21", "Will the White Sox beat the Yankees?",
                 ["White Sox", "Yankees"])
        self.assertEqual(ps.classify(m)[0], "moneyline")

    def test_other(self):
        m = _mkt("mlb-al-mvp-2026", "AL MVP 2026", ["Judge", "Soto", "Witt"])
        self.assertEqual(ps.classify(m)[0], "other")


class TestBookMetrics(unittest.TestCase):
    def test_spread_and_depth(self):
        book = {"bids": [{"price": "0.48", "size": "100"}, {"price": "0.40", "size": "200"}],
                "asks": [{"price": "0.52", "size": "100"}, {"price": "0.60", "size": "300"}]}
        m = ps.book_metrics(book)
        self.assertAlmostEqual(m["bid"], 0.48)
        self.assertAlmostEqual(m["ask"], 0.52)
        self.assertAlmostEqual(m["spread"], 0.04, places=6)
        # within 5c of touch: bid 0.40 is 8c off (excluded), ask 0.60 is 8c off (excluded).
        self.assertAlmostEqual(m["depth_usd"], 0.48 * 100 + 0.52 * 100, places=2)

    def test_empty_book(self):
        m = ps.book_metrics({"bids": [], "asks": []})
        self.assertIsNone(m["spread"])
        self.assertEqual(m["depth_usd"], 0.0)


class TestVerdict(unittest.TestCase):
    def test_no_props(self):
        rep = {"by_kind": {"moneyline": {"n": 10}}, "top_props": []}
        self.assertIn("NO MLB player props", ps.verdict(rep, 500, 3))

    def test_viable(self):
        rep = {"by_kind": {"player_prop": {"n": 20, "n_liquid": 8}},
               "top_props": [{"depth_usd": 1200}, {"depth_usd": 900}]}
        self.assertIn("VIABLE", ps.verdict(rep, 500, 3))

    def test_thin(self):
        rep = {"by_kind": {"player_prop": {"n": 6, "n_liquid": 1}},
               "top_props": [{"depth_usd": 50}]}
        self.assertIn("THIN", ps.verdict(rep, 500, 3))


if __name__ == "__main__":
    unittest.main(verbosity=2)
