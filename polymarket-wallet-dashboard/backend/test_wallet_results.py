#!/usr/bin/env python3
"""Offline tests for Phase-2 merge (CSV snapshot + live settled bets)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wallet_results as wres  # noqa: E402
import wallet_report as wr      # noqa: E402


def _bet(cat, sub, conf, pnl, pos, status):
    return {"category": cat, "subcategory": sub, "confidence": conf, "pnl": pnl,
            "total_position": pos, "status": status, "side": "OVER"}


class TestBetToRecord(unittest.TestCase):
    def test_won_lost_void(self):
        self.assertEqual(wres.bet_to_record(_bet("Soccer", "x", "Alta", 50, 100, "WON"))["won"], True)
        self.assertEqual(wres.bet_to_record(_bet("Soccer", "x", "Alta", -100, 100, "LOST"))["won"], False)
        self.assertIsNone(wres.bet_to_record(_bet("Soccer", "x", "Alta", 0, 100, "VOID"))["won"])
        r = wres.bet_to_record(_bet("Soccer", "x", "Alta", 50, 100, "WON"))
        self.assertEqual(r["invested"], 100.0)
        self.assertEqual(r["total_pnl"], 50.0)


class TestLiveResults(unittest.TestCase):
    def test_only_settled_live_bets_count_no_csv(self):
        live = [
            _bet("Soccer", "Over/Under gols", "Alta", 80.0, 100.0, "WON"),
            _bet("Soccer", "Over/Under gols", "Alta", -50.0, 50.0, "LOST"),
            _bet("Baseball", "Moneyline", "Média", 20.0, 40.0, "WON"),
            _bet("Soccer", "Over/Under gols", "Alta", 0.0, 999.0, "OPEN"),   # excluded from figures
        ]
        m = wres.live_results(live)
        self.assertEqual(m["live_settled"], 3)
        self.assertEqual(m["live_open"], 1)
        ov = m["overall"]
        self.assertEqual(ov["markets"], 3)                      # 3 settled only — CSV never added
        self.assertEqual(ov["wins"], 2)
        self.assertEqual(ov["losses"], 1)
        self.assertAlmostEqual(ov["total_pnl"], 50.0)           # 80 - 50 + 20
        self.assertAlmostEqual(ov["invested"], 190.0)           # 100 + 50 + 40
        cats = {c["category"] for c in m["by_category"]}
        self.assertEqual(cats, {"Soccer", "Baseball"})
        soccer = next(c for c in m["by_category"] if c["category"] == "Soccer")
        self.assertTrue(soccer["by_confidence"])

    def test_empty_until_settled(self):
        m = wres.live_results([_bet("Soccer", "Moneyline (1X2)", "Alta", 0.0, 50.0, "OPEN")])
        self.assertEqual(m["overall"]["markets"], 0)            # no settled bets yet
        self.assertEqual(m["live_open"], 1)
        self.assertEqual(m["by_category"], [])

    def test_none(self):
        m = wres.live_results([])
        self.assertEqual(m["live_settled"], 0)
        self.assertEqual(m["overall"]["markets"], 0)


class TestLiveResultsFilter(unittest.TestCase):
    """Resultados must count ONLY bets passing the wallet's filter (category+subcategory+confidence),
    the same predicate the watcher uses to forward to Sports/Telegram."""

    def _bets(self):
        return [
            _bet("Soccer", "Over/Under gols", "Alta", 80.0, 100.0, "WON"),     # kept by the subset
            _bet("Soccer", "Over/Under gols", "Média", 30.0, 60.0, "WON"),     # confidence not selected
            _bet("Soccer", "Ambas Marcam", "Alta", 25.0, 50.0, "WON"),         # subcategory not selected
            _bet("Tennis", "Vencedor da partida", "Alta", 40.0, 80.0, "WON"),  # category not selected
            _bet("Soccer", "Over/Under gols", "Alta", 0.0, 999.0, "OPEN"),     # kept, open
            _bet("Tennis", "Vencedor da partida", "Alta", 0.0, 10.0, "OPEN"),  # category not selected, open
        ]

    def test_filter_restricts_to_selected_triple(self):
        f = {"Soccer": {"Over/Under gols": ["Alta"]}}
        m = wres.live_results(self._bets(), f)
        self.assertEqual(m["live_settled"], 1)                   # only Soccer/OU/Alta settled
        self.assertEqual(m["live_open"], 1)                      # only Soccer/OU/Alta open
        ov = m["overall"]
        self.assertEqual(ov["markets"], 1)
        self.assertEqual(ov["wins"], 1)
        self.assertAlmostEqual(ov["total_pnl"], 80.0)
        self.assertEqual({c["category"] for c in m["by_category"]}, {"Soccer"})
        soccer = next(c for c in m["by_category"] if c["category"] == "Soccer")
        self.assertEqual({s["subcategory"] for s in soccer["subcategories"]}, {"Over/Under gols"})
        self.assertEqual({b["confidence"] for b in soccer["by_confidence"]}, {"Alta"})

    def test_two_confidences_selected(self):
        f = {"Soccer": {"Over/Under gols": ["Alta", "Média"]}}
        m = wres.live_results(self._bets(), f)
        self.assertEqual(m["live_settled"], 2)                   # Alta + Média Soccer/OU
        self.assertAlmostEqual(m["overall"]["total_pnl"], 110.0)  # 80 + 30

    def test_none_keeps_everything(self):
        m = wres.live_results(self._bets(), None)
        self.assertEqual(m["live_settled"], 4)
        self.assertEqual(m["live_open"], 2)

    def test_empty_filter_keeps_nothing(self):
        m = wres.live_results(self._bets(), {})
        self.assertEqual(m["live_settled"], 0)
        self.assertEqual(m["live_open"], 0)
        self.assertEqual(m["overall"]["markets"], 0)
        self.assertEqual(m["by_category"], [])


class TestTotalResults(unittest.TestCase):
    """total_results = stored CSV rollup + ALL live bets (filter IGNORED) — the Carteiras TOTAL."""

    def _csv(self):
        return wr.rollup_csv([
            {"category": "Soccer", "subcategory": "Over/Under gols", "confidence": "Alta",
             "total_pnl": 100.0, "realized_pnl": 100.0, "unrealized_pnl": 0.0, "invested": 50.0,
             "current_value": 0.0, "won": True, "n_trades": 1}])

    def test_csv_plus_all_live_unfiltered(self):
        live = [
            _bet("Soccer", "Over/Under gols", "Alta", 40.0, 80.0, "WON"),
            _bet("Tennis", "Vencedor da partida", "Baixa", 10.0, 20.0, "WON"),  # would be filtered out
            _bet("Soccer", "Over/Under gols", "Alta", 0.0, 999.0, "OPEN"),
        ]
        total = wres.total_results(self._csv(), live)
        ov = total["overall"]
        self.assertEqual(ov["markets"], 3)                  # 1 CSV + 2 live settled
        self.assertAlmostEqual(ov["total_pnl"], 150.0)      # 100 + 40 + 10
        self.assertEqual(ov["wins"], 3)
        self.assertEqual(total["live_settled"], 2)
        self.assertEqual(total["live_open"], 1)
        self.assertEqual({c["category"] for c in total["by_category"]}, {"Soccer", "Tennis"})

    def test_none_csv_is_just_live(self):
        total = wres.total_results(None, [_bet("Soccer", "Over/Under gols", "Alta", 5.0, 10.0, "WON")])
        self.assertEqual(total["overall"]["markets"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
