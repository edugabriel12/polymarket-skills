#!/usr/bin/env python3
"""Offline tests for the model suggestion → entry mapping (no subprocess/network)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import model_runner as mr  # noqa: E402
import entries as en  # noqa: E402


class TestSuggestionToEntry(unittest.TestCase):
    def test_soccer_total(self):
        sug = {"game": "epl-ars-che-2026-06-25-total-2pt5", "market": "TOTAL", "side": "OVER",
               "line": 2.5, "recommendation": {"price": 0.56}, "market_url": "u"}
        e = mr.suggestion_to_entry(sug, "soccer")
        self.assertEqual(e["category"], "Soccer")
        self.assertEqual(e["subcategory"], "Over/Under gols")
        self.assertEqual(e["side"], "OVER")
        self.assertEqual(e["unit"], 1.0)              # model = always 1U
        self.assertEqual(e["confidence"], "Alta")
        self.assertEqual(e["live"], en.PRELIVE)        # pregame
        self.assertAlmostEqual(e["odds"], 1 / 0.56, places=3)
        self.assertEqual(e["event"], "ARS vs CHE")
        self.assertEqual(e["source"], "model")

    def test_soccer_btts(self):
        sug = {"game": "bra-fla-pal-2026-06-25-btts", "market": "BTTS", "side": "YES",
               "recommendation": {"price": 0.5}}
        e = mr.suggestion_to_entry(sug, "soccer")
        self.assertEqual(e["subcategory"], "Ambas Marcam")

    def test_tennis_match(self):
        sug = {"game": "atp-alcaraz-sinner-2026-06-25", "market": "MATCH", "side": "ALCARAZ",
               "recommendation": {"price": 0.62}}
        e = mr.suggestion_to_entry(sug, "tennis")
        self.assertEqual(e["category"], "Tennis")
        self.assertEqual(e["subcategory"], "Vencedor da partida")
        self.assertEqual(e["unit"], 1.0)
        self.assertEqual(e["live"], en.PRELIVE)

    def test_zero_price_safe(self):
        e = mr.suggestion_to_entry({"game": "x-a-b-2026-06-25", "market": "MATCH", "side": "A",
                                    "recommendation": {"price": 0}}, "tennis")
        self.assertEqual(e["odds"], 0.0)

    def test_model_entries_maps_both(self):
        # Stub the runners so no subprocess runs.
        mr.run_soccer = lambda d: {"suggestions": [
            {"game": "epl-ars-che-2026-06-25-total-2pt5", "market": "TOTAL", "side": "OVER",
             "recommendation": {"price": 0.55}}]}
        mr.run_tennis = lambda d: {"suggestions": [
            {"game": "atp-alcaraz-sinner-2026-06-25", "market": "MATCH", "side": "ALCARAZ",
             "recommendation": {"price": 0.6}}]}
        ents = mr.model_entries("2026-06-25")
        self.assertEqual(len(ents), 2)
        self.assertEqual({e["category"] for e in ents}, {"Soccer", "Tennis"})
        self.assertTrue(all(e["unit"] == 1.0 and e["live"] == en.PRELIVE for e in ents))


if __name__ == "__main__":
    unittest.main(verbosity=2)
