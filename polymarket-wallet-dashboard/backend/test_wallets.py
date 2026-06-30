#!/usr/bin/env python3
"""Offline tests for the watched-wallets store + the add/list/get/delete endpoints."""

import json
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

    def test_filters_round_trip_and_update(self):
        flt = {"Soccer": {"Over/Under gols": ["Alta"]}}
        a_flt = "0x" + "cc" * 20
        wid = ws.add_wallet("F", a_flt, {"n_markets": 0, "overall": {}},
                            {"Alta": {"floor": 1, "unit": 1.0}}, "f.csv",
                            filters=flt, db_path=_DB)
        self.assertEqual(ws.get_wallet(wid, db_path=_DB)["filters"], flt)
        # a wallet added without filters stores NULL -> None
        a_none = "0x" + "dd" * 20
        wid2 = ws.add_wallet("G", a_none, {"n_markets": 0, "overall": {}}, {}, "g.csv", db_path=_DB)
        self.assertIsNone(ws.get_wallet(wid2, db_path=_DB)["filters"])
        # update_filters changes ONLY the filters (no cascade)
        self.assertTrue(ws.update_filters(wid2, flt, db_path=_DB))
        self.assertEqual(ws.get_wallet(wid2, db_path=_DB)["filters"], flt)
        ws.delete_wallet(wid, db_path=_DB)
        ws.delete_wallet(wid2, db_path=_DB)

    def test_baseline_and_reset_tracking(self):
        a = "0x" + "ba" * 20
        wid = ws.add_wallet("B", a, {"n_markets": 0, "overall": {}},
                            {"Alta": {"floor": 1, "unit": 1.0}}, "b.csv", db_path=_DB)
        # a fresh wallet has no baseline yet
        self.assertFalse(ws.baseline_established(wid, db_path=_DB))
        ws.set_baseline(wid, ["m1", "m2", "m1", ""], db_path=_DB)   # dedups + drops blank
        self.assertTrue(ws.baseline_established(wid, db_path=_DB))
        self.assertEqual(ws.baseline_markets(wid, db_path=_DB), {"m1", "m2"})
        # seed some live tracking, then reset it
        ws.upsert_bet(wid, "m9", {"event": "x", "category": "Soccer", "subcategory": "Outro",
                                  "confidence": "Alta", "side": "OVER", "total_position": 100.0,
                                  "entry_price": 0.5, "odds": 2.0, "status": "WON", "pnl": 5.0},
                      db_path=_DB)
        ws.set_seen_confidence(wid, "m9", "Alta", db_path=_DB)
        ws.mark_settled(wid, "m9", db_path=_DB)
        removed = ws.reset_tracking(wid, db_path=_DB)
        self.assertEqual(removed["wallet_bets"], 1)
        self.assertEqual(removed["baseline_markets"], 2)
        # reset nulls the baseline and wipes tracking, but KEEPS the wallet
        self.assertFalse(ws.baseline_established(wid, db_path=_DB))
        self.assertEqual(ws.baseline_markets(wid, db_path=_DB), set())
        self.assertEqual(ws.list_bets(wid, db_path=_DB), [])
        self.assertEqual(ws.seen_confidences(wid, db_path=_DB), {})
        self.assertEqual(ws.get_wallet(wid, db_path=_DB)["name"], "B")
        ws.delete_wallet(wid, db_path=_DB)


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

    def test_filters_persisted_and_tree_returned(self):
        r = self.client.post(
            "/api/wallets",
            data={"name": "Filt", "address": "0x" + "ce" * 20,
                  "filters": json.dumps({"Soccer": {"Over/Under gols": ["Alta"]}})},
            files={"file": ("hist.csv", _CSV, "text/csv")})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertNotIn("error", body)
        self.assertEqual(body["filters"], {"Soccer": {"Over/Under gols": ["Alta"]}})
        self.assertIn("filter_tree", body)               # options for the (edit) UI
        self.assertIn("Soccer", body["filter_tree"])
        self.client.delete(f"/api/wallets/{body['id']}")

    def test_patch_filters_preserves_history(self):
        import watcher
        addr = "0x" + "ef" * 20
        wid = self.client.post(
            "/api/wallets", data={"name": "PatchMe", "address": addr},
            files={"file": ("hist.csv", _CSV, "text/csv")}).json()["id"]
        # seed a live settled bet so we can prove the edit does NOT wipe history
        watcher.persist_bets(ws.get_wallet(wid), [{
            "conditionId": "cx", "initialValue": 45000, "title": "Arsenal vs. Chelsea",
            "outcome": "OVER", "avgPrice": 0.5, "curPrice": 0.5, "cashPnl": 12.0,
            "redeemable": True, "slug": "epl-ars-che-2026-06-25-total-2pt5"}])
        self.assertTrue(ws.list_bets(wid))
        pr = self.client.patch(
            f"/api/wallets/{wid}",
            data={"filters": json.dumps({"Soccer": {"Over/Under gols": ["Alta", "Média"]}})})
        self.assertEqual(pr.status_code, 200)
        self.assertEqual(pr.json()["filters"], {"Soccer": {"Over/Under gols": ["Alta", "Média"]}})
        self.assertTrue(ws.list_bets(wid))               # history survived the edit
        self.client.delete(f"/api/wallets/{wid}")

    def test_invalid_filters_rejected(self):
        r = self.client.post(
            "/api/wallets",
            data={"name": "Bad", "address": "0x" + "13" * 20, "filters": "{not json"},
            files={"file": ("hist.csv", _CSV, "text/csv")})
        self.assertIn("error", r.json())

    def test_invalid_address_rejected(self):
        r = self.client.post(
            "/api/wallets",
            data={"name": "x", "address": "not-an-address"},
            files={"file": ("hist.csv", _CSV, "text/csv")})
        self.assertIn("error", r.json())

    def test_thresholds_match_confidence_model(self):
        recs = __import__("csv_parser").parse_csv(_CSV)
        self.assertEqual(set(cm.derive_thresholds(recs)), {"Alta", "Média", "Baixa"})


