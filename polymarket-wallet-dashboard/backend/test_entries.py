#!/usr/bin/env python3
"""Offline tests for the unified entry contract."""

import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import entries as en  # noqa: E402


class TestKey(unittest.TestCase):
    def test_stable_and_case_insensitive(self):
        a = en.make_key("Soccer", "epl-ars-che", "OVER", "Alta")
        b = en.make_key("soccer", "EPL-ARS-CHE", "over", "alta")
        self.assertEqual(a, b)
        self.assertNotEqual(a, en.make_key("Soccer", "epl-ars-che", "UNDER", "Alta"))


class TestLiveFlag(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 6, 25, 18, 0, tzinfo=timezone.utc)

    def test_future_is_prelive(self):
        self.assertEqual(en.live_flag("2026-06-25T20:00:00Z", self.now), en.PRELIVE)

    def test_past_is_live(self):
        self.assertEqual(en.live_flag("2026-06-25T17:00:00Z", self.now), en.LIVE)

    def test_naive_and_missing(self):
        self.assertEqual(en.live_flag("2026-06-25T17:00:00", self.now), en.LIVE)  # naive -> UTC
        self.assertEqual(en.live_flag(None, self.now), en.PRELIVE)
        self.assertEqual(en.live_flag("garbage", self.now), en.PRELIVE)


class TestMakeEntry(unittest.TestCase):
    def test_shape_and_clamps(self):
        e = en.make_entry(
            key="k1", event="Arsenal vs Chelsea", category="Soccer",
            subcategory="Over/Under gols", side="OVER", odds=1.79, entry_price=0.56,
            unit=1.0, confidence="Alta", live=en.PRELIVE, source="model")
        self.assertEqual(e["unit"], 1.0)
        self.assertEqual(e["status"], "OPEN")
        self.assertEqual(e["live"], en.PRELIVE)
        self.assertEqual(e["source"], "model")
        # public_view strips source (no model/wallet origin reaches Sports)
        pub = en.public_view(e)
        self.assertNotIn("source", pub)
        self.assertIn("unit", pub)
        self.assertIn("live", pub)

    def test_bad_status_and_live_default(self):
        e = en.make_entry(key="k", event="x", category="Baseball", subcategory="Moneyline",
                          side="YANKEES", odds=1.5, entry_price=0.66, unit=0.5,
                          confidence="Média", live="weird", status="bogus")
        self.assertEqual(e["live"], en.PRELIVE)
        self.assertEqual(e["status"], "OPEN")

    def test_settled_pnl(self):
        e = en.make_entry(key="k", event="x", category="Soccer", subcategory="Moneyline (1X2)",
                          side="OVER", odds=2.0, entry_price=0.5, unit=1.0, confidence="Alta",
                          live=en.LIVE, status="WON", pnl=123.456)
        self.assertEqual(e["status"], "WON")
        self.assertEqual(e["pnl"], 123.46)


if __name__ == "__main__":
    unittest.main(verbosity=2)
