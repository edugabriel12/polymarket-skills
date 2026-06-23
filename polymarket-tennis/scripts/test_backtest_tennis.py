#!/usr/bin/env python3
"""Offline tests for the tennis walk-forward backtest (Layer 4) + ablations. No network.

Run: python polymarket-tennis/scripts/test_backtest_tennis.py
"""

from __future__ import annotations

import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtest_tennis as bt  # noqa: E402
import elo  # noqa: E402


class TestOdds(unittest.TestCase):
    def test_to_implied(self):
        self.assertAlmostEqual(bt.to_implied_prob(-150), 150 / 250, places=6)
        self.assertAlmostEqual(bt.to_implied_prob(2.5), 0.4, places=6)
        self.assertAlmostEqual(bt.to_implied_prob(0.62), 0.62, places=6)


class TestEloTracker(unittest.TestCase):
    def test_winner_rating_rises_no_lookahead(self):
        tr = bt.EloTracker()
        before = tr.rating("a", "hard")["elo"]
        tr.update("a", "b", "hard")
        self.assertGreater(tr.overall["a"], before)   # winner up
        self.assertLess(tr.overall["b"], before)      # loser down
        self.assertEqual(tr.matches("a"), 1)

    def test_surface_rating_separate_from_overall(self):
        tr = bt.EloTracker()
        tr.update("a", "b", "clay")
        # clay rating moved; grass rating untouched (still START).
        self.assertGreater(tr.surface["clay"]["a"], elo.START_ELO)
        self.assertEqual(tr.surface["grass"]["a"], elo.START_ELO)


def _synth_csv(path, n=1500, seed=3, with_hand=False, with_odds=True):
    """Players have latent skill; winner sampled by the true Elo-like prob → calibrated data."""
    rng = random.Random(seed)
    nplayers = 40
    skill = {f"p{i}": rng.gauss(1500, 200) for i in range(nplayers)}
    hand = {f"p{i}": ("L" if rng.random() < 0.15 else "R") for i in range(nplayers)}
    cols = ["date", "winner", "loser", "surface"]
    if with_odds:
        cols += ["w_odds", "l_odds"]
    if with_hand:
        cols += ["w_hand", "l_hand"]
    rows = [",".join(cols)]
    day = 1
    for _ in range(n):
        x, y = rng.sample(list(skill), 2)
        surf = rng.choice(elo.SURFACES)
        p_x = 1.0 / (1.0 + 10 ** ((skill[y] - skill[x]) / 400.0))
        if rng.random() < p_x:
            w, l = x, y
        else:
            w, l = y, x
        row = [f"2024-01-{day%28+1:02d}", w, l, surf]
        if with_odds:
            pw = 1.0 / (1.0 + 10 ** ((skill[l] - skill[w]) / 400.0))
            row += [f"{1/pw:.2f}", f"{1/(1-pw):.2f}"]
        if with_hand:
            row += [hand[w], hand[l]]
        rows.append(",".join(row))
        day += 1
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(rows) + "\n")


class TestBacktest(unittest.TestCase):
    def test_runs_and_scores(self):
        with tempfile.TemporaryDirectory() as d:
            csv = os.path.join(d, "m.csv")
            _synth_csv(csv)
            games = bt.load_games(csv)
            rep = bt.run_backtest(games, warmup=10)
            self.assertGreater(rep["modeled"], 100)
            # A model fit on data generated from Elo-like skills should beat a coin flip.
            self.assertLess(rep["model"]["brier"], 0.25)
            self.assertGreater(rep["accuracy"], 0.55)
            self.assertIsNotNone(rep["market"])     # odds supplied
            self.assertIn("Brier", bt.format_report(rep))

    def test_hand_ablation_reports_delta(self):
        with tempfile.TemporaryDirectory() as d:
            csv = os.path.join(d, "m.csv")
            _synth_csv(csv, with_hand=True)
            rep = bt.run_backtest(bt.load_games(csv), warmup=10, test_hand=True)
            ab = rep["ablation_handedness"]
            self.assertGreater(ab["n"], 0)
            self.assertIn("delta_brier", ab)
            # Data has no real handedness effect → adding the bump should NOT help.
            self.assertGreaterEqual(ab["delta_brier"], -0.001)

    def test_h2h_ablation_reports_delta(self):
        with tempfile.TemporaryDirectory() as d:
            csv = os.path.join(d, "m.csv")
            _synth_csv(csv)
            rep = bt.run_backtest(bt.load_games(csv), warmup=10, test_h2h=True)
            ab = rep["ablation_h2h"]
            self.assertGreater(ab["n"], 0)
            self.assertIn("verdict", ab)

    def test_hand_ablation_no_data(self):
        with tempfile.TemporaryDirectory() as d:
            csv = os.path.join(d, "m.csv")
            _synth_csv(csv, n=300, with_hand=False)
            rep = bt.run_backtest(bt.load_games(csv), warmup=10, test_hand=True)
            self.assertEqual(rep["ablation_handedness"]["n"], 0)   # gracefully no-op


if __name__ == "__main__":
    unittest.main(verbosity=2)
