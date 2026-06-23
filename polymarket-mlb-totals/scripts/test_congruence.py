#!/usr/bin/env python3
"""Offline tests for the shared model↔sharp congruence core. No network.

Run: python polymarket-mlb-totals/scripts/test_congruence.py
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import congruence as cg  # noqa: E402


class TestFactor(unittest.TestCase):
    def test_full_agreement_full_size(self):
        self.assertEqual(cg.congruence_factor(0.0), 1.0)
        self.assertEqual(cg.congruence_factor(cg.FULL_AGREE), 1.0)

    def test_beyond_band_zero_size(self):
        self.assertEqual(cg.congruence_factor(cg.ZERO_AT), 0.0)
        self.assertEqual(cg.congruence_factor(0.30), 0.0)

    def test_monotone_decreasing(self):
        gaps = [0.0, 0.05, 0.08, 0.12, 0.15, 0.20]
        fs = [cg.congruence_factor(g) for g in gaps]
        self.assertTrue(all(fs[i] >= fs[i + 1] for i in range(len(fs) - 1)))

    def test_never_above_one_or_below_zero(self):
        for g in (0.0, 0.04, 0.1, 0.15, 0.5):
            self.assertGreaterEqual(cg.congruence_factor(g), 0.0)
            self.assertLessEqual(cg.congruence_factor(g), 1.0)


class TestAssess(unittest.TestCase):
    def test_neutral_when_no_model(self):
        a = cg.assess(None, 0.6)
        self.assertEqual(a["factor"], 1.0)
        self.assertFalse(a["applied"])
        self.assertFalse(a["incongruent"])

    def test_neutral_when_no_sharp(self):
        self.assertEqual(cg.assess(0.6, None)["factor"], 1.0)

    def test_high_agreement(self):
        a = cg.assess(0.61, 0.60)
        self.assertEqual(a["factor"], 1.0)
        self.assertEqual(a["agreement"], "high")
        self.assertFalse(a["incongruent"])
        self.assertTrue(a["applied"])

    def test_low_agreement_flagged_and_shrunk(self):
        a = cg.assess(0.75, 0.55)               # 20pt gap -> beyond the band
        self.assertEqual(a["factor"], 0.0)
        self.assertTrue(a["incongruent"])
        self.assertEqual(a["agreement"], "low")

    def test_moderate_partial_size(self):
        a = cg.assess(0.62, 0.55)               # 7pt gap -> partial
        self.assertTrue(0.0 < a["factor"] < 1.0)
        self.assertFalse(a["incongruent"])      # below 10pt threshold
        self.assertEqual(a["agreement"], "moderate")

    def test_gap_is_symmetric(self):
        self.assertEqual(cg.assess(0.7, 0.6)["gap"], cg.assess(0.6, 0.7)["gap"])


class TestConfidence(unittest.TestCase):
    def test_full_factor_keeps_confidence(self):
        self.assertAlmostEqual(cg.apply_confidence(0.65, 1.0), 0.65, places=9)

    def test_zero_factor_pulls_to_half(self):
        self.assertAlmostEqual(cg.apply_confidence(0.65, 0.0), 0.5, places=9)

    def test_partial_factor_interpolates(self):
        self.assertAlmostEqual(cg.apply_confidence(0.70, 0.5), 0.6, places=9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
