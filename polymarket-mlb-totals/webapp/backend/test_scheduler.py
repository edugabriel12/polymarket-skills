#!/usr/bin/env python3
"""Offline tests for the sharp-close scheduler's time math + loop (no network).

Run: cd polymarket-mlb-totals/webapp/backend && python test_scheduler.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sharp_close_scheduler as scl  # noqa: E402


class TestParseTimes(unittest.TestCase):
    def test_parses_and_validates(self):
        self.assertEqual(scl.parse_times("23:00"), [(23, 0)])
        self.assertEqual(scl.parse_times("23:00, 01:30"), [(23, 0), (1, 30)])
        self.assertEqual(scl.parse_times(""), [])
        self.assertEqual(scl.parse_times("nonsense,25:00,12:99"), [])  # all invalid -> dropped


class TestSecondsUntilNext(unittest.TestCase):
    def test_later_today(self):
        now = datetime(2026, 6, 23, 20, 0, tzinfo=timezone.utc)
        self.assertEqual(scl.seconds_until_next([(23, 0)], now), 3 * 3600)

    def test_rolls_to_tomorrow_when_past(self):
        now = datetime(2026, 6, 23, 23, 30, tzinfo=timezone.utc)
        # next 23:00 is tomorrow -> 23.5h away
        self.assertAlmostEqual(scl.seconds_until_next([(23, 0)], now), 23.5 * 3600)

    def test_picks_soonest_of_many(self):
        now = datetime(2026, 6, 23, 22, 0, tzinfo=timezone.utc)
        self.assertEqual(scl.seconds_until_next([(1, 0), (23, 0)], now), 3600)  # 23:00 is sooner

    def test_empty(self):
        self.assertEqual(scl.seconds_until_next([]), 24 * 3600)


class TestRunLoop(unittest.TestCase):
    def test_fires_capture_then_survives_and_stops(self):
        calls = {"n": 0}

        async def do_capture():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")     # first capture fails -> loop must survive
            if calls["n"] >= 2:
                raise asyncio.CancelledError    # stop the loop on the second fire

        async def fake_sleep(_s):
            return None                         # don't actually wait

        async def driver():
            with self.assertRaises(asyncio.CancelledError):
                await scl.run_loop([(23, 0)], do_capture, lambda *a: None, sleep=fake_sleep)

        asyncio.run(driver())
        self.assertGreaterEqual(calls["n"], 2)  # survived the first failure, fired again


if __name__ == "__main__":
    unittest.main(verbosity=2)
