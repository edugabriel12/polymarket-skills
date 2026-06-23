#!/usr/bin/env python3
"""Offline tests for the per-game recalc wave scheduler (no network).

Run: cd polymarket-mlb-totals/webapp/backend && python test_wave_scheduler.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wave_scheduler as wsch  # noqa: E402

UTC = timezone.utc


class TestWavesFromCommences(unittest.TestCase):
    def test_trigger_is_lead_before_start(self):
        now = datetime(2026, 6, 23, 16, 0, tzinfo=UTC)
        games = [datetime(2026, 6, 23, 17, 0, tzinfo=UTC)]   # one game at 17:00
        waves = wsch.waves_from_commences(games, now, lead_min=10, bucket_min=10)
        self.assertEqual(waves, [datetime(2026, 6, 23, 16, 50, tzinfo=UTC)])

    def test_simultaneous_starts_collapse_to_one_wave(self):
        now = datetime(2026, 6, 23, 16, 0, tzinfo=UTC)
        # three games within a 7-min window -> one wave (the earliest trigger)
        games = [
            datetime(2026, 6, 23, 23, 5, tzinfo=UTC),
            datetime(2026, 6, 23, 23, 8, tzinfo=UTC),
            datetime(2026, 6, 23, 23, 12, tzinfo=UTC),
        ]
        waves = wsch.waves_from_commences(games, now, lead_min=10, bucket_min=10)
        self.assertEqual(waves, [datetime(2026, 6, 23, 22, 55, tzinfo=UTC)])

    def test_separate_blocks_are_separate_waves(self):
        now = datetime(2026, 6, 23, 16, 0, tzinfo=UTC)
        games = [
            datetime(2026, 6, 23, 17, 10, tzinfo=UTC),   # afternoon
            datetime(2026, 6, 23, 23, 5, tzinfo=UTC),    # night
        ]
        waves = wsch.waves_from_commences(games, now, lead_min=10, bucket_min=10)
        self.assertEqual(len(waves), 2)
        self.assertEqual(waves[0], datetime(2026, 6, 23, 17, 0, tzinfo=UTC))
        self.assertEqual(waves[1], datetime(2026, 6, 23, 22, 55, tzinfo=UTC))

    def test_past_triggers_dropped(self):
        now = datetime(2026, 6, 23, 18, 0, tzinfo=UTC)
        games = [
            datetime(2026, 6, 23, 17, 0, tzinfo=UTC),    # already started -> trigger in the past
            datetime(2026, 6, 23, 20, 0, tzinfo=UTC),    # future
        ]
        waves = wsch.waves_from_commences(games, now, lead_min=10, bucket_min=10)
        self.assertEqual(waves, [datetime(2026, 6, 23, 19, 50, tzinfo=UTC)])


class TestNextWave(unittest.TestCase):
    def test_picks_soonest_unfired_future(self):
        now = datetime(2026, 6, 23, 16, 0, tzinfo=UTC)
        w1 = datetime(2026, 6, 23, 16, 50, tzinfo=UTC)
        w2 = datetime(2026, 6, 23, 22, 55, tzinfo=UTC)
        self.assertEqual(wsch.next_wave([w1, w2], set(), now), w1)
        self.assertEqual(wsch.next_wave([w1, w2], {w1}, now), w2)   # w1 fired -> w2
        self.assertIsNone(wsch.next_wave([w1, w2], {w1, w2}, now))  # all fired


class TestRunWaveLoop(unittest.TestCase):
    def test_fires_one_wave_when_due_then_stops(self):
        base = datetime(2026, 6, 23, 16, 0, tzinfo=UTC)
        clock = {"t": base}
        commences = [base + timedelta(minutes=12)]   # trigger at +2 min
        fired = {"n": 0}

        async def get_commences():
            return commences

        async def do_wave():
            fired["n"] += 1

        async def fake_sleep(_s):
            clock["t"] += timedelta(seconds=300)     # advance 5 min per poll
            if clock["t"] > base + timedelta(minutes=30):
                raise asyncio.CancelledError          # end the test

        async def driver():
            with self.assertRaises(asyncio.CancelledError):
                await wsch.run_wave_loop(
                    lambda: "2026-06-23", get_commences, do_wave, lambda *a: None,
                    lead_min=10, bucket_min=10, poll_sec=300,
                    now_fn=lambda: clock["t"], sleep=fake_sleep)

        asyncio.run(driver())
        self.assertEqual(fired["n"], 1)   # the single due wave fired exactly once (no burst)


if __name__ == "__main__":
    unittest.main(verbosity=2)
