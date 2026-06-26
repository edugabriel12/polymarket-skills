#!/usr/bin/env python3
"""Offline tests for Layer 4 proper scoring rules (no network).

Run: python polymarket-forecasting/scripts/test_scoring.py
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run_distribution as rd  # noqa: E402
import scoring  # noqa: E402


def _point_mass(m: int, kmax: int = 40) -> list[float]:
    pmf = [0.0] * (kmax + 1)
    pmf[m] = 1.0
    return pmf


class TestCRPS(unittest.TestCase):
    def test_point_mass_reduces_to_absolute_error(self):
        # CRPS of a deterministic forecast == |forecast − actual| (in run units).
        for m, y in [(8, 8), (8, 11), (12, 5), (0, 7)]:
            self.assertAlmostEqual(scoring.crps_pmf(_point_mass(m), y), abs(m - y),
                                   places=9, msg=f"m={m} y={y}")

    def test_exact_forecast_is_zero(self):
        self.assertAlmostEqual(scoring.crps_pmf(_point_mass(9), 9), 0.0, places=12)

    def test_sharper_distribution_scores_better_when_centered(self):
        # Two distributions centered on the actual; the tighter one should win.
        actual = 9
        tight = rd.negbin_total_runs_pmf(9.0, rd.variance_from_mu(9.0, 1.3))
        wide = rd.negbin_total_runs_pmf(9.0, rd.variance_from_mu(9.0, 3.0))
        self.assertLess(scoring.crps_pmf(tight, actual), scoring.crps_pmf(wide, actual))

    def test_confident_wrong_beaten_by_humble(self):
        # A confident pmf far from the truth should score WORSE than a humble wide one.
        actual = 15
        confident_wrong = rd.negbin_total_runs_pmf(7.0, rd.variance_from_mu(7.0, 1.2))
        humble = rd.negbin_total_runs_pmf(8.5, rd.variance_from_mu(8.5, 3.0))
        self.assertGreater(scoring.crps_pmf(confident_wrong, actual),
                           scoring.crps_pmf(humble, actual))

    def test_crps_point_matches_abs(self):
        self.assertAlmostEqual(scoring.crps_point(8.3, 9.0), 0.7, places=9)


class TestBinaryScores(unittest.TestCase):
    def test_brier_known_values(self):
        self.assertAlmostEqual(scoring.brier([(0.5, 1), (0.5, 0)]), 0.25, places=9)
        self.assertAlmostEqual(scoring.brier([(1.0, 1), (0.0, 0)]), 0.0, places=9)
        self.assertIsNone(scoring.brier([]))

    def test_log_loss_finite_on_confident_miss(self):
        ll = scoring.log_loss([(0.999999999, 0)])   # clipped, stays finite
        self.assertTrue(ll < 20 and ll > 0)

    def test_log_loss_perfect_is_near_zero(self):
        self.assertAlmostEqual(scoring.log_loss([(1.0, 1), (0.0, 0)]), 0.0, places=4)


class TestCoverage(unittest.TestCase):
    def test_coverage_counts_inside_closed_interval(self):
        recs = [(7, 11, 9), (7, 11, 7), (7, 11, 11), (7, 11, 6), (7, 11, 12)]
        self.assertAlmostEqual(scoring.coverage(recs), 3 / 5, places=9)
        self.assertIsNone(scoring.coverage([]))

    def test_mean_crps(self):
        pmfs = [(_point_mass(8), 8), (_point_mass(8), 10)]
        self.assertAlmostEqual(scoring.mean_crps(pmfs), 1.0, places=9)  # (0 + 2)/2


if __name__ == "__main__":
    unittest.main(verbosity=2)
