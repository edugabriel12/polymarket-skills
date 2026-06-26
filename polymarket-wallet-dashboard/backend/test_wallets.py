#!/usr/bin/env python3
"""Offline tests for the watched-wallets store + the add/list/get/delete endpoints."""

import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp()
os.environ["DASHBOARD_WALLETS_DB"] = os.path.join(_TMP, "wallets.db")
os.environ["AUTO_BRAIN"] = "0"   # don't start the model/watch scheduler during tests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wallets_store as ws  # noqa: E402
import confidence_model as cm  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import app  # noqa: E402

_DB = os.path.join(_TMP, "store.db")
_CSV = (
    "Data;Evento;Aposta;Conf.;Odd;Investido;ROI%;Lucro\n"
    '2026-06-25;"Curaçao vs. Côte d\'Ivoire: O/U 3.5";UNDER;Média;1,79;19999,96;78,6;15714,32\n'
    '2026-06-24;"Bosnia vs. Qatar: O/U 2.5";OVER;Alta;1,68;99999,74;67,9;67924,81\n'
    '2026-06-23;"Will Algeria win on 2026-06-23?";YES;Baixa;1,54;5999,99;-100;-5999,99\n'
).encode("utf-8")
_ADDR = "0x" + "ab" * 20


class TestStore(unittest.TestCase):
    def test_crud(self):
        analysis = {"n_markets": 3, "overall": {"win_rate": 0.66, "total_pnl": 77.0, "roi": 0.1}}
        thresholds = {"Alta": {"floor": 40000, "unit": 1.0}}
        wid = ws.add_wallet("Trader X", _ADDR, analysis, thresholds, "x.csv", db_path=_DB)
        self.assertTrue(wid)
        got = ws.get_wallet(wid, db_path=_DB)
        self.assertEqual(got["name"], "Trader X")
        self.assertEqual(got["address"], _ADDR.lower())
        self.assertEqual(got["analysis"]["n_markets"], 3)
        self.assertIn("Alta", got["thresholds"])
        self.assertEqual(len(ws.list_wallets(db_path=_DB)), 1)
        # upsert by address (no duplicate)
        ws.add_wallet("Trader X (renamed)", _ADDR, analysis, thresholds, "x.csv", db_path=_DB)
        self.assertEqual(len(ws.list_wallets(db_path=_DB)), 1)
        self.assertEqual(ws.get_wallet(wid, db_path=_DB)["name"], "Trader X (renamed)")
        self.assertTrue(ws.delete_wallet(wid, db_path=_DB))
        self.assertEqual(ws.list_wallets(db_path=_DB), [])


class TestEndpoints(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app.app)

    def test_add_then_list_get_delete(self):
        r = self.client.post(
            "/api/wallets",
            data={"name": "Oneger", "address": _ADDR},
            files={"file": ("hist.csv", _CSV, "text/csv")})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertNotIn("error", body)
        wid = body["id"]
        # thresholds derived from the 3 confidence levels in the CSV
        self.assertIn("Alta", body["thresholds"])
        self.assertEqual(body["thresholds"]["Alta"]["unit"], 1.0)
        # analysis present with by_confidence
        self.assertIn("by_confidence", body["analysis"])

        lst = self.client.get("/api/wallets").json()["wallets"]
        self.assertTrue(any(w["id"] == wid for w in lst))

        one = self.client.get(f"/api/wallets/{wid}").json()
        self.assertEqual(one["address"], _ADDR.lower())

        self.assertTrue(self.client.delete(f"/api/wallets/{wid}").json()["deleted"])

    def test_invalid_address_rejected(self):
        r = self.client.post(
            "/api/wallets",
            data={"name": "x", "address": "not-an-address"},
            files={"file": ("hist.csv", _CSV, "text/csv")})
        self.assertIn("error", r.json())

    def test_thresholds_match_confidence_model(self):
        recs = __import__("csv_parser").parse_csv(_CSV)
        self.assertEqual(set(cm.derive_thresholds(recs)), {"Alta", "Média", "Baixa"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
