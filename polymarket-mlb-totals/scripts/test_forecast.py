#!/usr/bin/env python3
"""Offline tests for Layers 1 & 3 (distribution + per-prediction confidence).

Run: python polymarket-mlb-totals/scripts/test_forecast.py
"""

from __future__ import annotations

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run_distribution as rd  # noqa: E402
import forecast as fc  # noqa: E402


def _pmf(mu: float, dispersion: float = 2.0) -> list[float]:
    return rd.negbin_total_runs_pmf(mu, rd.variance_from_mu(mu, dispersion))


class TestPmfReads(unittest.TestCase):
    def test_cdf_is_monotonic_and_ends_at_one(self):
        c = fc.cdf(_pmf(8.5))
        self.assertTrue(all(c[i] <= c[i + 1] + 1e-12 for i in range(len(c) - 1)))
        self.assertAlmostEqual(c[-1], 1.0, places=9)

    def test_mean_matches_target_mu(self):
        # E[T] of the NegBin pmf should equal the requested mu (within tail truncation).
        self.assertAlmostEqual(fc.mean_of_pmf(_pmf(8.5)), 8.5, places=2)

    def test_quantile_monotone_in_q(self):
        pmf = _pmf(9.0)
        qs = [fc.quantile(pmf, q) for q in (0.1, 0.25, 0.5, 0.75, 0.9)]
        self.assertTrue(all(qs[i] <= qs[i + 1] for i in range(len(qs) - 1)))

    def test_mode_near_mean_for_unimodal(self):
        self.assertTrue(abs(fc.mode_of_pmf(_pmf(8.5)) - 8.5) <= 2)


class TestIntervals(unittest.TestCase):
    def test_interval_covers_at_least_nominal_mass(self):
        # Discrete intervals are conservative: realized mass ≥ nominal, never below.
        pmf = _pmf(8.5)
        for mass in (0.50, 0.80):
            lo, hi = fc.prediction_interval(pmf, mass)
            self.assertGreaterEqual(fc.interval_mass(pmf, lo, hi), mass - 1e-9)
            self.assertLessEqual(lo, hi)

    def test_80_interval_wider_than_50(self):
        pmf = _pmf(8.5)
        lo50, hi50 = fc.prediction_interval(pmf, 0.50)
        lo80, hi80 = fc.prediction_interval(pmf, 0.80)
        self.assertLessEqual(lo80, lo50)
        self.assertGreaterEqual(hi80, hi50)

    def test_single_game_interval_is_wide(self):
        # A single MLB total is irreducibly uncertain — the 80% band must span many runs.
        lo, hi = fc.prediction_interval(_pmf(8.5), 0.80)
        self.assertGreaterEqual(hi - lo, 5)


class TestEntropy(unittest.TestCase):
    def test_entropy_nonnegative_and_higher_for_wider(self):
        narrow = fc.predictive_entropy(_pmf(8.5, dispersion=1.3))
        wide = fc.predictive_entropy(_pmf(8.5, dispersion=3.0))
        self.assertGreaterEqual(narrow, 0.0)
        self.assertGreater(wide, narrow)

    def test_point_mass_has_zero_entropy(self):
        pmf = [0.0] * 41
        pmf[9] = 1.0
        self.assertAlmostEqual(fc.predictive_entropy(pmf), 0.0, places=12)


class TestForecastSummary(unittest.TestCase):
    def test_summary_has_layers_1_and_3(self):
        s = fc.forecast_summary(8.5, rd.variance_from_mu(8.5), line=8.5)
        for key in ("mean", "median", "mode", "pi50", "pi80", "entropy_bits",
                    "p_over", "p_under", "pmf"):
            self.assertIn(key, s)
        self.assertAlmostEqual(s["p_over"] + s["p_under"], 1.0, places=6)  # 8.5 -> no push
        self.assertAlmostEqual(math.fsum(s["pmf"]), 1.0, places=9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
