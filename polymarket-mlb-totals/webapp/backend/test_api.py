#!/usr/bin/env python3
"""Offline API tests (FastAPI TestClient) for soccer + tennis against temp DBs.

Run inside the backend venv:
    cd polymarket-mlb-totals/webapp/backend && . .venv/bin/activate && python test_api.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp()
os.environ["SOCCER_PREDICTIONS_DB"] = os.path.join(_TMP, "soccer.db")
os.environ["TENNIS_PREDICTIONS_DB"] = os.path.join(_TMP, "tennis.db")
os.environ["DASHBOARD_CACHE_DB"] = os.path.join(_TMP, "cache.db")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fastapi.testclient import TestClient  # noqa: E402
import app as backend  # noqa: E402

# Stub the network/subprocess model runs (their logic is covered in the skills'
# offline suites); here we verify the sport-dispatch + cache wiring.
backend._run_soccer = lambda date: {
    "counts": {"total_markets": 0, "btts_markets": 0, "suggestions": 0, "skipped": 0},
    "suggestions": [], "skipped": [], "disclaimer": "test"}
backend._run_tennis = lambda date: {
    "counts": {"matches": 0, "suggestions": 0, "skipped": 0},
    "suggestions": [], "skipped": [], "disclaimer": "test"}

client = TestClient(backend.app)


class TestApi(unittest.TestCase):
    def test_health_lists_sports(self):
        r = client.get("/api/health")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(set(body["sports"]), {"soccer", "tennis"})
        self.assertNotIn("mlb", body["sports"])

    def test_health_has_no_mlb_fields(self):
        body = client.get("/api/health").json()
        self.assertNotIn("mlb_db", body)
        self.assertNotIn("odds_api_key", body)
        self.assertNotIn("sharp_close", body)
        self.assertIn("cache_db", body)

    def test_mlb_endpoints_are_gone(self):
        # The MLB-only routes were removed entirely.
        self.assertEqual(client.get("/api/clv?sport=mlb").status_code, 404)
        self.assertEqual(client.post("/api/capture-close").status_code, 404)

    def test_unknown_sport_falls_back_to_soccer(self):
        # An "mlb" (or any unknown) sport param normalizes to the soccer default.
        self.assertEqual(client.get("/api/results?sport=mlb").json()["sport"], "soccer")

    def test_seed_and_results_soccer(self):
        self.assertGreater(
            client.post("/api/seed-demo?sport=soccer&reset=true").json()["seeded"], 0)
        body = client.get("/api/results?sport=soccer").json()
        self.assertEqual(body["sport"], "soccer")
        for w in ("daily", "weekly", "monthly"):
            self.assertIn(w, body["performance"])
        self.assertIn("roi", body["performance"]["monthly"])
        self.assertGreater(len(body["recent"]), 0)
        self.assertTrue(all(row.get("market_url") for row in body["recent"]))
        self.assertTrue(any(r["market"] == "BTTS" for r in body["recent"]))
        self.assertTrue(any(r["side"] in ("YES", "NO", "OVER", "UNDER") for r in body["recent"]))

    def test_results_tennis_runs(self):
        body = client.get("/api/results?sport=tennis").json()
        self.assertEqual(body["sport"], "tennis")
        self.assertIn("monthly", body["performance"])

    def test_seed_demo_tennis_is_noop(self):
        body = client.post("/api/seed-demo?sport=tennis&reset=true").json()
        self.assertEqual(body["sport"], "tennis")
        self.assertEqual(body["seeded"], 0)

    def test_analyses_always_shows_pending_predictions(self):
        # An open PENDENTE position the (stubbed, empty) recompute does NOT surface must
        # still appear in the panel.
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
        # pending merge. Dedupe is by prediction_id.
        date = "2026-07-02"
        pid = backend.spdb.record_prediction({
            "game_slug": "epl-ars-che-2026-07-02-total-2pt5", "game_date": date, "league": "epl",
            "market": "TOTAL", "line": 2.5, "side": "OVER", "entry_price": 0.5,
            "decimal_odds": 2.0, "model_prob": 0.6, "edge": 0.1, "size_pct": 0.02,
            "size_usd": 200.0, "confidence": 0.6, "kelly_fraction": 0.04, "used_external": 0,
            "fee_rate": 0.0, "strategy": "x", "market_url": "u", "stats_log": "{}"},
            backend.SOCCER_DB)
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
        # Tennis cache is independent of soccer's.
        self.assertFalse(client.get("/api/analyses?sport=tennis&date=2026-06-14").json()["cached"])
        self.assertTrue(client.get("/api/analyses?sport=tennis&date=2026-06-14").json()["cached"])
        # force recomputes.
        self.assertFalse(client.get("/api/analyses?sport=soccer&date=2026-06-14&force=true").json()["cached"])
        # clear by sport.
        client.post("/api/cache/clear?sport=soccer")
        self.assertFalse(client.get("/api/analyses?sport=soccer&date=2026-06-14").json()["cached"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
