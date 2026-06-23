#!/usr/bin/env python3
"""Offline tests for the soccer sharp reference (no network).

Run: python polymarket-soccer-goals/scripts/test_sharp_odds_soccer.py
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sharp_odds_soccer as so  # noqa: E402


class TestDevig(unittest.TestCase):
    def test_american_and_devig(self):
        self.assertAlmostEqual(so.american_to_implied(-110), 110 / 210, places=6)
        self.assertAlmostEqual(so.american_to_implied(0.52), 0.52, places=6)   # already a prob
        fair = so.devig(0.55, 0.55)
        self.assertAlmostEqual(fair[0], 0.5, places=6)
        self.assertIsNone(so.devig(0, 0.5))


class TestNameNormalization(unittest.TestCase):
    def test_accents_and_punct(self):
        self.assertEqual(so.norm_name("Côte d'Ivoire"), "cote divoire")
        self.assertEqual(so.norm_name("  Portugal "), "portugal")

    def test_aliases(self):
        self.assertEqual(so.norm_name("USA"), "united states")
        self.assertEqual(so.norm_name("DR Congo"), "congo dr")
        self.assertEqual(so.norm_name("South Korea"), "korea republic")

    def test_key_matches_across_spellings(self):
        # The sharp source ("DR Congo") and the Polymarket question ("Congo DR") must key alike.
        self.assertEqual(so._key("2026-06-23", "Colombia", "DR Congo"),
                         so._key("2026-06-23", "Congo DR", "colombia"))


class TestQuestionParsing(unittest.TestCase):
    def test_extracts_two_teams(self):
        self.assertEqual(so.extract_teams_from_question("Portugal vs Uzbekistan: O/U 2.5"),
                         ("portugal", "uzbekistan"))
        self.assertEqual(so.extract_teams_from_question("England x Ghana: Both teams to score?"),
                         ("england", "ghana"))
        self.assertEqual(so.extract_teams_from_question("Colombia vs. DR Congo"),
                         ("colombia", "congo dr"))

    def test_unparseable(self):
        self.assertIsNone(so.extract_teams_from_question("AL MVP 2026"))
        self.assertIsNone(so.extract_teams_from_question(""))


def _totals_event(home, away, date, point, over_px=-105, under_px=-105, alt=None):
    outcomes = [{"name": "Over", "price": over_px, "point": point},
                {"name": "Under", "price": under_px, "point": point}]
    if alt:
        outcomes += [{"name": "Over", "price": -2000, "point": alt},
                     {"name": "Under", "price": 1200, "point": alt}]
    return {"home_team": home, "away_team": away, "commence_time": f"{date}T18:00:00Z",
            "id": f"{home}-{away}", "bookmakers": [{"key": "pinnacle",
            "markets": [{"key": "totals", "outcomes": outcomes}]}]}


def _btts_event(home, away, date, yes_px=-110, no_px=-110):
    return {"home_team": home, "away_team": away, "commence_time": f"{date}T18:00:00Z",
            "bookmakers": [{"key": "pinnacle", "markets": [{"key": "btts", "outcomes": [
                {"name": "Yes", "price": yes_px}, {"name": "No", "price": no_px}]}]}]}


class TestParse(unittest.TestCase):
    def test_parse_totals_devigs_main_line(self):
        events = [_totals_event("England", "Ghana", "2026-06-23", 2.5)]
        d = so.parse_totals(events)
        ref = so.sharp_total_ref(d, "2026-06-23", "england", "ghana")
        self.assertIsNotNone(ref)
        self.assertEqual(ref[0], 2.5)
        self.assertAlmostEqual(ref[1], 0.5, places=6)

    def test_parse_totals_picks_balanced_alternate(self):
        # A stray alternate (over -2000) must not be chosen over the balanced 2.5 line.
        events = [_totals_event("England", "Ghana", "2026-06-23", 2.5, alt=0.5)]
        d = so.parse_totals(events)
        self.assertEqual(so.sharp_total_ref(d, "2026-06-23", "england", "ghana")[0], 2.5)

    def test_parse_btts_and_merge(self):
        totals = so.parse_totals([_totals_event("England", "Ghana", "2026-06-23", 2.5)])
        btts = so.parse_btts([_btts_event("England", "Ghana", "2026-06-23", -120, +100)])
        lk = so.merge_lookup(totals, btts)
        # devig(over -120=0.545, under +100=0.5) -> yes ~0.522
        y = so.sharp_btts_ref(lk, "2026-06-23", "ghana", "england")   # order-free
        self.assertGreater(y, 0.5)
        self.assertIsNotNone(so.sharp_total_ref(lk, "2026-06-23", "england", "ghana"))

    def test_missing_refs_return_none(self):
        self.assertIsNone(so.sharp_total_ref({}, "2026-06-23", "a", "b"))
        self.assertIsNone(so.sharp_btts_ref({}, "2026-06-23", "a", "b"))


class TestQuotaReserve(unittest.TestCase):
    def test_stops_at_reserve(self):
        # A fake requests whose /odds responses report a decreasing remaining quota; the
        # fetch must stop querying further leagues once remaining <= the reserve.
        import types

        class _Resp:
            def __init__(self, rem, payload):
                self.headers = {"x-requests-remaining": str(rem), "x-requests-used": "0"}
                self._p = payload
            def raise_for_status(self): pass
            def json(self): return self._p

        calls = {"n": 0}
        seq = [250, 150]  # second league call drops below a reserve of 200

        def fake_get(url, params=None, timeout=None):
            calls["n"] += 1
            rem = seq[min(calls["n"] - 1, len(seq) - 1)]
            return _Resp(rem, [_totals_event("A", "B", "2026-06-23", 2.5)])

        fake_requests = types.SimpleNamespace(get=fake_get)
        orig = sys.modules.get("requests")
        sys.modules["requests"] = fake_requests
        try:
            so.fetch_sharp_soccer("k", ["soccer_a", "soccer_b", "soccer_c"], with_btts=False,
                                  min_quota_reserve=200)
        finally:
            if orig is not None:
                sys.modules["requests"] = orig
            else:
                sys.modules.pop("requests", None)
        # league A (rem 250) queried, league B (rem 150) queried, then reserve hit -> stop
        # before league C. So at most 2 league calls, never 3.
        self.assertLessEqual(calls["n"], 2)


class TestEndToEndMatch(unittest.TestCase):
    def test_question_teams_resolve_sharp_ref(self):
        # The whole point: teams parsed from a Polymarket question resolve the sharp ref.
        lk = so.merge_lookup(
            so.parse_totals([_totals_event("Portugal", "Uzbekistan", "2026-06-23", 2.5, over_px=-130)]))
        a, b = so.extract_teams_from_question("Portugal vs Uzbekistan: O/U 2.5")
        ref = so.sharp_total_ref(lk, "2026-06-23", a, b)
        self.assertIsNotNone(ref)
        self.assertEqual(ref[0], 2.5)
        self.assertGreater(ref[1], 0.5)   # -130 over -> fair over > 0.5


if __name__ == "__main__":
    unittest.main(verbosity=2)
