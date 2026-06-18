#!/usr/bin/env python3
"""Offline tests for cross-source settlement logic and the demo seed.

Run: python polymarket-mlb-totals/scripts/test_settlement.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import predictions_db as pdb  # noqa: E402
import settlement  # noqa: E402
import seed_demo  # noqa: E402


class _NoNetAPI:
    def get(self, url, params=None):
        raise RuntimeError("network disabled")


class TestDecideSettlements(unittest.TestCase):
    def setUp(self):
        self.pending = [
            {"id": 1, "game_slug": "mlb-hou-kc-2026-06-13", "game_date": "2026-06-13", "condition_id": "0xA"},
            {"id": 2, "game_slug": "mlb-nyy-bos-2026-06-13", "game_date": "2026-06-13", "condition_id": "0xB"},
            {"id": 3, "game_slug": "mlb-lad-sf-2026-06-13", "game_date": "2026-06-13", "condition_id": "0xC"},
        ]
        # Finals are keyed by (game_date, away, home); lad-sf not final.
        self.finals = {("2026-06-13", "hou", "kc"): 9.0, ("2026-06-13", "nyy", "bos"): 7.0}

    def test_default_settles_on_mlb_final(self):
        # New default: the MLB final is authoritative; Polymarket-closed is ignored.
        closed = {"0xA": True, "0xB": False}  # B final but market not (yet) closed
        out = settlement.decide_settlements(self.pending, self.finals, closed)
        self.assertEqual(sorted(out), [(1, 9.0), (2, 7.0)])  # both finals settle

    def test_require_closed_opt_in_needs_both(self):
        closed = {"0xA": True, "0xB": False}
        out = settlement.decide_settlements(self.pending, self.finals, closed, require_closed=True)
        self.assertEqual(out, [(1, 9.0)])  # only A: final + closed

    def test_no_market_status_blocks_only_when_required(self):
        self.assertEqual(
            settlement.decide_settlements(self.pending, self.finals, {}, require_closed=True), [])
        self.assertEqual(
            sorted(settlement.decide_settlements(self.pending, self.finals, {})),
            [(1, 9.0), (2, 7.0)])  # default: no close gate -> both finals settle

    def test_consecutive_day_rematch_does_not_cross_settle(self):
        # Same two teams play on the 16th (final) and again on the 17th (not started).
        # The 17th's prediction must NOT inherit the 16th's final.
        pending = [
            {"id": 10, "game_slug": "mlb-cws-nyy-2026-06-16", "game_date": "2026-06-16", "condition_id": "0xD"},
            {"id": 11, "game_slug": "mlb-cws-nyy-2026-06-17", "game_date": "2026-06-17", "condition_id": "0xE"},
        ]
        finals = {("2026-06-16", "cws", "nyy"): 8.0}  # only the 16th is final
        out = settlement.decide_settlements(pending, finals, {})
        self.assertEqual(out, [(10, 8.0)])  # the 17th stays pending


class TestSettlePendingOffline(unittest.TestCase):
    def test_offline_settles_nothing_but_does_not_crash(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "p.db")
            seed_demo.seed(db, reset=True)
            before = pdb.summary(db)["pendente"]
            res = settlement.settle_pending(_NoNetAPI(), db)
            after = pdb.summary(db)["pendente"]
            self.assertEqual(res["settled"], [])      # no network -> nothing settles
            self.assertEqual(before, after)           # still pending
            self.assertGreater(res["checked"], 0)


class TestSeedDemo(unittest.TestCase):
    def test_seed_produces_mixed_statuses(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "p.db")
            n = seed_demo.seed(db, reset=True)
            self.assertGreater(n, 5)
            s = pdb.summary(db)
            self.assertGreater(s["acerto"], 0)
            self.assertGreater(s["erro"], 0)
            self.assertGreater(s["pendente"], 0)
            self.assertGreater(s["anulado"], 0)
            # Every row carries a Polymarket market link.
            self.assertTrue(all(r["market_url"] for r in pdb.get_predictions(db)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
