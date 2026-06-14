#!/usr/bin/env python3
"""Offline API tests (FastAPI TestClient) against a seeded temp predictions DB.

Run inside the backend venv:
    cd polymarket-mlb-totals/webapp/backend && . .venv/bin/activate
    python test_api.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

# Point the app at a throwaway DB BEFORE importing it (DB_PATH is read at import).
_TMP = tempfile.mkdtemp()
os.environ["PREDICTIONS_DB"] = os.path.join(_TMP, "predictions.db")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fastapi.testclient import TestClient  # noqa: E402
import app as backend  # noqa: E402

# Stub the network-bound calls (their logic is covered fast in the scripts'
# offline test suites); here we just verify the endpoint/cache wiring.
backend.settlement.settle_pending = lambda *a, **k: {"checked": 0, "settled": []}
backend.suggest_totals.run = lambda args: {
    "counts": {"games": 0, "suggestions": 0, "skipped": 0},
    "suggestions": [], "skipped": [], "disclaimer": "test", "_texts": [],
}

client = TestClient(backend.app)


class TestApi(unittest.TestCase):
    def test_health(self):
        r = client.get("/api/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")

    def test_seed_then_results(self):
        r = client.post("/api/seed-demo?reset=true")
        self.assertEqual(r.status_code, 200)
        self.assertGreater(r.json()["seeded"], 0)

        r = client.get("/api/results")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        # Settlement runs (no-ops offline) and analytics come back for 3 windows.
        self.assertIn("settlement", body)
        for w in ("daily", "weekly", "monthly"):
            self.assertIn(w, body["performance"])
        monthly = body["performance"]["monthly"]
        self.assertIn("roi", monthly)
        self.assertIn("win_rate_over", monthly)
        self.assertGreater(len(body["recent"]), 0)
        # Recent rows carry the Polymarket market link.
        self.assertTrue(all(row.get("market_url") for row in body["recent"]))

    def test_analyses_caches_once_per_day(self):
        # Offline run() yields 0 games, but the cache contract still holds.
        r1 = client.get("/api/analyses?date=2026-06-14")
        self.assertEqual(r1.status_code, 200)
        self.assertFalse(r1.json()["cached"])      # first computation
        r2 = client.get("/api/analyses?date=2026-06-14")
        self.assertTrue(r2.json()["cached"])        # served from cache
        self.assertEqual(r1.json()["computed_at"], r2.json()["computed_at"])
        # force recomputes.
        r3 = client.get("/api/analyses?date=2026-06-14&force=true")
        self.assertFalse(r3.json()["cached"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
