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

    def test_broad_league_coverage(self):
        # The expanded mapping should recognize many leagues across confederations and
        # expose them for discovery (SOCCER_TAGS) + auto-ratings (API-Football ids).
        import apifootball_source as apif
        for tag in ("world-cup", "bra2", "bra", "epl", "laliga", "sea", "ligue-1",
                    "mls", "allsvenskan", "csl", "saudi", "ucl"):
            self.assertIn(tag, ss.SOCCER_TAGS, tag)
        # A spread of leagues must each map to a non-default baseline + an API id.
        for pfx in ("epl", "laliga", "seriea", "bundesliga", "ligue1", "eredivisie",
                    "mls", "bra", "bra2", "argentina", "allsvenskan", "csl", "saudi",
                    "belgium", "scotland", "ucl", "uel"):
            self.assertNotEqual(leagues.league_baseline(f"{pfx}-aaa-bbb-2026-06-14"),
                                leagues.DEFAULT_BASELINE, pfx)
            self.assertIn(pfx, apif.LEAGUE_API_ID, pfx)
        # is_soccer_slug recognizes games from newly-added leagues.
        for slug in ("allsvenskan-aik-ham-2026-06-14-total-2pt5",
                     "saudi-hil-nas-2026-06-14-btts",
                     "argentina-boc-riv-2026-06-14-total-2pt5"):
            self.assertTrue(leagues.is_soccer_slug(slug), slug)

    def test_date_window_params(self):
        # Brackets the day (±margin) so low-volume games aren't lost behind the cap.
        w = ss.date_window_params("2026-06-14")
        self.assertEqual(w["start_date_min"], "2026-06-13T00:00:00Z")
        self.assertEqual(w["end_date_max"], "2026-06-16T00:00:00Z")
        self.assertIsNone(ss.date_window_params("garbage"))

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

    def test_brasileirao_serie_b_mapping_and_url(self):
        # The user's example: a Série B game (bra2-) must map, parse, and link to /sports/bra2/.
        slug = "bra2-nov-nau-2026-06-14-total-2pt5"
        self.assertTrue(leagues.is_soccer_slug(slug))
        self.assertEqual(leagues.league_prefix(slug), "bra2")
        self.assertFalse(leagues.is_neutral(slug))      # club league, home advantage applies
        self.assertFalse(leagues.is_international(slug))  # clubs -> Club Elo, not national
        self.assertEqual(leagues.parse_teams(slug), ("nov", "nau"))
        self.assertLess(leagues.league_baseline(slug), 2.4)  # lower-scoring league
        self.assertEqual(leagues.game_url(slug),
                         "https://polymarket.com/sports/bra2/bra2-nov-nau-2026-06-14")


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


class TestImplausibleEdgeCap(unittest.TestCase):
    def test_large_edge_is_capped(self):
        # The eng-gha BTTS case from the live log: model 0.578 vs price 0.385 -> +19.3%
        # edge, above the 15% cap -> flagged implausible and excluded (no bet).
        sides = [("YES", "y", 0.385, 0.578), ("NO", "n", 0.615, 0.422)]
        chosen, notes = ss.pick_side(sides, 0.0, 1.50, 3.00)
        self.assertIsNone(chosen)
        yes = next(n for n in notes if n["side"] == "YES")
        self.assertTrue(yes["implausible"])

    def test_plausible_edge_passes(self):
        sides = [("OVER", "o", 0.50, 0.58), ("UNDER", "u", 0.50, 0.42)]  # +8% edge, within cap
        chosen, _ = ss.pick_side(sides, 0.0, 1.50, 3.00)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["side"], "OVER")


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

    def test_supersede_voids_stale_total_keeps_btts(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "p.db")
            g = "epl-ars-che-2026-06-14"
            # First run: total-2.5 OVER + a BTTS YES bet.
            old_total = spdb.record_prediction(self._row("TOTAL", "OVER", 2.5), db)
            btts_id = spdb.record_prediction(self._row("BTTS", "YES", line=None), db)
            # Re-run: best total line moved to 3.5; BTTS unchanged (re-recorded -> same id).
            new_total = spdb.record_prediction(self._row("TOTAL", "OVER", 3.5), db)
            btts_id2 = spdb.record_prediction(self._row("BTTS", "YES", line=None), db)
            self.assertEqual(btts_id, btts_id2)  # BTTS upserted in place

            voided = spdb.supersede_pending(db, g, {new_total, btts_id2})
            self.assertEqual(voided, 1)
            by_id = {r["id"]: r["status"] for r in spdb.get_predictions(db)}
            self.assertEqual(by_id[old_total], "ANULADO")   # stale total line voided
            self.assertEqual(by_id[new_total], "PENDENTE")  # current total kept
            self.assertEqual(by_id[btts_id], "PENDENTE")    # BTTS bet untouched


