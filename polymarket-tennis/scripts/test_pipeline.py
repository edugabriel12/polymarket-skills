#!/usr/bin/env python3
"""Offline tests: market parsing, the predictions store, and an end-to-end run with
injected tennis moneyline markets (no network).

Run: python polymarket-tennis/scripts/test_pipeline.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tennis_market as tm  # noqa: E402
import tennis_predictions as tdb  # noqa: E402
import suggest_tennis as st  # noqa: E402


def _mk(slug, outcomes, prices, tokens, event_slug=None, vol=40000):
    return {"event_slug": event_slug if event_slug is not None else slug, "slug": slug,
            "question": " vs ".join(outcomes), "outcomes": outcomes,
            "outcome_prices": prices, "token_ids": tokens, "volume_24h": vol,
            "end_date": "2027-01-01T00:00:00Z", "accepting_orders": True,
            "game_start_time": "2026-06-20T14:00:00Z", "condition_id": "0x" + slug}


class TestMarket(unittest.TestCase):
    def test_is_match_market(self):
        ml = _mk("atp-alcaraz-sinner-2026-06-20", ["Carlos Alcaraz", "Jannik Sinner"],
                 [0.55, 0.45], ["a", "b"])
        ou = _mk("atp-alcaraz-sinner-2026-06-20-total-22pt5", ["Over 22.5", "Under 22.5"],
                 [0.5, 0.5], ["o", "u"])
        self.assertTrue(tm.is_match_market(ml))
        self.assertFalse(tm.is_match_market(ou))      # over/under is not moneyline

    def test_match_sides_and_players(self):
        ml = _mk("wta-swiatek-gauff-2026-06-20", ["Iga Swiatek", "Coco Gauff"],
                 [0.62, 0.38], ["s", "g"])
        sides = tm.match_sides(ml)
        self.assertEqual(sides["sides"][0]["label"], "Iga Swiatek")
        self.assertEqual(sides["sides"][1]["token"], "g")
        self.assertTrue(sides["price_sane"])
        self.assertEqual(tm.parse_players("wta-swiatek-gauff-2026-06-20"), ("swiatek", "gauff"))

    def test_surface_inference(self):
        self.assertEqual(tm.surface_for("atp-french-open-x-y-2026-06-01"), "clay")
        self.assertEqual(tm.surface_for("atp-wimbledon-x-y-2026-07-01"), "grass")
        self.assertEqual(tm.surface_for("atp-x-y-2026-06-20"), "hard")  # default

    def test_is_tennis_slug(self):
        self.assertTrue(st.is_tennis_slug("atp-alcaraz-sinner-2026-06-20"))
        self.assertFalse(st.is_tennis_slug("cs2-spirit-falcons-2026-06-20"))  # esports


class TestStore(unittest.TestCase):
    def _row(self, side="Carlos Alcaraz", opp="Jannik Sinner", **kw):
        base = dict(match_slug="atp-alcaraz-sinner-2026-06-20", match_date="2026-06-20",
                    tour="atp", surface="hard", market_question="q", condition_id="0x",
                    token_id="t", side=side, opponent=opp, entry_price=0.5, decimal_odds=2.0,
                    model_prob=0.6, edge=0.1, elo_side=2250, elo_opp=2230, confidence=0.6,
                    size_pct=0.02, size_usd=200.0, kelly_fraction=0.1, used_external=True,
                    fee_rate=0.0, strategy="tennis-elo-moneyline",
                    market_url="https://polymarket.com/event/x", stats={"model": "surface_elo"})
        base.update(kw); return base

    def test_record_and_settle(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "p.db")
            tdb.record_prediction(self._row(), db)
            self.assertEqual(tdb.summary(db)["pendente"], 1)
            res = tdb.settle_match("atp-alcaraz-sinner-2026-06-20", "Carlos Alcaraz", db)
            self.assertEqual(res[0]["status"], "ACERTO")
            s = tdb.summary(db)
            self.assertEqual(s["acerto"], 1)
            self.assertEqual(s["win_rate"], 1.0)

    def test_settle_loss_and_void(self):
        self.assertEqual(tdb.compute_status("A", "B", "B"), "ERRO")
        self.assertEqual(tdb.compute_status("A", "B", "A"), "ACERTO")
        self.assertEqual(tdb.compute_status("A", "B", ""), "ANULADO")     # walkover/void
        self.assertEqual(tdb.compute_status("A", "B", "C"), "ANULADO")    # unknown winner

    def test_model_log_settles_ref_outcome(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "p.db")
            tdb.record_model_log({"match_slug": "atp-a-b-2026-06-20", "match_date": "2026-06-20",
                                  "ref_side": "Player A", "ref_prob": 0.6, "ref_price": 0.55,
                                  "ref_token": "tA", "bet": 1, "model_params": {"elo_a": 2200}}, db)
            n = tdb.settle_model_log(db, {"atp-a-b-2026-06-20": "Player A"})
            self.assertEqual(n, 1)
            self.assertEqual(tdb.get_model_log(db)[0]["ref_outcome"], 1)  # ref won


class _NoNetAPI:
    def __init__(self, *a, **k): pass
    def get(self, *a, **k): raise RuntimeError("net")


class _Args:
    def __init__(self, **kw):
        import elo
        d = dict(date="2026-06-20", ratings_csv=None, blend=elo.SURFACE_BLEND,
                 odds_min=elo.ODDS_MIN_DEFAULT, odds_max=elo.ODDS_MAX_DEFAULT, min_edge=0.05,
                 fee_rate=0.0, portfolio_value=10000.0, predictions_db=None, record=False,
                 output="json", verbose=False, debug=False, rate_limit=0)
        d.update(kw); self.__dict__.update(d)


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self._orig = (st.discover_markets, st.game_date, st.APIClient)

    def tearDown(self):
        st.discover_markets, st.game_date, st.APIClient = self._orig

    def test_strong_favorite_with_ratings_suggests(self):
        markets = [
            _mk("atp-alcaraz-qualifier-2026-06-20", ["Carlos Alcaraz", "Qualifier Q"],
                [0.62, 0.38], ["a", "q"]),
            # esports moneyline must be ignored despite being a 2-name market.
            _mk("cs2-spirit-falcons-2026-06-20", ["Spirit", "Falcons"], [0.5, 0.5], ["s", "f"]),
        ]
        st.discover_markets = lambda *a, **k: ("tennis", markets)
        st.game_date = lambda m: "2026-06-20"
        st.APIClient = _NoNetAPI

        with tempfile.TemporaryDirectory() as d:
            csv_path = os.path.join(d, "r.csv")
            with open(csv_path, "w", encoding="utf-8") as fh:
                # Alcaraz much stronger than an unrated qualifier -> model P well above 0.62.
                fh.write("player,elo,hard,clay,grass\nCarlos Alcaraz,2250,2250,2300,2200\n"
                         "Qualifier Q,1500,1500,1500,1500\n")
            args = _Args(ratings_csv=csv_path, record=True,
                         predictions_db=os.path.join(d, "p.db"))
            result = st.run(args)

            self.assertEqual(result["counts"]["matches"], 1)   # esports dropped
            self.assertEqual(len(result["suggestions"]), 1)
            sug = result["suggestions"][0]
            self.assertEqual(sug["side"], "Carlos Alcaraz")
            self.assertGreater(sug["edge"], 0.05)
            # Shadow log captured the modeled match (bet=1).
            mlog = tdb.get_model_log(args.predictions_db)
            self.assertEqual(len(mlog), 1)
            self.assertEqual(mlog[0]["bet"], 1)

    def test_no_ratings_is_market_implied_no_edge(self):
        markets = [_mk("atp-x-y-2026-06-20", ["Player X", "Player Y"], [0.55, 0.50], ["x", "y"])]
        st.discover_markets = lambda *a, **k: ("tennis", markets)
        st.game_date = lambda m: "2026-06-20"
        st.APIClient = _NoNetAPI
        result = _Args() and st.run(_Args())   # no ratings csv -> market-implied
        # Devig fair price ~0.524 vs price 0.55 -> negative edge -> no suggestion.
        self.assertEqual(len(result["suggestions"]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
