#!/usr/bin/env python3
"""Offline tests for the MLB team run-environment factors (no network)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import team_factors as tf  # noqa: E402


def _standings(rows):
    """rows: [(team_id, runs_scored, runs_allowed, games)]."""
    return {"records": [{"teamRecords": [
        {"team": {"id": tid}, "runsScored": rs, "runsAllowed": ra, "gamesPlayed": gp}
        for (tid, rs, ra, gp) in rows]}]}


class TestParse(unittest.TestCase):
    def test_factors_relative_to_league(self):
        # COL scores a lot, allows a lot; OAK the opposite. League avg = 4.0 r/g.
        data = _standings([(115, 500, 480, 100), (133, 300, 320, 100)])
        f = tf.parse_standings_factors(data)
        self.assertAlmostEqual(f["col"]["off_factor"], 5.0 / 4.0)   # 500/100=5.0
        self.assertAlmostEqual(f["col"]["pitch_factor"], 4.8 / 4.0)
        self.assertAlmostEqual(f["oak"]["off_factor"], 3.0 / 4.0)
        self.assertAlmostEqual(f["oak"]["pitch_factor"], 3.2 / 4.0)

    def test_games_fallback_to_wins_losses(self):
        data = {"records": [{"teamRecords": [
            {"team": {"id": 112}, "runsScored": 90, "runsAllowed": 90,
             "wins": 10, "losses": 10},   # no gamesPlayed -> 20
            {"team": {"id": 137}, "runsScored": 90, "runsAllowed": 90,
             "wins": 10, "losses": 10}]}]}
        f = tf.parse_standings_factors(data)
        self.assertAlmostEqual(f["chc"]["off_factor"], 1.0)  # both identical -> league avg

    def test_unknown_team_and_missing_fields_skipped(self):
        data = _standings([(999999, 400, 400, 100), (112, 400, 400, 100)])
        f = tf.parse_standings_factors(data)
        self.assertNotIn(999999, f)
        self.assertIn("chc", f)

    def test_empty(self):
        self.assertEqual(tf.parse_standings_factors({}), {})
        self.assertEqual(tf.parse_standings_factors({"records": []}), {})


class TestFetchNoNetwork(unittest.TestCase):
    class _Boom:
        def get(self, *a, **k):
            raise RuntimeError("no network")

    def test_fetch_best_effort_empty(self):
        self.assertEqual(tf.fetch_run_factors(self._Boom(), 2026), {})

    def test_no_season(self):
        self.assertEqual(tf.fetch_run_factors(self._Boom(), 0), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
