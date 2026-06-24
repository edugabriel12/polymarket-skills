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


def _mk(slug, outcomes, prices, tokens, event_slug=None, vol=40000, start="2099-01-01T00:00:00Z"):
    # start defaults to the far future so the pre-game filter (skip live/started matches) is a
    # no-op in logic tests; TestPregame exercises the filter directly with an explicit `now`.
    return {"event_slug": event_slug if event_slug is not None else slug, "slug": slug,
            "question": " vs ".join(outcomes), "outcomes": outcomes,
            "outcome_prices": prices, "token_ids": tokens, "volume_24h": vol,
            "end_date": "2027-01-01T00:00:00Z", "accepting_orders": True,
            "game_start_time": start, "condition_id": "0x" + slug}


class TestMarket(unittest.TestCase):
    def test_is_match_market(self):
        ml = _mk("atp-alcaraz-sinner-2026-06-20", ["Carlos Alcaraz", "Jannik Sinner"],
                 [0.55, 0.45], ["a", "b"])
        ou = _mk("atp-alcaraz-sinner-2026-06-20-total-22pt5", ["Over 22.5", "Under 22.5"],
                 [0.5, 0.5], ["o", "u"])
        self.assertTrue(tm.is_match_market(ml))
        self.assertFalse(tm.is_match_market(ou))      # over/under is not moneyline

    def test_excludes_handicaps_and_doubles(self):
        # Set-handicap has player-name outcomes but is NOT the match winner.
        hcap = _mk("wta-pegula-noskova-2026-06-21-set-handicap-home-1pt5",
                   ["Pegula", "Noskova"], [0.6, 0.4], ["a", "b"])
        spread = _mk("atp-x-y-2026-06-21-games-spread-4pt5", ["X", "Y"], [0.5, 0.5], ["a", "b"])
        # Doubles: singles Elo doesn't apply (pairs, '/' in names / 'doubles' in slug).
        dbl = _mk("atp-doubles-helipat-arevpav-2026-06-21",
                  ["Helioevaara/Patten", "Arevalo/Pavic"], [0.5, 0.5], ["a", "b"])
        for m in (hcap, spread, dbl):
            self.assertFalse(tm.is_match_market(m), m["slug"])
        self.assertTrue(tm.is_doubles(dbl))
        self.assertFalse(tm.is_moneyline_slug("wta-pegula-noskova-2026-06-21-set-handicap-home-1pt5"))
        self.assertTrue(tm.is_moneyline_slug("wta-pegula-noskova-2026-06-21"))

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

    def test_real_polymarket_slug(self):
        # The user's example: /sports/atp/atp-altmaie-tiafoe-2026-06-20
        slug = "atp-altmaie-tiafoe-2026-06-20"
        self.assertTrue(st.is_tennis_slug(slug))
        self.assertEqual(tm.parse_players(slug), ("altmaie", "tiafoe"))
        self.assertEqual(tm.base_match_slug(slug), slug)
        # Truncated slug token 'altmaie' must resolve to 'Daniel Altmaier' via prefix match.
        import ratings as rmod, tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "r.csv")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("player,elo,hard,clay,grass\nDaniel Altmaier,1850,1840,1900,1800\n"
                         "Frances Tiafoe,1980,2000,1850,1950\n")
            rt = rmod.load_ratings(path)
            self.assertIsNotNone(rmod.resolve("altmaie", rt))
            self.assertEqual(rmod.resolve("altmaie", rt)["clay"], 1900.0)


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
        self.assertEqual(tdb.compute_status("A", "B", ""), "ANULADO")     # no winner -> void
        self.assertEqual(tdb.compute_status("A", "B", "C"), "ANULADO")    # unknown winner

    def test_retirement_pays_the_winner_not_voided(self):
        # Polymarket pays the player who advanced on a retirement; if we backed the
        # retiree's opponent (the advancer), it's ACERTO — never ANULADO.
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "p.db")
            tdb.record_prediction(self._row(side="Frances Tiafoe", opp="Daniel Altmaier"), db)
            # Altmaier retires -> Tiafoe advances -> Tiafoe is the winner label.
            res = tdb.settle_match("atp-alcaraz-sinner-2026-06-20", "Frances Tiafoe", db)
            self.assertEqual(res[0]["status"], "ACERTO")
            self.assertEqual(tdb.summary(db)["anulado"], 0)

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
        d = dict(date="2026-06-20", ratings_csv=None, auto_ratings=False, tour="atp",
                 surface=None, blend=elo.SURFACE_BLEND, odds_min=elo.ODDS_MIN_DEFAULT,
                 odds_max=elo.ODDS_MAX_DEFAULT, min_edge=0.05, max_edge=1.0, fee_rate=0.0,
                 no_sharp=True, odds_api_key=None, sharp_tours=None, require_sharp=True,
                 sharp_min_reserve=0,
                 portfolio_value=10000.0, predictions_db=None, record=False,
                 output="json", verbose=False, debug=False, rate_limit=0)
        d.update(kw); self.__dict__.update(d)


