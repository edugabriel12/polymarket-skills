#!/usr/bin/env python3
"""Offline tests for the sharp-close capture + line-drift CLV scoring (no network).

Run: python polymarket-mlb-totals/scripts/test_capture_close.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sharp_odds as so  # noqa: E402
import capture_close as cc  # noqa: E402
import clv_vs_sharp as cv  # noqa: E402


class TestLookupToRows(unittest.TestCase):
    def test_rows_for_date_only(self):
        lookup = {
            so._key("2026-06-23", "CHC", "NYM"): {"line": 8.5, "over_fair": 0.52, "under_fair": 0.48},
            so._key("2026-06-24", "SEA", "OAK"): {"line": 7.5, "over_fair": 0.50, "under_fair": 0.50},
        }
        rows = cc.lookup_to_rows(lookup, "2026-06-23")
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["date"], "2026-06-23")
        self.assertEqual({r["away"], r["home"]}, {"chc", "nym"})
        self.assertEqual(r["total_line"], 8.5)
        self.assertAlmostEqual(float(r["close_over_odds"]), 0.52)


class TestMergeAndRoundTrip(unittest.TestCase):
    def test_merge_upserts_by_team_set(self):
        existing = [{"date": "2026-06-23", "away": "chc", "home": "nym", "total_line": 8.5,
                     "close_over_odds": 0.50, "close_under_odds": 0.50}]
        # Same game (order flipped) with a fresh close -> should REPLACE, not duplicate.
        new = [{"date": "2026-06-23", "away": "nym", "home": "chc", "total_line": 9.0,
                "close_over_odds": 0.55, "close_under_odds": 0.45}]
        merged = cc.merge_rows(existing, new)
        self.assertEqual(len(merged), 1)
        self.assertEqual(float(merged[0]["total_line"]), 9.0)

    def test_write_then_load_sharp_csv_resolves_close(self):
        lookup = {so._key("2026-06-23", "CHC", "NYM"):
                  {"line": 8.5, "over_fair": 0.40, "under_fair": 0.60}}
        rows = cc.lookup_to_rows(lookup, "2026-06-23")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "close.csv")
            cc.write_csv(p, rows)
            loaded = so.load_sharp_csv(p)
        ref = so.sharp_ref(loaded, "2026-06-23", "chc", "nym", use_close=True)
        self.assertIsNotNone(ref)
        line, close_over = ref
        self.assertEqual(line, 8.5)
        self.assertAlmostEqual(close_over, 0.40, places=6)


class TestLineDriftCLV(unittest.TestCase):
    def test_same_line_is_direct(self):
        # Close line == bet line -> returns the close prob directly (exact).
        lookup = {so._key("2026-06-23", "cws", "nyy"):
                  {"line": 8.5, "close_over_fair": 0.40, "close_under_fair": 0.60}}
        sc = cv.sharp_close_over_at(lookup, "2026-06-23", "cws", "nyy", 8.5)
        self.assertAlmostEqual(sc, 0.40, places=6)

    def test_drifted_line_prices_off_close_mu(self):
        # Sharp close at 8.5 (over 0.40 -> mu < 8.5). Bet was at an ALTERNATE 7.5; P(over 7.5)
        # off the close mu must be HIGHER than 0.40 (lower line -> more overs).
        lookup = {so._key("2026-06-23", "cws", "nyy"):
                  {"line": 8.5, "close_over_fair": 0.40, "close_under_fair": 0.60}}
        sc = cv.sharp_close_over_at(lookup, "2026-06-23", "cws", "nyy", 7.5)
        self.assertIsNotNone(sc)
        self.assertGreater(sc, 0.40)

    def test_drifted_clv_no_longer_drops(self):
        # A bet at 7.5 with a sharp close at 8.5 used to score None (tolerance gate). Now it
        # produces a real CLV.
        preds = [{"game_slug": "mlb-cws-nyy-2026-06-23", "game_date": "2026-06-23",
                  "side": "OVER", "line": 7.5, "entry_price": 0.50, "status": "PENDENTE"}]
        lookup = {so._key("2026-06-23", "cws", "nyy"):
                  {"line": 8.5, "close_over_fair": 0.40, "close_under_fair": 0.60}}
        scored = cv.score(preds, lookup)
        self.assertEqual(len(scored), 1)
        self.assertIsNotNone(scored[0]["clv"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