class TestAutoRatings(unittest.TestCase):
    def test_national_and_club_lookups(self):
        import ratings_sources as rs
        self.assertIsNotNone(rs.national_elo("nld"))     # Netherlands
        self.assertEqual(rs.national_elo("ned"), rs.national_elo("nld"))  # alias
        self.assertIsNone(rs.national_elo("zzz"))
        self.assertEqual(rs.club_elo_name("ars"), "Arsenal")
        self.assertEqual(rs.club_elo_name("rma"), "RealMadrid")
        # Expanded big-5 + Eredivisie/Primeira coverage (one per league).
        self.assertEqual(rs.club_elo_name("nap"), "Napoli")
        self.assertEqual(rs.club_elo_name("bou"), "Bournemouth")
        self.assertEqual(rs.club_elo_name("gir"), "Girona")
        self.assertEqual(rs.club_elo_name("scf"), "Freiburg")
        self.assertEqual(rs.club_elo_name("psv"), "PSV")
        self.assertIsNone(rs.club_elo_name("zzz"))
        # Conflicting 3-letter codes resolved deterministically.
        self.assertEqual(rs.club_elo_name("mon"), "Monaco")    # not Monza (mnz)
        self.assertEqual(rs.club_elo_name("mnz"), "Monza")
        self.assertEqual(rs.club_elo_name("bre"), "Brentford")  # not Bremen (wbr)
        self.assertEqual(rs.club_elo_name("wbr"), "Bremen")

    def test_world_cup_auto_national_elo_no_csv(self):
        import data_inputs as di
        # International game, no CSV -> national-team Elo (in-memory, no network).
        inp = di.get_match_inputs(None, "nld", "jpn", "fifwc", international=True, auto=True)
        self.assertIn("home_elo", inp)
        self.assertIn("away_elo", inp)
        self.assertGreater(inp["home_elo"], inp["away_elo"])  # NLD stronger than JPN

    def test_csv_overrides_auto(self):
        import data_inputs as di
        ratings = {"nld": {"elo": 1700.0}, "jpn": {"elo": 1700.0}}
        inp = di.get_match_inputs(None, "nld", "jpn", "fifwc", ratings=ratings,
                                  international=True, auto=True)
        self.assertEqual(inp["home_elo"], 1700.0)  # CSV wins over national snapshot

    def test_auto_off_returns_empty(self):
        import data_inputs as di
        self.assertEqual(di.get_match_inputs(None, "nld", "jpn", "fifwc",
                                             international=True, auto=False), {})

    def test_club_path_uses_fetcher(self):
        import data_inputs as di, ratings_sources as rs
        orig = rs.fetch_club_elo
        rs.fetch_club_elo = lambda abbr, **k: {"ars": 1950.0, "che": 1850.0}.get(abbr)
        try:
            inp = di.get_match_inputs(None, "ars", "che", "epl", international=False, auto=True)
            self.assertEqual(inp["home_elo"], 1950.0)
            self.assertEqual(inp["away_elo"], 1850.0)
        finally:
            rs.fetch_club_elo = orig


def _rich(slug, question, outcomes, prices, tokens, vol=40000, event_slug=None):
    return {"event_slug": event_slug if event_slug is not None else slug,
            "slug": slug, "question": question, "outcomes": outcomes,
            "outcome_prices": prices, "token_ids": tokens, "volume_24h": vol,
            "end_date": "2027-01-01T00:00:00Z", "accepting_orders": True,
            "game_start_time": "2026-06-14T18:00:00Z", "condition_id": "0x" + slug}


class _NoNetAPI:
    def __init__(self, *a, **k): pass
    def get(self, url, params=None): raise RuntimeError("net")


