#!/usr/bin/env python3
"""Offline tests for analytics (P&L/ROI/win rate) and the market_url migration.

Run: python polymarket-mlb-totals/scripts/test_analytics.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import predictions_db as pdb  # noqa: E402
import analytics  # noqa: E402


def _pred(side, status, line=8.5, price=0.50, size=100.0, game_date=None,
          game="mlb-aaa-bbb", actual=None):
    return {
        "game_slug": f"{game}-{game_date}", "game_date": game_date,
        "market_question": "total runs?", "condition_id": "0x1",
        "token_id": f"t-{side}-{status}-{game_date}", "line": line, "side": side,
        "entry_price": price, "decimal_odds": 1 / price, "model_prob": 0.6,
        "edge": 0.1, "mu": 9.0, "variance": 18.0, "dispersion": 2.0,
        "park_factor": 100.0, "confidence": 0.6, "size_pct": 0.01, "size_usd": size,
        "kelly_fraction": 0.2, "used_external": True, "fee_rate": 0.0,
        "strategy": "mlb-totals-negbin",
        "market_url": "https://polymarket.com/event/abc",
        "stats": {"model": "negative_binomial"},
    }


def _seed(db, rows):
    for r in rows:
        rid = pdb.record_prediction(r, db)
        # Settle directly to the desired status when not pending.
        st = r.get("_status")
        if st in ("ACERTO", "ERRO", "ANULADO"):
            actual = {"ACERTO": (r["line"] + 1 if r["side"] == "OVER" else r["line"] - 1),
                      "ERRO": (r["line"] - 1 if r["side"] == "OVER" else r["line"] + 1),
                      "ANULADO": r["line"]}[st]
            # Use an integer line for ANULADO so total == line is a real push.
            pdb.settle_prediction(rid, actual, db)


class TestComputePnl(unittest.TestCase):
    def test_acerto_profit(self):
        # price 0.50 -> decimal odds 2.0 -> profit = stake on a win.
        self.assertAlmostEqual(
            analytics.compute_pnl({"status": "ACERTO", "size_usd": 100, "entry_price": 0.5}),
            100.0)

    def test_erro_loss(self):
        self.assertAlmostEqual(
            analytics.compute_pnl({"status": "ERRO", "size_usd": 100, "entry_price": 0.5}),
            -100.0)

    def test_pending_and_void_zero(self):
        self.assertEqual(analytics.compute_pnl({"status": "PENDENTE", "size_usd": 100, "entry_price": 0.5}), 0.0)
        self.assertEqual(analytics.compute_pnl({"status": "ANULADO", "size_usd": 100, "entry_price": 0.5}), 0.0)


class TestPerformance(unittest.TestCase):
    def test_win_rates_and_roi(self):
        today = date(2026, 6, 14)
        td = today.isoformat()
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "p.db")
            rows = [
                dict(_pred("OVER", None, price=0.5, game_date=td), _status="ACERTO"),
                dict(_pred("OVER", None, price=0.5, game="mlb-ccc-ddd", game_date=td), _status="ERRO"),
                dict(_pred("UNDER", None, price=0.5, game="mlb-eee-fff", game_date=td), _status="ACERTO"),
                dict(_pred("UNDER", None, price=0.5, line=9.0, game="mlb-ggg-hhh", game_date=td), _status="ANULADO"),
                dict(_pred("OVER", None, price=0.5, game="mlb-iii-jjj", game_date=td)),  # PENDENTE
            ]
            _seed(db, rows)
            perf = analytics.performance(db, today=today)
            day = perf["daily"]
            # 2 ACERTO, 1 ERRO settled -> win rate 2/3; 1 ANULADO; 1 PENDENTE.
            self.assertEqual(day["counts"], {"acerto": 2, "erro": 1, "pendente": 1, "anulado": 1})
            self.assertAlmostEqual(day["win_rate"], 2 / 3, places=4)
            self.assertAlmostEqual(day["win_rate_over"], 0.5, places=4)   # 1/2 OVER settled
            self.assertAlmostEqual(day["win_rate_under"], 1.0, places=4)  # 1/1 UNDER settled
            # P&L: +100 (over win) -100 (over loss) +100 (under win) = +100; invested 300.
            self.assertAlmostEqual(day["pnl"], 100.0, places=2)
            self.assertAlmostEqual(day["invested"], 300.0, places=2)
            self.assertAlmostEqual(day["roi"], 100.0 / 300.0, places=4)

    def test_window_filtering(self):
        today = date(2026, 6, 14)
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "p.db")
            rows = [
                dict(_pred("OVER", None, game="mlb-a-b", game_date="2026-06-14"), _status="ACERTO"),
                dict(_pred("OVER", None, game="mlb-c-d", game_date="2026-06-01"), _status="ACERTO"),
                dict(_pred("OVER", None, game="mlb-e-f", game_date="2026-05-15"), _status="ERRO"),
            ]
            _seed(db, rows)
            perf = analytics.performance(db, today=today)
            self.assertEqual(perf["daily"]["counts"]["acerto"], 1)       # only 06-14
            self.assertEqual(perf["monthly"]["counts"]["acerto"], 2)     # June entries
            self.assertEqual(perf["monthly"]["settled"], 2)              # May excluded


class TestMarketUrlMigration(unittest.TestCase):
    def test_old_db_gets_market_url_column(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "old.db")
            # Simulate a v1 DB that has every column EXCEPT market_url.
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE predictions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                        "created_at TEXT, updated_at TEXT, game_slug TEXT, game_date TEXT, "
                        "line REAL, side TEXT, status TEXT DEFAULT 'PENDENTE', stats_log TEXT)")
            con.commit()
            con.close()
            # connect() should add the missing column without error.
            con2 = pdb.connect(db)
            cols = {r["name"] for r in con2.execute("PRAGMA table_info(predictions)")}
            con2.close()
            self.assertIn("market_url", cols)

    def test_market_url_round_trips(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "p.db")
            row = _pred("OVER", None, game_date="2026-06-14")
            row["market_url"] = "https://polymarket.com/event/xyz"
            pdb.record_prediction(row, db)
            got = pdb.get_predictions(db)[0]
            self.assertEqual(got["market_url"], "https://polymarket.com/event/xyz")


class TestSupersedePending(unittest.TestCase):
    def test_rerun_voids_stale_line_keeps_settled(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "p.db")
            g = "mlb-cws-nyy-2026-06-18"
            # First run records the total-8.5 OVER.
            r1 = _pred("OVER", None, line=8.5, game_date="2026-06-18")
            r1["game_slug"] = g
            id1 = pdb.record_prediction(r1, db)
            # Re-run: best line moved to total-9.5 OVER (a new row).
            r2 = _pred("OVER", None, line=9.5, game_date="2026-06-18")
            r2["game_slug"] = g
            id2 = pdb.record_prediction(r2, db)

            voided = pdb.supersede_pending(db, g, {id2})
            self.assertEqual(voided, 1)
            by_id = {r["id"]: r["status"] for r in pdb.get_predictions(db)}
            self.assertEqual(by_id[id1], "ANULADO")   # stale line neutralized
            self.assertEqual(by_id[id2], "PENDENTE")  # current best line kept

    def test_settled_row_never_superseded(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "p.db")
            g = "mlb-cws-nyy-2026-06-18"
            r1 = _pred("UNDER", None, line=7.5, game_date="2026-06-18")
            r1["game_slug"] = g
            id1 = pdb.record_prediction(r1, db)
            pdb.settle_prediction(id1, 10.0, db)  # UNDER 7.5 vs 10 -> ERRO
            r2 = _pred("OVER", None, line=9.5, game_date="2026-06-18")
            r2["game_slug"] = g
            id2 = pdb.record_prediction(r2, db)

            voided = pdb.supersede_pending(db, g, {id2})
            self.assertEqual(voided, 0)  # nothing to void; settled is protected
            self.assertEqual(
                {r["id"]: r["status"] for r in pdb.get_predictions(db)}[id1], "ERRO")


if __name__ == "__main__":
    unittest.main(verbosity=2)