class TestFilteredResults(unittest.TestCase):
    """The wallet's Resultados (analysis) AND its bet lists (/bets, /open-bets) must count only
    the bets passing the wallet's filter — while filter_tree stays the full option universe."""

    def setUp(self):
        self.client = TestClient(app.app)

    def _seed(self, wid, cond, cat, sub, conf, status, pnl, pos=100.0):
        ws.upsert_bet(wid, cond, {"event": cond, "category": cat, "subcategory": sub,
                                  "confidence": conf, "side": "OVER", "total_position": pos,
                                  "entry_price": 0.5, "odds": 2.0, "status": status, "pnl": pnl})

    def test_results_and_lists_honor_filter(self):
        addr = "0x" + "f1" * 20
        wid = self.client.post(
            "/api/wallets",
            data={"name": "Filtered", "address": addr,
                  "filters": json.dumps({"Soccer": {"Over/Under gols": ["Alta"]}})},
            files={"file": ("hist.csv", _CSV, "text/csv")}).json()["id"]
        # kept by the filter (Soccer / Over/Under gols / Alta)
        self._seed(wid, "k1", "Soccer", "Over/Under gols", "Alta", "WON", 80.0)
        self._seed(wid, "k2", "Soccer", "Over/Under gols", "Alta", "OPEN", 0.0)
        # filtered out: wrong confidence, subcategory, category (settled + open)
        self._seed(wid, "x1", "Soccer", "Over/Under gols", "Média", "WON", 30.0)
        self._seed(wid, "x2", "Soccer", "Ambas Marcam", "Alta", "LOST", -20.0)
        self._seed(wid, "x3", "Tennis", "Vencedor da partida", "Alta", "WON", 40.0)
        self._seed(wid, "x4", "Tennis", "Vencedor da partida", "Alta", "OPEN", 0.0)

        body = self.client.get(f"/api/wallets/{wid}").json()
        an = body["analysis"]
        self.assertEqual(an["live_settled"], 1)            # only k1
        self.assertEqual(an["live_open"], 1)               # only k2
        self.assertAlmostEqual(an["overall"]["total_pnl"], 80.0)
        self.assertEqual({c["category"] for c in an["by_category"]}, {"Soccer"})
        self.assertIn("Soccer", body["filter_tree"])       # full options preserved (re-add UI)

        settled = self.client.get(f"/api/wallets/{wid}/bets").json()
        self.assertEqual(settled["total"], 1)
        self.assertEqual({b["condition_id"] for b in settled["bets"]}, {"k1"})

        openb = self.client.get(f"/api/wallets/{wid}/open-bets").json()
        self.assertEqual(openb["total"], 1)
        self.assertEqual({b["condition_id"] for b in openb["bets"]}, {"k2"})

        self.client.delete(f"/api/wallets/{wid}")

    def test_no_filter_keeps_all(self):
        addr = "0x" + "f2" * 20
        wid = self.client.post(
            "/api/wallets", data={"name": "AllIn", "address": addr},
            files={"file": ("hist.csv", _CSV, "text/csv")}).json()["id"]
        self._seed(wid, "a1", "Soccer", "Over/Under gols", "Alta", "WON", 10.0)
        self._seed(wid, "a2", "Tennis", "Vencedor da partida", "Baixa", "LOST", -5.0)
        an = self.client.get(f"/api/wallets/{wid}").json()["analysis"]
        self.assertEqual(an["live_settled"], 2)            # no filter -> both count
        self.assertEqual(self.client.get(f"/api/wallets/{wid}/bets").json()["total"], 2)
        self.client.delete(f"/api/wallets/{wid}")


