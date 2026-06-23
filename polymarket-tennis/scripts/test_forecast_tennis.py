#!/usr/bin/env python3
"""Offline tests for tennis Layers 1 & 3 (binary forecast + confidence). No network.

Run: python polymarket-tennis/scripts/test_forecast_tennis.py
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import forecast_tennis as fct  # noqa: E402


class TestForecastBlock(unittest.TestCase):
    def test_block_fields_and_complement(self):
        b = fct.forecast_block(0.70)
        for key in ("p_win", "p_lose", "entropy_bits", "confidence", "uncertainty_flag"):
            self.assertIn(key, b)
        self.assertAlmostEqual(b["p_win"] + b["p_lose"], 1.0, places=6)

    def test_tossup_has_max_entropy(self):
        b = fct.forecast_block(0.50)
        self.assertAlmostEqual(b["entropy_bits"], 1.0, places=6)
        self.assertEqual(b["confidence"], "toss-up")

    def test_sharper_forecast_lower_entropy_and_strong(self):
        b = fct.forecast_block(0.90)
        self.assertLess(b["entropy_bits"], 0.6)
        self.assertEqual(b["confidence"], "strong")

    def test_confidence_bands(self):
        self.assertEqual(fct.forecast_block(0.55)["confidence"], "toss-up")  # ent≈0.993
        self.assertEqual(fct.forecast_block(0.75)["confidence"], "lean")     # ent≈0.811
        self.assertEqual(fct.forecast_block(0.85)["confidence"], "strong")   # ent≈0.610

    def test_uncertainty_flag_widens_confidence(self):
        # Heat/wind flag bumps a strong call down to lean, WITHOUT moving the probability.
        base = fct.forecast_block(0.85)
        flagged = fct.forecast_block(0.85, uncertainty_flag=True)
        self.assertEqual(base["confidence"], "strong")
        self.assertEqual(flagged["confidence"], "lean")
        self.assertEqual(base["p_win"], flagged["p_win"])     # probability unchanged

    def test_entropy_symmetric(self):
        self.assertAlmostEqual(fct.forecast_block(0.3)["entropy_bits"],
                               fct.forecast_block(0.7)["entropy_bits"], places=9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
