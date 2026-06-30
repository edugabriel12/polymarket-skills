#!/usr/bin/env python3
"""Offline tests for the 3-level rollup (overall -> category -> subcategory)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wallet_report as wr  # noqa: E402
import demo as demo_mod      # noqa: E402


def _rec(cat, sub, total, realized, invested, won, n=1):
    return {"category": cat, "subcategory": sub, "total_pnl": total, "realized_pnl": realized,
            "unrealized_pnl": round(total - realized, 2), "invested": invested,
            "current_value": 0.0, "won": won, "n_trades": n}


def _rec_conf(cat, sub, conf):
    return {"category": cat, "subcategory": sub, "confidence": conf, "total_pnl": 0.0,
            "realized_pnl": 0.0, "unrealized_pnl": 0.0, "invested": 10.0,
            "current_value": 0.0, "won": True, "n_trades": 1}


class TestRollup(unittest.TestCase):
    def test_overall_and_nesting(self):
        records = [
            _rec("Soccer", "Ambas Marcam", 100.0, 100.0, 50.0, True, 2),
            _rec("Soccer", "Ambas Marcam", -20.0, -20.0, 20.0, False, 1),
            _rec("Soccer", "Over/Under gols", 30.0, 0.0, 40.0, None, 1),   # open: not resolved
            _rec("Tennis", "Vencedor da partida", 10.0, 10.0, 25.0, True, 1),
        ]
        rep = wr.rollup("0xabc", records, n_trades_total=5)

        self.assertEqual(rep["n_markets"], 4)
        self.assertEqual(rep["n_trades"], 5)
        ov = rep["overall"]
        self.assertEqual(ov["markets"], 4)
        self.assertEqual(ov["resolved"], 3)        # the open O/U excluded
        self.assertEqual(ov["wins"], 2)
        self.assertEqual(ov["losses"], 1)
        self.assertAlmostEqual(ov["total_pnl"], 120.0)
        self.assertAlmostEqual(ov["win_rate"], 2 / 3, places=4)
        self.assertAlmostEqual(ov["roi"], 120.0 / 135.0, places=4)

        # Sorted by P&L desc -> Soccer (110) before Tennis (10).
        cats = {c["category"]: c for c in rep["by_category"]}
        self.assertEqual(rep["by_category"][0]["category"], "Soccer")
        soccer = cats["Soccer"]
        self.assertAlmostEqual(soccer["total_pnl"], 110.0)
        self.assertEqual(soccer["markets"], 3)
        self.assertEqual(soccer["resolved"], 2)
        self.assertAlmostEqual(soccer["win_rate"], 0.5, places=4)

        subs = {s["subcategory"]: s for s in soccer["subcategories"]}
        self.assertAlmostEqual(subs["Ambas Marcam"]["total_pnl"], 80.0)
        self.assertEqual(subs["Ambas Marcam"]["resolved"], 2)
        self.assertEqual(subs["Ambas Marcam"]["wins"], 1)
        self.assertIsNone(subs["Over/Under gols"]["win_rate"])    # 0 resolved -> None
        self.assertAlmostEqual(subs["Over/Under gols"]["roi"], 30.0 / 40.0, places=4)  # invested>0

    def test_empty(self):
        rep = wr.rollup("0xabc", [], 0)
        self.assertEqual(rep["n_markets"], 0)
        self.assertEqual(rep["overall"]["markets"], 0)
        self.assertIsNone(rep["overall"]["win_rate"])
        self.assertIsNone(rep["overall"]["roi"])
        self.assertEqual(rep["by_category"], [])


class TestFilterTree(unittest.TestCase):
    def test_shape_and_canonical_order(self):
        records = [
            _rec_conf("Soccer", "Over/Under gols", "Baixa"),
            _rec_conf("Soccer", "Over/Under gols", "Alta"),
            _rec_conf("Soccer", "Ambas Marcam", "Média"),
            _rec_conf("Tennis", "Vencedor da partida", "Alta"),
        ]
        tree = wr.filter_tree(records)
        self.assertEqual(set(tree), {"Soccer", "Tennis"})
        self.assertEqual(set(tree["Soccer"]), {"Over/Under gols", "Ambas Marcam"})
        self.assertEqual(tree["Soccer"]["Over/Under gols"], ["Alta", "Baixa"])  # Alta<Média<Baixa
        self.assertEqual(tree["Tennis"]["Vencedor da partida"], ["Alta"])

    def test_defaults_match_rollup(self):
        # missing category/subcategory/confidence -> the same defaults rollup_csv uses
        self.assertEqual(wr.filter_tree([{"total_pnl": 0.0, "invested": 0.0}]),
                         {"Other": {"Outro": ["—"]}})


class TestSubcategoryConfidence(unittest.TestCase):
    def test_each_subcategory_has_by_confidence(self):
        records = [
            _rec_conf("Soccer", "Over/Under gols", "Alta"),
            _rec_conf("Soccer", "Over/Under gols", "Baixa"),
            _rec_conf("Soccer", "Ambas Marcam", "Média"),
        ]
        rep = wr.rollup_csv(records)
        soccer = next(c for c in rep["by_category"] if c["category"] == "Soccer")
        self.assertTrue(all("by_confidence" in s for s in soccer["subcategories"]))
        ou = next(s for s in soccer["subcategories"] if s["subcategory"] == "Over/Under gols")
        self.assertEqual([b["confidence"] for b in ou["by_confidence"]], ["Alta", "Baixa"])
        self.assertIn("filter_tree", rep)


class TestAttachSubcategories(unittest.TestCase):
    def test_attaches_from_text(self):
        records = [{"category": "Soccer", "title": "Both teams to score",
                    "slug": "epl-x-y-btts", "eventSlug": "epl-x-y"}]
        wr.attach_subcategories(records)
        self.assertEqual(records[0]["subcategory"], "Ambas Marcam")


class TestMergeReports(unittest.TestCase):
    """merge_reports sums two rollups (e.g. stored CSV + live) by name at every level and
    recomputes win_rate/roi — the basis for the wallet TOTAL view."""

    def test_sums_and_recomputes(self):
        a = wr.rollup_csv([
            _rec("Soccer", "Over/Under gols", 100.0, 100.0, 50.0, True),
            _rec("Soccer", "Over/Under gols", -50.0, -50.0, 50.0, False),
            _rec("Tennis", "Vencedor da partida", 10.0, 10.0, 20.0, True),
        ])
        b = wr.rollup_csv([
            _rec("Soccer", "Over/Under gols", 30.0, 30.0, 30.0, True),
            _rec("Baseball", "Moneyline", 5.0, 5.0, 10.0, True),
        ])
        m = wr.merge_reports(a, b)
        self.assertEqual(m["source"], "combined")
        self.assertEqual(m["n_markets"], 5)
        ov = m["overall"]
        self.assertEqual((ov["markets"], ov["wins"], ov["losses"], ov["resolved"]), (5, 4, 1, 5))
        self.assertAlmostEqual(ov["total_pnl"], 95.0)
        self.assertAlmostEqual(ov["invested"], 160.0)
        self.assertAlmostEqual(ov["win_rate"], 0.8, places=4)            # 4/5 recomputed
        self.assertAlmostEqual(ov["roi"], round(95.0 / 160.0, 4), places=4)
        # categories aligned by name, sorted by P&L desc: Soccer(80) > Tennis(10) > Baseball(5)
        self.assertEqual([c["category"] for c in m["by_category"]], ["Soccer", "Tennis", "Baseball"])
        soccer = m["by_category"][0]
        self.assertAlmostEqual(soccer["total_pnl"], 80.0)
        self.assertEqual((soccer["wins"], soccer["losses"]), (2, 1))
        sub = next(s for s in soccer["subcategories"] if s["subcategory"] == "Over/Under gols")
        self.assertAlmostEqual(sub["total_pnl"], 80.0)
        self.assertTrue(sub["by_confidence"])

    def test_by_confidence_aligns_and_orders(self):
        a = wr.rollup_csv([_rec_conf("Soccer", "Over/Under gols", "Alta"),
                           _rec_conf("Soccer", "Over/Under gols", "Baixa")])
        b = wr.rollup_csv([_rec_conf("Soccer", "Over/Under gols", "Alta"),
                           _rec_conf("Soccer", "Over/Under gols", "Média")])
        m = wr.merge_reports(a, b)
        self.assertEqual([x["confidence"] for x in m["by_confidence"]], ["Alta", "Média", "Baixa"])
        alta = next(x for x in m["by_confidence"] if x["confidence"] == "Alta")
        self.assertEqual(alta["markets"], 2)                              # one from each side

    def test_merge_with_empty_returns_other(self):
        a = wr.rollup_csv([_rec("Soccer", "Over/Under gols", 10.0, 10.0, 20.0, True)])
        self.assertEqual(wr.merge_reports(a, {})["overall"]["markets"], 1)
        self.assertEqual(wr.merge_reports({}, a)["overall"]["markets"], 1)
        empty = wr.merge_reports({}, {})
        self.assertEqual(empty["overall"]["markets"], 0)
        self.assertEqual(empty["by_category"], [])

    def test_live_counts_carried(self):
        a = wr.rollup_csv([_rec("Soccer", "Over/Under gols", 10.0, 10.0, 20.0, True)])
        b = wr.rollup_csv([_rec("Tennis", "Vencedor da partida", 5.0, 5.0, 10.0, True)])
        b["live_settled"], b["live_open"] = 1, 2
        m = wr.merge_reports(a, b)
        self.assertEqual((m["live_settled"], m["live_open"]), (1, 2))


class TestDemoReport(unittest.TestCase):
    def test_demo_report_shape(self):
        rep = demo_mod.demo_report()
        self.assertTrue(rep.get("demo"))
        self.assertIn("overall", rep)
        self.assertIn("by_category", rep)
        self.assertGreater(len(rep["by_category"]), 3)
        # every category carries subcategories, each with the 4 headline metrics
        for c in rep["by_category"]:
            self.assertIn("win_rate", c)
            self.assertIn("roi", c)
            self.assertTrue(c["subcategories"])
            for s in c["subcategories"]:
                for k in ("markets", "total_pnl", "win_rate", "roi"):
                    self.assertIn(k, s)
        # Soccer should expose Ambas Marcam + Over/Under gols + Moneyline (1X2)
        soccer = next(c for c in rep["by_category"] if c["category"] == "Soccer")
        names = {s["subcategory"] for s in soccer["subcategories"]}
        self.assertIn("Ambas Marcam", names)
        self.assertIn("Over/Under gols", names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