class _Args:
    def __init__(self, **kw):
        d = dict(date="2026-06-14", min_volume=1000.0, min_edge=0.05, max_edge=1.0, min_hours=0.0,
                 odds_min=1.60, odds_max=3.00, rho=dc.DEFAULT_RHO, ratings_csv=None,
                 auto_ratings=False, home_first=True, best_line_only=True, fee_rate=0.0,
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

            # Shadow model_log: both modeled markets (TOTAL + BTTS) logged with bet=1.
            import soccer_predictions as spdb_
            mlog = spdb_.get_model_log(args.predictions_db)
            self.assertEqual({r["market"] for r in mlog}, {"TOTAL", "BTTS"})
            self.assertTrue(all(r["bet"] == 1 for r in mlog))
            self.assertEqual({r["ref_side"] for r in mlog}, {"OVER", "YES"})

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

    def test_event_slug_grouping_classifies_markets(self):
        # Real Gamma shape: every market of a game shares one event_slug (the
        # game-level slug, no -total-/-btts suffix); the suffix lives on each
        # market's own slug. Classifying by the event key alone finds 0 markets
        # (the reported bug) — classification must inspect the per-market slugs.
        ev = "epl-ars-che-2026-06-14"
        markets = [
            _rich("epl-ars-che-2026-06-14-total-2pt5", "Arsenal vs Chelsea: O/U 2.5",
                  ["Over 2.5", "Under 2.5"], [0.50, 0.50], ["T25_o", "T25_u"], event_slug=ev),
            _rich("epl-ars-che-2026-06-14-total-3pt5", "Arsenal vs Chelsea: O/U 3.5",
                  ["Over 3.5", "Under 3.5"], [0.50, 0.50], ["T35_o", "T35_u"], event_slug=ev),
            _rich("epl-ars-che-2026-06-14-btts", "Arsenal vs Chelsea: Both teams to score?",
                  ["Yes", "No"], [0.50, 0.50], ["B_yes", "B_no"], event_slug=ev),
            _rich("epl-ars-che-2026-06-14", "Arsenal vs Chelsea",
                  ["Arsenal", "Chelsea"], [0.5, 0.5], ["m1", "m2"], event_slug=ev),  # moneyline
        ]
        ss.discover_markets = lambda *a, **k: ("soccer", markets)
        ss.game_date = lambda m: "2026-06-14"
        ss.APIClient = _NoNetAPI

        with tempfile.TemporaryDirectory() as d:
            csv_path = os.path.join(d, "r.csv")
            with open(csv_path, "w", encoding="utf-8") as fh:
                fh.write("team,elo,att_factor,def_factor\nars,1850,1.25,1.25\nche,1550,1.2,1.2\n")
            args = _Args(ratings_csv=csv_path, record=True,
                         predictions_db=os.path.join(d, "p.db"))
            result = ss.run(args)

            # One TOTAL event + one BTTS event, despite two total lines under the event.
            self.assertEqual(result["counts"]["total_markets"], 1)
            self.assertEqual(result["counts"]["btts_markets"], 1)
            # Both total lines are modeled (shadow-logged); best-line-only records one.
            import soccer_predictions as spdb_
            mlog = spdb_.get_model_log(args.predictions_db)
            total_lines = sorted(r["line"] for r in mlog if r["market"] == "TOTAL")
            self.assertEqual(total_lines, [2.5, 3.5])
            self.assertEqual(sum(1 for r in mlog if r["market"] == "TOTAL" and r["bet"] == 1), 1)

    def test_world_cup_auto_no_csv(self):
        # World Cup game; no ratings CSV -> national Elo computes lambdas automatically.
        markets = [
            _rich("fifwc-nld-jpn-2026-06-14-total-2pt5", "Netherlands vs Japan: O/U 2.5",
                  ["Over 2.5", "Under 2.5"], [0.50, 0.50], ["o", "u"]),
            _rich("fifwc-nld-jpn-2026-06-14-btts", "Netherlands vs Japan: Both teams to score?",
                  ["Yes", "No"], [0.50, 0.50], ["y", "n"]),
        ]
        ss.discover_markets = lambda *a, **k: ("soccer", markets)
        ss.game_date = lambda m: "2026-06-14"
        ss.APIClient = _NoNetAPI
        result = ss.run(_Args(ratings_csv=None, auto_ratings=True))  # auto national Elo, no network
        self.assertEqual(result["counts"]["total_markets"], 1)
        self.assertEqual(result["counts"]["btts_markets"], 1)
        # Model ran with real (national-Elo) inputs -> at least one suggestion or a
        # decision-tree skip, never a market-implied no-op.
        evaluated = result["counts"]["suggestions"] + result["counts"]["skipped"]
        self.assertEqual(evaluated, 2)

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
