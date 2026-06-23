#!/usr/bin/env python3
"""Offline tests for Layer 2 calibration math + post-hoc calibrators (no network).

Run: python polymarket-mlb-totals/scripts/test_calibration_core.py
"""

from __future__ import annotations

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import calibration_core as cc  # noqa: E402


def _perfectly_calibrated_pairs() -> list[tuple[float, int]]:
    """For each bucket center p, make exactly p·N outcomes 1 — so empirical == predicted."""
    pairs = []
    for p, n in [(0.1, 100), (0.3, 100), (0.5, 100), (0.7, 100), (0.9, 100)]:
        ones = round(p * n)
        pairs += [(p, 1)] * ones + [(p, 0)] * (n - ones)
    return pairs


class TestMetrics(unittest.TestCase):
    def test_perfect_calibration_has_zero_ece(self):
        pairs = _perfectly_calibrated_pairs()
        self.assertLess(cc.ece(pairs, nbins=10), 1e-9)
        self.assertLess(cc.mce(pairs, nbins=10), 1e-9)

    def test_overconfident_has_positive_ece(self):
        # Model always says 0.9 but truth is a coin flip -> big calibration error.
        pairs = [(0.9, i % 2) for i in range(100)]
        self.assertGreater(cc.ece(pairs), 0.3)

    def test_brier_decomposition_sums_to_brier_when_bins_constant(self):
        # With constant forecasts inside each bucket, the Murphy identity is exact.
        pairs = _perfectly_calibrated_pairs()
        d = cc.brier_decomposition(pairs, nbins=10)
        self.assertAlmostEqual(d["recombined"], d["brier"], places=9)
        # Perfect calibration -> reliability ≈ 0.
        self.assertLess(d["reliability"], 1e-9)

    def test_reliability_diagram_tracks_diagonal_when_calibrated(self):
        for row in cc.reliability_diagram(_perfectly_calibrated_pairs()):
            self.assertLess(abs(row["gap"]), 1e-9)

    def test_empty_inputs_return_none(self):
        self.assertIsNone(cc.ece([]))
        self.assertIsNone(cc.mce([]))
        self.assertIsNone(cc.brier_decomposition([]))


class TestCalibrators(unittest.TestCase):
    def _overconfident_set(self, n=4000, seed=7):
        """Raw probs pushed AWAY from 0.5 vs the true rate -> overconfident model."""
        rng = random.Random(seed)
        pairs = []
        for _ in range(n):
            true_p = rng.uniform(0.2, 0.8)
            # Overconfident report: exaggerate distance from 0.5.
            raw = 0.5 + (true_p - 0.5) * 1.8
            raw = min(0.999, max(0.001, raw))
            outcome = 1 if rng.random() < true_p else 0
            pairs.append((raw, outcome))
        return pairs

    def test_temperature_improves_calibration(self):
        pairs = self._overconfident_set()
        before = cc.ece(pairs)
        cal = cc.fit_calibrator("temperature", pairs)
        after = cc.ece([(cal.predict(p), o) for p, o in pairs])
        self.assertLess(after, before)
        self.assertGreater(cal.temperature, 1.0)   # softening an overconfident model

    def test_temperature_preserves_argmax(self):
        cal = cc.TemperatureCalibrator(temperature=2.5)
        # Order is preserved: a higher raw prob stays higher after scaling.
        self.assertGreater(cal.predict(0.8), cal.predict(0.6))
        # A 0.5 forecast stays 0.5 (logit 0).
        self.assertAlmostEqual(cal.predict(0.5), 0.5, places=9)

    def test_platt_improves_calibration(self):
        pairs = self._overconfident_set()
        before = cc.ece(pairs)
        cal = cc.fit_calibrator("platt", pairs)
        after = cc.ece([(cal.predict(p), o) for p, o in pairs])
        self.assertLess(after, before)

    def test_isotonic_is_monotonic_and_improves(self):
        pairs = self._overconfident_set()
        cal = cc.fit_calibrator("isotonic", pairs)
        # Monotonic map: non-decreasing in the raw prob.
        grid = [i / 20 for i in range(1, 20)]
        preds = cal.predict_all(grid)
        self.assertTrue(all(preds[i] <= preds[i + 1] + 1e-9 for i in range(len(preds) - 1)))
        before = cc.ece(pairs)
        after = cc.ece([(cal.predict(p), o) for p, o in pairs])
        self.assertLessEqual(after, before + 1e-9)

    def test_identity_temperature_is_noop(self):
        cal = cc.TemperatureCalibrator(temperature=1.0)
        for p in (0.2, 0.5, 0.8):
            self.assertAlmostEqual(cal.predict(p), p, places=6)

    def test_unknown_calibrator_raises(self):
        with self.assertRaises(ValueError):
            cc.fit_calibrator("nope", [(0.5, 1)])


class TestPAV(unittest.TestCase):
    def test_pav_on_violating_sequence(self):
        # Classic PAV example: [1,2,3] target but with a dip -> pooled.
        ys = [3.0, 1.0, 2.0]
        out = cc._pav(ys, [1.0, 1.0, 1.0])
        self.assertTrue(all(out[i] <= out[i + 1] + 1e-12 for i in range(len(out) - 1)))
        self.assertAlmostEqual(sum(out), sum(ys), places=9)   # PAV preserves the sum


if __name__ == "__main__":
    unittest.main(verbosity=2)