class TestTotalView(unittest.TestCase):
    """Carteiras tab = TOTAL (CSV + ALL live, unfiltered); Resultados tab = filtered live.
    GET /api/wallets/{id} returns both `analysis` (filtered) and `total_analysis` (total); the
    list cards reflect the total; /bets?filtered=false returns all live bets."""

    def setUp(self):
        self.client = TestClient(app.app)

    def _seed(self, wid, cond, cat, sub, conf, status, pnl, pos=100.0):
        ws.upsert_bet(wid, cond, {"event": cond, "category": cat, "subcategory": sub,
                                  "confidence": conf, "side": "OVER", "total_position": pos,
                                  "entry_price": 0.5, "odds": 2.0, "status": status, "pnl": pnl})

    def test_total_includes_csv_and_all_live(self):
        addr = "0x" + "f3" * 20
        wid = self.client.post(
            "/api/wallets",
            data={"name": "Tot", "address": addr,
                  "filters": json.dumps({"Soccer": {"Over/Under gols": ["Alta"]}})},
            files={"file": ("hist.csv", _CSV, "text/csv")}).json()["id"]
        self._seed(wid, "k1", "Soccer", "Over/Under gols", "Alta", "WON", 80.0)   # filtered-in
        self._seed(wid, "x1", "Tennis", "Vencedor da partida", "Alta", "WON", 40.0)  # filtered-out

        body = self.client.get(f"/api/wallets/{wid}").json()
        # Resultados (filtered): only the filtered-in live bet
        self.assertEqual(body["analysis"]["live_settled"], 1)
        # Total: CSV (3 settled rows in _CSV) + ALL live settled (2), unfiltered
        tot = body["total_analysis"]
        self.assertEqual(tot["overall"]["markets"], 5)
        self.assertEqual(tot["live_settled"], 2)
        self.assertIn("Tennis", {c["category"] for c in tot["by_category"]})  # unfiltered present

        # List card reflects the total (5 markets)
        card = next(w for w in self.client.get("/api/wallets").json()["wallets"] if w["id"] == wid)
        self.assertEqual(card["n_markets"], 5)
        self.assertIsNotNone(card["total_pnl"])

        # /bets: default filtered (1), filtered=false = all live settled (2)
        self.assertEqual(self.client.get(f"/api/wallets/{wid}/bets").json()["total"], 1)
        self.assertEqual(self.client.get(f"/api/wallets/{wid}/bets?filtered=false").json()["total"], 2)

        self.client.delete(f"/api/wallets/{wid}")


class TestCleanFilters(unittest.TestCase):
    """app._clean_filters semantics: blank/full → None; subset kept; {} → nothing."""

    def test_full_selection_collapses_to_none(self):
        tree = {"Soccer": {"O/U": ["Alta", "Média"]}, "Tennis": {"ML": ["Alta"]}}
        self.assertIsNone(app._clean_filters(json.dumps(tree), tree))   # all selected = no restriction

    def test_strict_subset_kept(self):
        tree = {"Soccer": {"O/U": ["Alta", "Média"], "ML": ["Baixa"]}}
        self.assertEqual(app._clean_filters(json.dumps({"Soccer": {"O/U": ["Alta"]}}), tree),
                         {"Soccer": {"O/U": ["Alta"]}})

    def test_empty_means_nothing(self):
        self.assertEqual(app._clean_filters("{}", {"Soccer": {"O/U": ["Alta"]}}), {})

    def test_blank_is_none(self):
        self.assertIsNone(app._clean_filters("", {"Soccer": {"O/U": ["Alta"]}}))

    def test_unknown_combos_dropped(self):
        tree = {"Soccer": {"O/U": ["Alta"]}}
        self.assertEqual(app._clean_filters(json.dumps({"Basketball": {"ML": ["Alta"]}}), tree), {})

    def test_confidence_normalized_and_ordered(self):
        tree = {"Soccer": {"O/U": ["Alta", "Média", "Baixa"]}}
        self.assertEqual(app._clean_filters(json.dumps({"Soccer": {"O/U": ["baixa", "alta"]}}), tree),
                         {"Soccer": {"O/U": ["Alta", "Baixa"]}})

    def test_invalid_json_raises(self):
        with self.assertRaises(ValueError):
            app._clean_filters("{not json", {"Soccer": {"O/U": ["Alta"]}})


if __name__ == "__main__":
    unittest.main(verbosity=2)
