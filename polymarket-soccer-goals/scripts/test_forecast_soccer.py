#!/usr/bin/env python3
"""Offline tests for soccer Layers 1 & 3 (goal distribution + confidence). No network.

Run: python polymarket-soccer-goals/scripts/test_forecast_soccer.py
"""

from __future__ import annotations

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dixon_coles as dc  # noqa: E402
import forecast_soccer as fcs  # noqa: E402


class TestTotalGoalsPmf(unittest.TestCase):
    def test_pmf_sums_to_one_and_matches_over(self):
        matrix = dc.score_matrix(1.5, 1.2, -0.10)
        pmf = fcs.total_goals_pmf(matrix)
        self.assertAlmostEqual(math.fsum(pmf), 1.0, places=9)
        # P(Over 2.5) from the pmf (totals ≥ 3) must equal dc.prob_over.
        p_over_pmf = math.fsum(pmf[3:])
        self.assertAlmostEqual(p_over_pmf, dc.prob_over(2.5, matrix)["p_over"], places=9)

    def test_mean_matches_lambda_sum(self):
        # E[Total] ≈ lam_home + lam_away (the DC correction barely moves the mean).
        matrix = dc.score_matrix(1.6, 1.1, -0.10)
        pmf = fcs.total_goals_pmf(matrix)
        mean = math.fsum(k * p for k, p in enumerate(pmf))
        self.assertAlmostEqual(mean, 2.7, delta=0.1)


class TestForecastBlock(unittest.TestCase):
    def test_block_has_layers_1_and_3(self):
        b = fcs.forecast_block(1.5, 1.2, -0.10, line=2.5, market_type="TOTAL")
        for key in ("mean_goals", "median_goals", "most_likely_goals", "pi50", "pi80",
                    "entropy_bits", "p_btts", "p_over", "p_under"):
            self.assertIn(key, b)
        self.assertAlmostEqual(b["p_over"] + b["p_under"], 1.0, places=6)  # 2.5 → no push
        self.assertLessEqual(b["pi80"][0], b["pi50"][0])
        self.assertGreaterEqual(b["pi80"][1], b["pi50"][1])

    def test_interval_is_wide_for_single_match(self):
        # A single match total is irreducibly uncertain — 80% band spans several goals.
        b = fcs.forecast_block(1.6, 1.3, -0.10)
        self.assertGreaterEqual(b["pi80"][1] - b["pi80"][0], 3)

    def test_btts_matches_dc(self):
        b = fcs.forecast_block(1.4, 1.1, -0.10)
        expected = dc.prob_btts(dc.score_matrix(1.4, 1.1, -0.10))["p_yes"]
        self.assertAlmostEqual(b["p_btts"], round(expected, 4), places=4)

    def test_higher_lambdas_more_goals_and_btts(self):
        low = fcs.forecast_block(0.8, 0.7, -0.10)
        high = fcs.forecast_block(2.2, 1.9, -0.10)
        self.assertGreater(high["mean_goals"], low["mean_goals"])
        self.assertGreater(high["p_btts"], low["p_btts"])

    def test_btts_market_omits_over_under(self):
        b = fcs.forecast_block(1.5, 1.2, -0.10, line=2.5, market_type="BTTS")
        self.assertNotIn("p_over", b)
        self.assertIn("p_btts", b)


if __name__ == "__main__":
    unittest.main(verbosity=2)