class TestPregame(unittest.TestCase):
    """Only pre-live matches are modeled — a started match is skipped as live."""

    def setUp(self):
        from datetime import datetime, timezone
        self.now = datetime(2026, 6, 20, 15, 0, tzinfo=timezone.utc)

    def test_started_match_is_live(self):
        m = _mk("atp-a-b-2026-06-20", ["A", "B"], [0.5, 0.5], ["a", "b"],
                start="2026-06-20T14:00:00Z")          # started 1h ago
        ok, why = st.pregame_status(m, now=self.now)
        self.assertFalse(ok)
        self.assertIn("live", why)

    def test_future_match_is_pregame(self):
        m = _mk("atp-a-b-2026-06-20", ["A", "B"], [0.5, 0.5], ["a", "b"],
                start="2026-06-20T18:00:00Z")          # starts in 3h
        ok, why = st.pregame_status(m, now=self.now)
        self.assertTrue(ok)
        self.assertIsNone(why)

    def test_not_accepting_orders_is_rejected(self):
        m = _mk("atp-a-b-2026-06-20", ["A", "B"], [0.5, 0.5], ["a", "b"],
                start="2026-06-20T18:00:00Z")
        m["accepting_orders"] = False
        ok, why = st.pregame_status(m, now=self.now)
        self.assertFalse(ok)
        self.assertIn("not accepting orders", why)

    def test_missing_start_falls_through_to_accepting(self):
        m = _mk("atp-a-b-2026-06-20", ["A", "B"], [0.5, 0.5], ["a", "b"], start="")
        self.assertTrue(st.pregame_status(m, now=self.now)[0])     # can't judge -> allow


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self._orig = (st.discover_markets, st.game_date, st.APIClient, st._load_sharp_lookup)

    def tearDown(self):
        (st.discover_markets, st.game_date, st.APIClient, st._load_sharp_lookup) = self._orig

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

    def test_window_includes_next_day_excludes_beyond(self):
        markets = [
            _mk("atp-aa-bb-2026-06-20", ["AA", "BB"], [0.55, 0.50], ["a", "b"]),   # today
            _mk("atp-cc-dd-2026-06-21", ["CC", "DD"], [0.55, 0.50], ["c", "d"]),   # tomorrow (in window)
            _mk("atp-ee-ff-2026-06-25", ["EE", "FF"], [0.55, 0.50], ["e", "f"]),   # beyond the window
        ]
        st.discover_markets = lambda *a, **k: ("tennis", markets)
        st.game_date = lambda m: "-".join(m["slug"].split("-")[-3:])
        st.APIClient = _NoNetAPI
        result = st.run(_Args(date="2026-06-20", days_ahead=1))
        self.assertEqual(result["counts"]["matches"], 2)      # today + tomorrow, not 06-25

    def _veto_run(self, alcaraz_elo, djokovic_elo, sharp_alcaraz):
        """Run the pipeline: the Elo model drives the pick; the injected sharp is the veto."""
        markets = [_mk("atp-alcaraz-djokovic-2026-06-20",
                       ["Carlos Alcaraz", "Novak Djokovic"], [0.50, 0.52], ["a", "d"])]
        st.discover_markets = lambda *a, **k: ("tennis", markets)
        st.game_date = lambda m: "2026-06-20"
        st.APIClient = _NoNetAPI
        # Sharp fair P(Alcaraz) = sharp_alcaraz, keyed by surname+date like the live loader.
        st._load_sharp_lookup = lambda args, target, vlog: {
            st.sot._key("2026-06-20", "Carlos Alcaraz", "Novak Djokovic"):
                {"alcaraz": sharp_alcaraz, "djokovic": 1.0 - sharp_alcaraz}}
        d = tempfile.mkdtemp()
        csv_path = os.path.join(d, "r.csv")
        with open(csv_path, "w", encoding="utf-8") as fh:
            fh.write("player,elo,hard,clay,grass\n"
                     f"Carlos Alcaraz,{alcaraz_elo},{alcaraz_elo},{alcaraz_elo},{alcaraz_elo}\n"
                     f"Novak Djokovic,{djokovic_elo},{djokovic_elo},{djokovic_elo},{djokovic_elo}\n")
        return st.run(_Args(ratings_csv=csv_path, record=False, no_sharp=False,
                            predictions_db=os.path.join(d, "p.db")))

    def test_sharp_confirms_positive_edge_suggests(self):
        # Elo picks Alcaraz (P≈0.60 vs price 0.50 -> model edge +0.10); the sharp fair 0.62 gives
        # sharp edge +0.12 > 0 -> confirmed, suggested.
        result = self._veto_run(2230, 2160, sharp_alcaraz=0.62)
        self.assertEqual(len(result["suggestions"]), 1)
        sug = result["suggestions"][0]
        self.assertEqual(sug["side"], "Carlos Alcaraz")
        self.assertGreater(sug["sharp_edge"], 0)

    def test_sharp_vetoes_negative_edge_skips(self):
        # Elo still picks Alcaraz (model edge +0.10), but the sharp fair 0.45 gives sharp edge
        # 0.45-0.50 = -0.05 <= 0 -> vetoed, even though the model loves the pick.
        result = self._veto_run(2230, 2160, sharp_alcaraz=0.45)
        self.assertEqual(len(result["suggestions"]), 0)
        self.assertTrue(any("sharp edge" in s["reason"] and "≤ 0" in s["reason"]
                            for s in result["skipped"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
