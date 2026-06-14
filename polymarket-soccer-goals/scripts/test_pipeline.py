#!/usr/bin/env python3
"""Offline tests for the soccer pipeline: market parsing, leagues, predictions,
and an end-to-end run with injected total-goals + BTTS games (no network).

Run: python polymarket-soccer-goals/scripts/test_pipeline.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import leagues  # noqa: E402
import soccer_market as sm  # noqa: E402
import soccer_predictions as spdb  # noqa: E402
import dixon_coles as dc  # noqa: E402
import suggest_soccer as ss  # noqa: E402


class TestLeagues(unittest.TestCase):
    def test_prefix_and_baseline(self):
        self.assertEqual(leagues.league_prefix("epl-ars-che-2026-06-14-total-2pt5"), "epl")
        self.assertEqual(leagues.league_prefix("fifwc-ger-kor-2026-06-14-btts"), "fifwc")
        self.assertGreater(leagues.league_baseline("bundesliga-bay-dor-2026-06-14-total-3pt5"), 3.0)
        self.assertTrue(leagues.is_neutral("fifwc-ger-kor-2026-06-14-total-2pt5"))
        self.assertFalse(leagues.is_neutral("epl-ars-che-2026-06-14-total-2pt5"))

    def test_parse_teams(self):
        self.assertEqual(leagues.parse_teams("epl-ars-che-2026-06-14-total-2pt5"), ("ars", "che"))
        self.assertEqual(leagues.parse_teams("epl-ars-che-2026-06-14", home_first=False), ("che", "ars"))

    def test_is_soccer(self):
        self.assertTrue(leagues.is_soccer_slug("epl-ars-che-2026-06-14-total-2pt5"))
        self.assertFalse(leagues.is_soccer_slug("mlb-hou-kc-2026-06-14-total-8pt5"))

    def test_world_cup_mapping_and_url(self):
        # The user's example: a neutral-venue World Cup game must map + link to /sports/world-cup/.
        slug = "fifwc-nld-jpn-2026-06-14-total-2pt5"
        self.assertTrue(leagues.is_soccer_slug(slug))
        self.assertEqual(leagues.league_prefix(slug), "fifwc")
        self.assertTrue(leagues.is_neutral(slug))  # no home advantage at the World Cup
        self.assertEqual(leagues.parse_teams(slug), ("nld", "jpn"))
        self.assertEqual(leagues.game_url(slug),
                         "https://polymarket.com/sports/world-cup/fifwc-nld-jpn-2026-06-14")
        self.assertEqual(leagues.game_url("fifwc-nld-jpn-2026-06-14-btts"),
                         "https://polymarket.com/sports/world-cup/fifwc-nld-jpn-2026-06-14")


class TestMarketParsing(unittest.TestCase):
    def test_total_and_btts_detection(self):
        total = {"question": "Arsenal vs Chelsea: O/U 2.5", "slug": "epl-ars-che-2026-06-14-total-2pt5",
                 "outcomes": ["Over 2.5", "Under 2.5"], "outcome_prices": [0.55, 0.45], "token_ids": ["o", "u"]}
        btts = {"question": "Arsenal vs Chelsea: Both teams to score?", "slug": "epl-ars-che-2026-06-14-btts",
                "outcomes": ["Yes", "No"], "outcome_prices": [0.58, 0.42], "token_ids": ["y", "n"]}
        self.assertTrue(sm.is_total_market(total) and not sm.is_btts_market(total))
        self.assertTrue(sm.is_btts_market(btts) and not sm.is_total_market(btts))
        self.assertEqual(sm.parse_total_line(total), 2.5)
        self.assertEqual(sm.over_under_tokens(total)["over_token"], "o")
        self.assertEqual(sm.btts_tokens(btts)["yes_token"], "y")
        self.assertTrue(sm.GAME_TOTAL_RE.search("epl-ars-che-2026-06-14-total-2pt5"))
        self.assertTrue(sm.GAME_BTTS_RE.search("epl-ars-che-2026-06-14-btts"))

    def test_btts_outcome_swap(self):
        btts = {"question": "btts?", "slug": "x-btts", "outcomes": ["No", "Yes"],
                "outcome_prices": [0.42, 0.58], "token_ids": ["n", "y"]}
        t = sm.btts_tokens(btts)
        self.assertEqual((t["yes_token"], t["no_token"]), ("y", "n"))


class TestPredictions(unittest.TestCase):
    def _row(self, market, side, line=2.5, **kw):
        base = dict(game_slug="epl-ars-che-2026-06-14", game_date="2026-06-14", league="epl",
                    market=market, market_question="q", condition_id="0x", token_id="t",
                    line=line, side=side, entry_price=0.5, decimal_odds=2.0, model_prob=0.6,
                    edge=0.1, lam_home=1.6, lam_away=1.2, rho=-0.1, confidence=0.6, size_pct=0.01,
                    size_usd=100.0, kelly_fraction=0.2, used_external=True, fee_rate=0.0,
                    strategy="soccer-goals-dc", market_url="https://polymarket.com/event/x",
                    stats={"model": "dixon_coles"})
        base.update(kw); return base

    def test_compute_status(self):
        self.assertEqual(spdb.compute_status("TOTAL", "OVER", 2.5, actual_total=3), "ACERTO")
        self.assertEqual(spdb.compute_status("TOTAL", "OVER", 2.5, actual_total=2), "ERRO")
        self.assertEqual(spdb.compute_status("TOTAL", "UNDER", 3.0, actual_total=3), "ANULADO")
        self.assertEqual(spdb.compute_status("BTTS", "YES", -1, actual_btts=True), "ACERTO")
        self.assertEqual(spdb.compute_status("BTTS", "YES", -1, actual_btts=False), "ERRO")
        self.assertEqual(spdb.compute_status("BTTS", "NO", -1, actual_btts=False), "ACERTO")

    def test_record_and_settle(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "p.db")
            spdb.record_prediction(self._row("TOTAL", "OVER", 2.5), db)
            spdb.record_prediction(self._row("BTTS", "YES", line=None), db)
            self.assertEqual(spdb.summary(db)["pendente"], 2)
            # Game finished 3-1: total 4 (>2.5 -> Over ACERTO), both scored (BTTS YES ACERTO).
            res = spdb.settle_game("epl-ars-che-2026-06-14", db, actual_total=4, actual_btts=True)
            statuses = sorted(r["status"] for r in res)
            self.assertEqual(statuses, ["ACERTO", "ACERTO"])
            s = spdb.summary(db)
            self.assertEqual(s["acerto"], 2)
            self.assertEqual(s["win_rate"], 1.0)

    def test_btts_yes_no_both_allowed(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "p.db")
            spdb.record_prediction(self._row("BTTS", "NO", line=None), db)  # CHECK allows NO
            self.assertEqual(len(spdb.get_predictions(db)), 1)


def _rich(slug, question, outcomes, prices, tokens, vol=40000):
    return {"event_slug": slug, "slug": slug, "question": question, "outcomes": outcomes,
            "outcome_prices": prices, "token_ids": tokens, "volume_24h": vol,
            "end_date": "2027-01-01T00:00:00Z", "accepting_orders": True,
            "game_start_time": "2026-06-14T18:00:00Z", "condition_id": "0x" + slug}


class _NoNetAPI:
    def __init__(self, *a, **k): pass
    def get(self, url, params=None): raise RuntimeError("net")


class _Args:
    def __init__(self, **kw):
        d = dict(date="2026-06-14", min_volume=1000.0, min_edge=0.05, min_hours=0.0,
                 odds_min=1.60, odds_max=3.00, rho=dc.DEFAULT_RHO, ratings_csv=None,
                 use_clubelo=False, home_first=True, best_line_only=True, fee_rate=0.0,
                 portfolio_value=10000.0, portfolio_db=None, record=False,
                 predictions_db=None, rate_limit=0, verbose=False, debug=False)
        d.update(kw); self.__dict__.update(d)


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self._orig = (ss.discover_markets, ss.game_date, ss.APIClient)

    def tearDown(self):
        ss.discover_markets, ss.game_date, ss.APIClient = self._orig

    def test_run_total_and_btts(self):
        markets = [
            _rich("epl-ars-che-2026-06-14-total-2pt5", "Arsenal vs Chelsea: O/U 2.5",
                  ["Over 2.5", "Under 2.5"], [0.50, 0.50], ["T_over", "T_under"]),
            _rich("epl-ars-che-2026-06-14-btts", "Arsenal vs Chelsea: Both teams to score?",
                  ["Yes", "No"], [0.50, 0.50], ["B_yes", "B_no"]),
            _rich("epl-ars-che-2026-06-14", "Arsenal vs Chelsea",
                  ["Arsenal", "Chelsea"], [0.5, 0.5], ["m1", "m2"]),  # moneyline -> dropped
        ]
        ss.discover_markets = lambda *a, **k: ("soccer", markets)
        ss.game_date = lambda m: "2026-06-14"
        ss.APIClient = _NoNetAPI

        with tempfile.TemporaryDirectory() as d:
            csv_path = os.path.join(d, "r.csv")
            with open(csv_path, "w", encoding="utf-8") as fh:
                # Strong home Elo + high attack factors -> mu up, supremacy up.
                fh.write("team,elo,att_factor,def_factor\nars,1850,1.25,1.25\nche,1550,1.2,1.2\n")
            args = _Args(ratings_csv=csv_path, record=True,
                         predictions_db=os.path.join(d, "p.db"))
            result = ss.run(args)

        self.assertEqual(result["counts"]["total_markets"], 1)
        self.assertEqual(result["counts"]["btts_markets"], 1)
        self.assertGreaterEqual(result["counts"]["suggestions"], 1)
        markets_suggested = {s["market"] for s in result["suggestions"]}
        self.assertTrue(markets_suggested.issubset({"TOTAL", "BTTS"}))
        # Moneyline event never modeled.
        self.assertFalse(any(s["game"] == "epl-ars-che-2026-06-14" for s in result["suggestions"]))
        for s in result["suggestions"]:
            rec = s["recommendation"]
            self.assertEqual(rec["side"], "YES")
            self.assertLessEqual(rec["size_pct"], 0.01)  # first-trade cap
            self.assertTrue(dc.passes_odds_filter(rec["price"]))

    def test_fallback_no_inputs_no_suggestion(self):
        markets = [_rich("epl-ars-che-2026-06-14-total-2pt5", "O/U 2.5",
                         ["Over 2.5", "Under 2.5"], [0.55, 0.45], ["o", "u"])]
        ss.discover_markets = lambda *a, **k: ("soccer", markets)
        ss.game_date = lambda m: "2026-06-14"
        ss.APIClient = _NoNetAPI
        result = ss.run(_Args(ratings_csv=None))  # no inputs -> market-implied -> ~0 edge
        # Either no suggestion, or any that slipped through has tiny edge below threshold.
        for s in result["suggestions"]:
            self.assertLess(abs(s["edge"]), 0.05)


if __name__ == "__main__":
    unittest.main(verbosity=2)
