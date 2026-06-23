#!/usr/bin/env python3
"""Offline tests for the soccer walk-forward backtest (Layer 4). No network.

Run: python polymarket-soccer-goals/scripts/test_backtest_soccer.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtest_soccer as bt  # noqa: E402


class TestOddsHelpers(unittest.TestCase):
    def test_to_implied_and_devig(self):
        self.assertAlmostEqual(bt.to_implied_prob(-110), 110 / 210, places=6)
        self.assertAlmostEqual(bt.to_implied_prob(1.91), 1 / 1.91, places=6)
        self.assertAlmostEqual(bt.to_implied_prob(0.524), 0.524, places=6)
        fair = bt.devig_two_way(0.55, 0.55)
        self.assertAlmostEqual(fair[0], 0.5, places=6)
        self.assertIsNone(bt.devig_two_way(0, 0.5))


class TestTeamFactors(unittest.TestCase):
    def test_warmup_blocks_until_enough_games(self):
        tf = bt.TeamFactors(warmup=3)
        self.assertIsNone(tf.lambdas_for("a", "b", -0.1))   # no games yet
        for _ in range(3):
            tf.update("a", "b", 2, 1)
            tf.update("a", "c", 1, 1)
            tf.update("b", "c", 1, 1)
        lam = tf.lambdas_for("a", "b", -0.1)
        self.assertIsNotNone(lam)
        self.assertTrue(all(dc_lo <= x <= dc_hi for x in lam
                            for dc_lo, dc_hi in [(0.1, 6.0)]))

    def test_no_lookahead(self):
        # factors_for must not see the current match's result.
        tf = bt.TeamFactors(warmup=1)
        tf.update("a", "b", 3, 0)
        before = tf.lambdas_for("a", "b", -0.1)
        tf.update("a", "b", 0, 5)          # a big upset AFTER
        # The pre-update lambdas were computed without the upset (deterministic check:
        # recomputing on a fresh object fed only the first match gives the same value).
        tf2 = bt.TeamFactors(warmup=1)
        tf2.update("a", "b", 3, 0)
        self.assertEqual(before, tf2.lambdas_for("a", "b", -0.1))


def _synth_csv(path, seasons=2, teams=8, seed=11):
    import random
    rng = random.Random(seed)
    rows = ["date,home,away,home_goals,away_goals,over_odds,under_odds"]
    tnames = [f"t{i}" for i in range(teams)]
    day = 1
    for s in range(seasons):
        for _ in range(120):
            h, a = rng.sample(tnames, 2)
            hg = min(7, int(rng.expovariate(1 / 1.5)))
            ag = min(7, int(rng.expovariate(1 / 1.2)))
            rows.append(f"20{20+s}-01-{day%28+1:02d},{h},{a},{hg},{ag},1.95,1.95")
            day += 1
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(rows) + "\n")


class TestBacktest(unittest.TestCase):
    def test_runs_and_scores_both_markets(self):
        with tempfile.TemporaryDirectory() as d:
            csv = os.path.join(d, "g.csv")
            _synth_csv(csv)
            games = bt.load_games(csv)
            self.assertGreater(len(games), 100)
            rep = bt.run_backtest(games, warmup=6)
            self.assertGreater(rep["modeled"], 0)
            # Layer 4 metrics present and sane.
            self.assertIsNotNone(rep["crps"])
            self.assertGreater(rep["crps"], 0.0)
            self.assertTrue(0.0 <= rep["over_under"]["brier"] <= 1.0)
            self.assertTrue(0.0 <= rep["btts"]["brier"] <= 1.0)
            # Coverage of an honest interval should be in a sane band (not 0 or 1).
            self.assertGreater(rep["coverage80"], 0.5)
            self.assertLessEqual(rep["coverage80"], 1.0)
            # Market benchmark present because odds were supplied.
            self.assertIsNotNone(rep["market_over_under"])
            self.assertIn("Brier", bt.format_report(rep))

    def test_load_defaults_line_to_2pt5(self):
        with tempfile.TemporaryDirectory() as d:
            csv = os.path.join(d, "g.csv")
            with open(csv, "w") as fh:
                fh.write("date,home,away,home_goals,away_goals\n2024-01-01,a,b,2,1\n")
            games = bt.load_games(csv)
            self.assertEqual(games[0]["line"], 2.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
