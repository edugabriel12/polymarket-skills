#!/usr/bin/env python3
"""Offline tests for the starting-pitcher run-prevention factors (no network)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import starter_factors as sf  # noqa: E402


class TestPureMath(unittest.TestCase):
    def test_parse_ip_thirds(self):
        self.assertAlmostEqual(sf.parse_ip("120.0"), 120.0)
        self.assertAlmostEqual(sf.parse_ip("120.1"), 120 + 1 / 3)
        self.assertAlmostEqual(sf.parse_ip("120.2"), 120 + 2 / 3)
        self.assertAlmostEqual(sf.parse_ip("7"), 7.0)
        self.assertIsNone(sf.parse_ip(None))

    def test_fip_known_value(self):
        # (13*15 + 3*40 - 2*180)/180 + 3.10 = -0.25 + 3.10 = 2.85
        stat = {"homeRuns": 15, "baseOnBalls": 40, "strikeOuts": 180, "inningsPitched": "180.0"}
        self.assertAlmostEqual(sf.fip_from_stat(stat), 2.85, places=4)

    def test_fip_small_sample_none(self):
        stat = {"homeRuns": 1, "baseOnBalls": 2, "strikeOuts": 8, "inningsPitched": "5.0"}
        self.assertIsNone(sf.fip_from_stat(stat))

    def test_fip_missing_fields_none(self):
        self.assertIsNone(sf.fip_from_stat({"homeRuns": 5}))

    def test_pitcher_factor_direction_and_clamp(self):
        self.assertLess(sf.pitcher_factor(2.85), 1.0)         # ace -> allows fewer
        self.assertGreater(sf.pitcher_factor(5.70), 1.0)      # bad -> allows more
        self.assertEqual(sf.pitcher_factor(1.0), sf.SP_FACTOR_LO)   # clamped low
        self.assertEqual(sf.pitcher_factor(10.0), sf.SP_FACTOR_HI)  # clamped high
        self.assertIsNone(sf.pitcher_factor(None))

    def test_parse_probables_maps_team_to_pitcher(self):
        schedule = {"dates": [{"games": [{"teams": {
            "home": {"team": {"id": 112}, "probablePitcher": {"id": 5001}},   # chc
            "away": {"team": {"id": 137}, "probablePitcher": {"id": 5002}}}}]}]}  # sf
        out = sf.parse_probables(schedule)
        self.assertEqual(out, {"chc": 5001, "sf": 5002})


class _FakeAPI:
    def __init__(self, schedule, stats):
        self.schedule, self.stats = schedule, stats

    def get(self, url, params=None):
        if url.endswith("/schedule"):
            return self.schedule
        if "/people/" in url:
            pid = int(url.split("/people/")[1].split("/")[0])
            return {"stats": [{"splits": [{"stat": self.stats.get(pid, {})}]}]}
        return {}


class _BoomAPI:
    def get(self, *a, **k):
        raise RuntimeError("no network")


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        sf._PROBABLES_CACHE.clear()
        sf._PITCHER_CACHE.clear()

    def test_starter_factor_resolves_ace(self):
        schedule = {"dates": [{"games": [{"teams": {
            "home": {"team": {"id": 112}, "probablePitcher": {"id": 5001}},
            "away": {"team": {"id": 137}, "probablePitcher": {"id": 5002}}}}]}]}
        stats = {5001: {"homeRuns": 15, "baseOnBalls": 40, "strikeOuts": 180,
                        "inningsPitched": "180.0"}}  # ace -> factor < 1
        api = _FakeAPI(schedule, stats)
        self.assertLess(sf.starter_factor(api, "chc", "2026-06-16", 2026), 1.0)
        self.assertIsNone(sf.starter_factor(api, "sf", "2026-06-16", 2026))  # no stats -> None
        self.assertIsNone(sf.starter_factor(api, "lad", "2026-06-16", 2026))  # not playing

    def test_best_effort_no_network(self):
        self.assertIsNone(sf.starter_factor(_BoomAPI(), "chc", "2026-06-16", 2026))


if __name__ == "__main__":
    unittest.main(verbosity=2)
