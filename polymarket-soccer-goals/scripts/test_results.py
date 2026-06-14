#!/usr/bin/env python3
"""Offline tests for soccer auto-settlement (results feed parsing + matching).

Run: python polymarket-soccer-goals/scripts/test_results.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import soccer_results as sr  # noqa: E402
import soccer_predictions as spdb  # noqa: E402


_PAYLOAD = {
    "matches": [
        {"status": "FINISHED", "utcDate": "2026-06-14T18:00:00Z",
         "homeTeam": {"tla": "JPN"}, "awayTeam": {"tla": "NED"},  # note: order reversed vs slug
         "score": {"fullTime": {"home": 1, "away": 2}}},
        {"status": "FINISHED", "utcDate": "2026-06-14T15:00:00Z",
         "homeTeam": {"tla": "ARS"}, "awayTeam": {"tla": "CHE"},
         "score": {"fullTime": {"home": 0, "away": 0}}},
        {"status": "TIMED", "utcDate": "2026-06-14T20:00:00Z",  # not finished -> ignored
         "homeTeam": {"tla": "GER"}, "awayTeam": {"tla": "BRA"},
         "score": {"fullTime": {"home": None, "away": None}}},
    ]
}


class TestParse(unittest.TestCase):
    def test_parse_finished_normalizes_and_computes(self):
        lk = sr.parse_finished(_PAYLOAD)
        # NED normalizes to nld; pair is order-independent and sorted.
        self.assertIn(("2026-06-14", "jpn", "nld"), lk)
        total, btts = lk[("2026-06-14", "jpn", "nld")]
        self.assertEqual(total, 3.0)         # 1 + 2
        self.assertTrue(btts)                 # both scored
        # 0-0 -> total 0, no BTTS
        self.assertEqual(lk[("2026-06-14", "ars", "che")], (0.0, False))
        # the unfinished GER-BRA game is excluded
        self.assertNotIn(("2026-06-14", "bra", "ger"), lk)


class TestDecide(unittest.TestCase):
    def test_order_independent_match(self):
        lk = sr.parse_finished(_PAYLOAD)
        # Our slug lists nld first (home_first); the feed had JPN home — still matches.
        pending = [{"game_slug": "fifwc-nld-jpn-2026-06-14", "game_date": "2026-06-14",
                    "home": "nld", "away": "jpn"}]
        out = sr.decide_settlements(pending, lk)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["actual_total"], 3.0)
        self.assertTrue(out[0]["actual_btts"])

    def test_no_match(self):
        lk = sr.parse_finished(_PAYLOAD)
        pending = [{"game_slug": "epl-liv-mci-2026-06-14", "game_date": "2026-06-14",
                    "home": "liv", "away": "mci"}]
        self.assertEqual(sr.decide_settlements(pending, lk), [])


class TestSettlePending(unittest.TestCase):
    def _seed(self, db):
        for market, side, line in [("TOTAL", "OVER", 2.5), ("BTTS", "YES", None)]:
            spdb.record_prediction({
                "game_slug": "fifwc-nld-jpn-2026-06-14", "game_date": "2026-06-14",
                "league": "fifwc", "market": market, "market_question": "q",
                "condition_id": "0x", "token_id": f"t-{side}", "line": line, "side": side,
                "entry_price": 0.5, "decimal_odds": 2.0, "model_prob": 0.6, "edge": 0.1,
                "lam_home": 1.5, "lam_away": 1.1, "rho": -0.1, "confidence": 0.6, "size_pct": 0.01,
                "size_usd": 100.0, "kelly_fraction": 0.2, "used_external": True, "fee_rate": 0.0,
                "strategy": "soccer-goals-dc", "market_url": "u", "stats": {}}, db)

    def test_no_token_no_settle(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "p.db")
            self._seed(db)
            res = sr.settle_pending(db, token=None)
            self.assertEqual(res["settled"], [])
            self.assertEqual(spdb.summary(db)["pendente"], 2)

    def test_settle_with_injected_feed(self):
        # Monkeypatch the network fetch to return our offline lookup.
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "p.db")
            self._seed(db)
            orig = sr.fetch_finished
            sr.fetch_finished = lambda *a, **k: sr.parse_finished(_PAYLOAD)
            try:
                res = sr.settle_pending(db, token="fake")
            finally:
                sr.fetch_finished = orig
            statuses = sorted(r["status"] for r in res["settled"])
            self.assertEqual(statuses, ["ACERTO", "ACERTO"])  # total 3>2.5 + both scored
            s = spdb.summary(db)
            self.assertEqual(s["acerto"], 2)
            self.assertEqual(s["pendente"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
