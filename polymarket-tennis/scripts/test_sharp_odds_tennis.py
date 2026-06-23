#!/usr/bin/env python3
"""Offline tests for the tennis sharp reference + the implausible-edge cap (no network).

Run: python polymarket-tennis/scripts/test_sharp_odds_tennis.py
"""

from __future__ import annotations

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sharp_odds_tennis as sot  # noqa: E402
import suggest_tennis as st  # noqa: E402


class TestDevigAndNorm(unittest.TestCase):
    def test_american_and_devig(self):
        self.assertAlmostEqual(sot.american_to_implied(-110), 110 / 210, places=6)
        fair = sot.devig(0.55, 0.55)
        self.assertAlmostEqual(fair[0], 0.5, places=6)
        self.assertIsNone(sot.devig(0, 0.5))

    def test_surname_normalization(self):
        self.assertEqual(sot.norm_player("Carlos Alcaraz"), "alcaraz")
        self.assertEqual(sot.norm_player("Alcaraz"), "alcaraz")
        self.assertEqual(sot.norm_player("Stefanos Tsitsipás"), "tsitsipas")

    def test_key_matches_full_vs_surname(self):
        self.assertEqual(sot._key("2026-06-21", "Carlos Alcaraz", "Novak Djokovic"),
                         sot._key("2026-06-21", "Alcaraz", "Djokovic"))


def _h2h_event(a, b, date, a_px=-150, b_px=+130):
    return {"home_team": a, "away_team": b, "commence_time": f"{date}T13:00:00Z",
            "id": f"{a}-{b}", "bookmakers": [{"key": "pinnacle", "markets": [{"key": "h2h",
            "outcomes": [{"name": a, "price": a_px}, {"name": b, "price": b_px}]}]}]}


class TestParse(unittest.TestCase):
    def test_parse_h2h_devigs(self):
        d = sot.parse_h2h([_h2h_event("Carlos Alcaraz", "Novak Djokovic", "2026-06-21",
                                      a_px=-200, b_px=+170)])
        pa = sot.sharp_win_ref(d, "2026-06-21", "Alcaraz", "Djokovic")  # surname + order-free
        self.assertIsNotNone(pa)
        self.assertGreater(pa, 0.5)            # -200 favourite -> fair > 0.5
        pb = sot.sharp_win_ref(d, "2026-06-21", "Djokovic", "Alcaraz")
        self.assertAlmostEqual(pa + pb, 1.0, places=6)

    def test_missing_ref(self):
        self.assertIsNone(sot.sharp_win_ref({}, "2026-06-21", "a", "b"))


class TestImplausibleEdgeCap(unittest.TestCase):
    def test_large_edge_capped(self):
        # Model says A wins 0.80, but A is priced 0.50 -> +30% edge, above the 15% cap. The
        # implausible side must be flagged and NEVER chosen (the opposite, negative-edge side
        # is then rejected by run()'s min_edge check).
        sides = [{"label": "A", "token": "a", "price": 0.50},
                 {"label": "B", "token": "b", "price": 0.50}]
        chosen, notes = st.pick_side(sides, 0.80, 0.0, 1.50, 3.00)
        self.assertTrue(next(n for n in notes if n["label"] == "A")["implausible"])
        self.assertNotEqual(chosen["side"] if chosen else None, "A")

    def test_plausible_edge_passes(self):
        sides = [{"label": "A", "token": "a", "price": 0.50},
                 {"label": "B", "token": "b", "price": 0.50}]
        chosen, _ = st.pick_side(sides, 0.58, 0.0, 1.50, 3.00)   # +8% edge, within cap
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["side"], "A")


class TestLoadSharpLogging(unittest.TestCase):
    """The loader's log must say whether the sharp slate loaded, is empty, or had no key."""

    def _run(self, lookup, key="k", tours=("tennis_atp",)):
        logs = []
        vlog = lambda *a, **k: logs.append(" ".join(str(x) for x in a))  # noqa: E731
        orig = (sot.fetch_active_tennis_keys, sot.fetch_sharp_tennis)
        saved_env = os.environ.pop("ODDS_API_KEY", None)
        try:
            sot.fetch_active_tennis_keys = lambda *a, **k: list(tours)
            sot.fetch_sharp_tennis = lambda *a, **k: lookup
            args = types.SimpleNamespace(no_sharp=False, odds_api_key=key,
                                         sharp_tours=None, sharp_min_reserve=0)
            out = st._load_sharp_lookup(args, "2026-06-21", vlog)
        finally:
            sot.fetch_active_tennis_keys, sot.fetch_sharp_tennis = orig
            if saved_env is not None:
                os.environ["ODDS_API_KEY"] = saved_env
        return out, "\n".join(logs)

    def test_empty_with_key_logs_off(self):
        out, log = self._run({})
        self.assertEqual(out, {})
        self.assertIn("divergence detector OFF", log)

    def test_loaded_logs_dated_count(self):
        lookup = {
            sot._key("2026-06-21", "Carlos Alcaraz", "Novak Djokovic"): {"alcaraz": 0.6, "djokovic": 0.4},
            sot._key("2026-06-22", "Player A", "Player B"): {"a": 0.5, "b": 0.5},
        }
        out, log = self._run(lookup)
        self.assertEqual(len(out), 2)
        self.assertIn("2 match(es) (1 dated 2026-06-21)", log)   # only one is today's slate

    def test_no_key_logs_none(self):
        out, log = self._run({}, key=None)
        self.assertEqual(out, {})
        self.assertIn("no ODDS_API_KEY", log)


if __name__ == "__main__":
    unittest.main(verbosity=2)
