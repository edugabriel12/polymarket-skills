#!/usr/bin/env python3
"""Offline tests for tennis settlement — winner matching across feeds + the source chain.

The live bug: settlement fetched ONLY Sackmann, which 404'd for the season, so 0 pending
settled. Now it uses ratings_source.fetch_matches (Sackmann -> tennis-data fallback), and the
winner lookup matches by full-name OR surname pair so tennis-data's "Surname I." names still
settle predictions stored with full names.

Run: python polymarket-tennis/scripts/test_tennis_results.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tennis_results as tr  # noqa: E402
import tennis_predictions as tdb  # noqa: E402
import ratings_source  # noqa: E402


class TestWinnerLookup(unittest.TestCase):
    def test_full_name_feed(self):
        lk = tr.build_winner_lookup([
            {"winner": "Carlos Alcaraz", "loser": "Jannik Sinner"}])
        self.assertEqual(tr.lookup_winner(lk, "Carlos Alcaraz", "Jannik Sinner"), "Carlos Alcaraz")

    def test_surname_initial_feed_matches_full_name_prediction(self):
        # tennis-data.co.uk writes "Surname I." — must still settle a full-name prediction.
        lk = tr.build_winner_lookup([
            {"winner": "Alcaraz C.", "loser": "Sinner J."},
            {"winner": "Bautista Agut R.", "loser": "Norrie C."}])
        self.assertEqual(tr.lookup_winner(lk, "Carlos Alcaraz", "Jannik Sinner"), "Alcaraz C.")
        self.assertEqual(tr.lookup_winner(lk, "alcaraz", "sinner"), "Alcaraz C.")    # surname slug
        self.assertEqual(tr.lookup_winner(lk, "Roberto Bautista Agut", "Cameron Norrie"),
                         "Bautista Agut R.")

    def test_unknown_pair_returns_none(self):
        lk = tr.build_winner_lookup([{"winner": "Alcaraz C.", "loser": "Sinner J."}])
        self.assertIsNone(tr.lookup_winner(lk, "Rafael Nadal", "Novak Djokovic"))

    def test_ambiguous_surname_not_aliased(self):
        # Two matches with a shared surname pair -> the surname alias is unsafe, so omitted.
        lk = tr.build_winner_lookup([
            {"winner": "Williams S.", "loser": "Williams V."},
            {"winner": "Williams V.", "loser": "Williams S."}])
        # Full-name pairs still resolve; the bare-surname pair must NOT (ambiguous).
        self.assertIsNone(lk.get(frozenset({"williams", "williams"})))


class TestSettlePendingChain(unittest.TestCase):
    def setUp(self):
        self._orig = ratings_source.fetch_matches

    def tearDown(self):
        ratings_source.fetch_matches = self._orig

    def test_settles_from_surname_feed_via_chain(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "t.db")
            tdb.record_prediction({
                "match_slug": "atp-alcaraz-sinner-2026-06-25", "match_date": "2026-06-25",
                "tour": "atp", "surface": "clay", "side": "Carlos Alcaraz",
                "opponent": "Jannik Sinner", "entry_price": 0.5, "decimal_odds": 2.0,
                "model_prob": 0.6, "edge": 0.1, "confidence": 0.6, "size_pct": 0.02,
                "size_usd": 200.0, "kelly_fraction": 0.04, "used_external": 1, "fee_rate": 0.0,
                "strategy": "divergence", "market_url": "https://polymarket.com/x"}, db)
            self.assertEqual(len(tdb.get_predictions(db, status="PENDENTE")), 1)

            # Simulate Sackmann 404 -> tennis-data fallback returning "Surname I." names.
            ratings_source.fetch_matches = lambda tour, years=None, debug=False: [
                {"date": "20260625", "surface": "clay", "winner": "Alcaraz C.",
                 "loser": "Sinner J."}]
            out = tr.settle_pending(db, tour="atp")

            self.assertEqual(out["checked"], 1)
            self.assertEqual(len(out["settled"]), 1)
            self.assertEqual(out["games_matched"], 1)
            self.assertEqual(len(tdb.get_predictions(db, status="PENDENTE")), 0)   # no longer pending

    def test_no_feed_keeps_pending(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "t.db")
            tdb.record_prediction({
                "match_slug": "atp-a-b-2026-06-25", "match_date": "2026-06-25", "tour": "atp",
                "surface": "hard", "side": "A", "opponent": "B", "entry_price": 0.5,
                "decimal_odds": 2.0, "model_prob": 0.6, "edge": 0.1, "confidence": 0.6,
                "size_pct": 0.02, "size_usd": 200.0, "kelly_fraction": 0.04, "used_external": 0,
                "fee_rate": 0.0, "strategy": "x", "market_url": "u"}, db)
            ratings_source.fetch_matches = lambda tour, years=None, debug=False: []
            out = tr.settle_pending(db, tour="atp")
            self.assertEqual(out["checked"], 1)
            self.assertEqual(out["settled"], [])
            self.assertEqual(out["finals_found"], 0)
            self.assertTrue(any("EMPTY feed" in d for d in out["diagnostics"]))
            self.assertEqual(len(tdb.get_predictions(db, status="PENDENTE")), 1)   # stays pending

    def test_unsettled_diagnostic_explains_why(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "t.db")
            tdb.record_prediction({
                "match_slug": "atp-alcaraz-sinner-2026-06-25", "match_date": "2026-06-25",
                "tour": "atp", "surface": "clay", "side": "Carlos Alcaraz",
                "opponent": "Jannik Sinner", "entry_price": 0.5, "decimal_odds": 2.0,
                "model_prob": 0.6, "edge": 0.1, "confidence": 0.6, "size_pct": 0.02,
                "size_usd": 200.0, "kelly_fraction": 0.04, "used_external": 1, "fee_rate": 0.0,
                "strategy": "x", "market_url": "u"}, db)
            # Feed has OTHER matches (neither player) -> must explain "neither player in the feed".
            ratings_source.fetch_matches = lambda tour, years=None, debug=False: [
                {"date": "20260625", "surface": "clay", "winner": "Rafael Nadal",
                 "loser": "Novak Djokovic"}]
            out = tr.settle_pending(db, tour="atp")
            self.assertEqual(out["settled"], [])
            self.assertEqual(out["finals_found"], 1)
            joined = " ".join(out["diagnostics"])
            self.assertIn("UNSETTLED", joined)
            self.assertIn("neither player is in the feed", joined)
            self.assertIn("feed[atp] surname sample", joined)

    def test_each_prediction_settles_against_its_own_tour_feed(self):
        # The live bug: settlement queried only the ATP feed, so WTA predictions never settled.
        # Each prediction must settle against the feed of ITS OWN tour (atp-… vs wta-… slug).
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "t.db")
            tdb.record_prediction({
                "match_slug": "atp-alcaraz-sinner-2026-06-25", "match_date": "2026-06-25",
                "tour": "atp", "surface": "clay", "side": "Carlos Alcaraz",
                "opponent": "Jannik Sinner", "entry_price": 0.5, "decimal_odds": 2.0,
                "model_prob": 0.6, "edge": 0.1, "confidence": 0.6, "size_pct": 0.02,
                "size_usd": 200.0, "kelly_fraction": 0.04, "used_external": 1, "fee_rate": 0.0,
                "strategy": "x", "market_url": "u"}, db)
            tdb.record_prediction({
                "match_slug": "wta-swiatek-sabalenka-2026-06-25", "match_date": "2026-06-25",
                "tour": "wta", "surface": "clay", "side": "Iga Swiatek",
                "opponent": "Aryna Sabalenka", "entry_price": 0.5, "decimal_odds": 2.0,
                "model_prob": 0.6, "edge": 0.1, "confidence": 0.6, "size_pct": 0.02,
                "size_usd": 200.0, "kelly_fraction": 0.04, "used_external": 1, "fee_rate": 0.0,
                "strategy": "x", "market_url": "u"}, db)

            # Each tour's feed carries ONLY its own match — settlement must query both.
            feeds = {
                "atp": [{"date": "20260625", "surface": "clay", "winner": "Alcaraz C.",
                         "loser": "Sinner J."}],
                "wta": [{"date": "20260625", "surface": "clay", "winner": "Swiatek I.",
                         "loser": "Sabalenka A."}],
            }
            ratings_source.fetch_matches = lambda tour, years=None, debug=False: feeds[tour]
            out = tr.settle_pending(db)

            self.assertEqual(out["checked"], 2)
            self.assertEqual(out["games_matched"], 2)            # both tours settled
            self.assertEqual(len(out["settled"]), 2)
            self.assertEqual(len(tdb.get_predictions(db, status="PENDENTE")), 0)
            joined = " ".join(out["diagnostics"])
            self.assertIn("tour(s) ['atp', 'wta']", joined)


if __name__ == "__main__":
    unittest.main(verbosity=2)
