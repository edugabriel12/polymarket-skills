#!/usr/bin/env python3
"""Offline API tests (FastAPI TestClient) for both sports against temp DBs.

Run inside the backend venv:
    cd polymarket-mlb-totals/webapp/backend && . .venv/bin/activate && python test_api.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp()
os.environ["PREDICTIONS_DB"] = os.path.join(_TMP, "mlb.db")
os.environ["SOCCER_PREDICTIONS_DB"] = os.path.join(_TMP, "soccer.db")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fastapi.testclient import TestClient  # noqa: E402
import app as backend  # noqa: E402

# Stub the network/subprocess model runs (their logic is covered in the skills'
# offline suites); here we verify the sport-dispatch + cache wiring.
backend.settlement.settle_pending = lambda *a, **k: {"checked": 0, "settled": []}
backend.suggest_totals.run = lambda args: {
    "counts": {"games": 0, "suggestions": 0, "skipped": 0},
    "suggestions": [], "skipped": [], "disclaimer": "test", "_texts": []}
backend._run_soccer = lambda date: {
    "counts": {"total_markets": 0, "btts_markets": 0, "suggestions": 0, "skipped": 0},
    "suggestions": [], "skipped": [], "disclaimer": "test"}

client = TestClient(backend.app)


class TestApi(unittest.TestCase):
    def test_health_lists_sports(self):
        r = client.get("/api/health")
        self.assertEqual(r.status_code, 200)
        self.assertIn("mlb", r.json()["sports"])
        self.assertIn("soccer", r.json()["sports"])

    def test_health_reports_sharp_close_scheduler(self):
        body = client.get("/api/health").json()
        self.assertIn("sharp_close", body)
        self.assertIn("enabled", body["sharp_close"])
        self.assertIn("csv", body["sharp_close"])
        self.assertIn("odds_api_key", body)

    def test_clv_endpoint_handles_missing_csv(self):
        # No sharp-close CSV in the temp dir -> graceful note, not an error.
        backend.SHARP_CLOSE_CSV = os.path.join(_TMP, "no_such_close.csv")
        body = client.get("/api/clv?sport=mlb").json()
        self.assertEqual(body["sport"], "mlb")
        self.assertEqual(body["scored"], 0)
        self.assertIn("note", body)

    def test_clv_endpoint_mlb_only(self):
        self.assertFalse(client.get("/api/clv?sport=soccer").json().get("supported", True))

    def test_seed_and_results_both_sports(self):
        for sport in ("mlb", "soccer"):
            self.assertGreater(client.post(f"/api/seed-demo?sport={sport}&reset=true").json()["seeded"], 0)
            body = client.get(f"/api/results?sport={sport}").json()
            self.assertEqual(body["sport"], sport)
            for w in ("daily", "weekly", "monthly"):
                self.assertIn(w, body["performance"])
            self.assertIn("roi", body["performance"]["monthly"])
            self.assertGreater(len(body["recent"]), 0)
            self.assertTrue(all(row.get("market_url") for row in body["recent"]))
        # Soccer rows carry market + YES/NO or OVER/UNDER sides.
        soccer_recent = client.get("/api/results?sport=soccer").json()["recent"]
        self.assertTrue(any(r["market"] == "BTTS" for r in soccer_recent))
        self.assertTrue(any(r["side"] in ("YES", "NO", "OVER", "UNDER") for r in soccer_recent))

    def test_analyses_always_shows_pending_predictions(self):
        # An open PENDENTE position the (stubbed, empty) recompute does NOT surface must
        # still appear in the panel — for every sport.
        date = "2026-07-01"
        backend.spdb.record_prediction({
            "game_slug": "bra2-cui-lon-2026-07-01-total-1pt5", "game_date": date, "league": "bra2",
            "market": "TOTAL", "line": 1.5, "side": "OVER", "entry_price": 0.55,
            "decimal_odds": 1.82, "model_prob": 0.63, "edge": 0.08, "size_pct": 0.02,
            "size_usd": 200.0, "confidence": 0.6, "kelly_fraction": 0.04, "used_external": 0,
            "fee_rate": 0.0, "strategy": "divergence",
            "market_url": "https://polymarket.com/event/bra2-cui-lon-2026-07-01",
            "stats_log": "{}"}, backend.SOCCER_DB)
        body = client.get(f"/api/analyses?sport=soccer&date={date}&force=true").json()
        pend = [s for s in body["suggestions"] if s.get("status") == "PENDENTE"]
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["game"], "bra2-cui-lon-2026-07-01-total-1pt5")
        self.assertEqual(pend[0]["recommendation"]["price"], 0.55)   # PredictionCard needs this
        self.assertGreaterEqual(body["counts"]["pending_shown"], 1)

    def test_pending_not_duplicated_when_recompute_surfaces_it(self):
        # Regression: a suggestion the recompute DID surface must not be doubled by the
        # pending merge. Dedupe is by prediction_id (MLB suggestions carry no top-level
        # market/side, so the field tuple alone would mismatch the DB row -> 2 cards).
        date = "2026-07-02"
        pid = backend.spdb.record_prediction({
            "game_slug": "epl-ars-che-2026-07-02-total-2pt5", "game_date": date, "league": "epl",
            "market": "TOTAL", "line": 2.5, "side": "OVER", "entry_price": 0.5,
            "decimal_odds": 2.0, "model_prob": 0.6, "edge": 0.1, "size_pct": 0.02,
            "size_usd": 200.0, "confidence": 0.6, "kelly_fraction": 0.04, "used_external": 0,
            "fee_rate": 0.0, "strategy": "x", "market_url": "u", "stats_log": "{}"},
            backend.SOCCER_DB)
        # The recompute surfaces the SAME prediction (by prediction_id), MLB-style shape
        # (no top-level market/side — they're in `recommendation`).
        model_sug = {"game": "epl-ars-che-2026-07-02-total-2pt5", "line": 2.5,
                     "edge": 0.1, "prediction_id": pid,
                     "recommendation": {"side": "OVER", "price": 0.5, "size_pct": 0.02}}
        merged = backend._with_pending(
            {"suggestions": [model_sug], "counts": {}}, "soccer", date)
        same = [s for s in merged["suggestions"]
                if s["game"] == "epl-ars-che-2026-07-02-total-2pt5"]
        self.assertEqual(len(same), 1)                       # exactly one card, not two
        self.assertEqual(merged["counts"]["pending_shown"], 0)

    def test_analyses_cache_per_sport(self):
        r1 = client.get("/api/analyses?sport=soccer&date=2026-06-14")
        self.assertEqual(r1.json()["sport"], "soccer")
        self.assertFalse(r1.json()["cached"])
        self.assertTrue(client.get("/api/analyses?sport=soccer&date=2026-06-14").json()["cached"])
        # MLB cache is independent.
        self.assertFalse(client.get("/api/analyses?sport=mlb&date=2026-06-14").json()["cached"])
        # force recomputes.
        self.assertFalse(client.get("/api/analyses?sport=soccer&date=2026-06-14&force=true").json()["cached"])
        # clear by sport.
        client.post("/api/cache/clear?sport=soccer")
        self.assertFalse(client.get("/api/analyses?sport=soccer&date=2026-06-14").json()["cached"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
