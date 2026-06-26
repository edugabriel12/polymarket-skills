#!/usr/bin/env python3
"""Offline tests for the confidence→value-band derivation + live classification."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import confidence_model as cm  # noqa: E402


def _recs(pairs):
    return [{"confidence": c, "invested": v} for c, v in pairs]


class TestUnitMapping(unittest.TestCase):
    def test_units(self):
        self.assertEqual(cm.unit_for("Alta"), 1.0)
        self.assertEqual(cm.unit_for("Média"), 0.5)
        self.assertEqual(cm.unit_for("Baixa"), 0.25)
        self.assertIsNone(cm.unit_for("?"))


class TestDerive(unittest.TestCase):
    def test_ordered_floors_and_units(self):
        recs = _recs([("Baixa", 500), ("Baixa", 1000), ("Baixa", 10000),
                      ("Média", 15000), ("Média", 20000), ("Média", 20000),
                      ("Alta", 40000), ("Alta", 100000), ("Alta", 200000)])
        th = cm.derive_thresholds(recs)
        self.assertLess(th["Baixa"]["floor"], th["Média"]["floor"])
        self.assertLess(th["Média"]["floor"], th["Alta"]["floor"])
        self.assertEqual(th["Alta"]["unit"], 1.0)
        self.assertEqual(th["Média"]["unit"], 0.5)
        self.assertEqual(th["Baixa"]["unit"], 0.25)
        # floor ≈ tier minimum (low percentile)
        self.assertLessEqual(th["Média"]["floor"], 15000)

    def test_monotonic_clamp_on_noisy_wallet(self):
        # A wallet whose "Média" bets are bigger than some "Alta" bets -> clamp keeps order.
        recs = _recs([("Baixa", 100), ("Média", 5000), ("Média", 6000),
                      ("Alta", 4000), ("Alta", 9000)])
        th = cm.derive_thresholds(recs)
        self.assertLessEqual(th["Baixa"]["floor"], th["Média"]["floor"])
        self.assertLessEqual(th["Média"]["floor"], th["Alta"]["floor"])

    def test_missing_tier_skipped(self):
        th = cm.derive_thresholds(_recs([("Baixa", 100), ("Alta", 5000)]))
        self.assertIn("Baixa", th)
        self.assertIn("Alta", th)
        self.assertNotIn("Média", th)


class TestClassify(unittest.TestCase):
    def setUp(self):
        self.th = cm.derive_thresholds(_recs([
            ("Baixa", 500), ("Baixa", 1000), ("Baixa", 10000),
            ("Média", 15000), ("Média", 20000),
            ("Alta", 40000), ("Alta", 200000)]))

    def test_highest_tier_met(self):
        self.assertIsNone(cm.classify_position(300, self.th))          # below Baixa floor
        self.assertEqual(cm.classify_position(1000, self.th)["confidence"], "Baixa")
        self.assertEqual(cm.classify_position(16000, self.th)["confidence"], "Média")
        self.assertEqual(cm.classify_position(45000, self.th)["confidence"], "Alta")

    def test_unit_attached(self):
        self.assertEqual(cm.classify_position(45000, self.th)["unit"], 1.0)
        self.assertEqual(cm.classify_position(16000, self.th)["unit"], 0.5)

    def test_none_position(self):
        self.assertIsNone(cm.classify_position(None, self.th))


if __name__ == "__main__":
    unittest.main(verbosity=2)
