#!/usr/bin/env python3
"""Offline integration tests for the suggest_totals pipeline logic.

Exercises side selection, the odds filter, the decision tree, sizing caps, the
market-implied zero-edge fallback, and the projections-CSV path — all without
network. Run: python polymarket-mlb-totals/scripts/test_pipeline.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run_distribution as rd  # noqa: E402
import data_inputs  # noqa: E402
import suggest_totals as st  # noqa: E402


def totals_market(line=8.5, over=0.52, under=0.48, vol=45000):
    return {
        "question": f"Will the total runs be over or under {line}?",
        "outcomes": [f"Over {line}", f"Under {line}"],
        "outcome_prices": [over, under],
        "token_ids": ["o1", "u1"],
        "volume_24h": vol,
        "end_date": "2027-01-01T00:00:00Z",
        "accepting_orders": True,
    }


def ou_of(market):
    import totals_market as tm
    return tm.over_under_tokens(market)


class TestFallbackNoEdge(unittest.TestCase):
    def test_market_implied_yields_no_actionable_edge(self):
        line, over_price = 8.5, 0.55
        m = st.model_probabilities(
            line, over_price, 100.0, {}, league_baseline=8.5, dispersion=2.0)
        self.assertFalse(m["used_external"])
        # P(Over) reproduces the market within tolerance -> ~zero edge.
        self.assertAlmostEqual(m["p_over"], over_price, delta=2e-3)
        mkt = totals_market(line, over_price, 1 - over_price)
        chosen, _notes = st.pick_side(line, ou_of(mkt), m["p_over"], m["p_under"],
                                      0.0, 1.60, 3.00)
        # Even if a microscopic edge sneaks through, the decision tree rejects it.
        if chosen:
            passed, reason = st.decision_tree(chosen, mkt, ou_of(mkt),
                                              min_volume=10000, min_edge=0.05)
            self.assertFalse(passed, f"fallback should not pass tree: {reason}")


class TestRealInputsCreateEdge(unittest.TestCase):
    def test_strong_offense_pushes_over_edge(self):
        line, over_price = 8.5, 0.52
        # Moderate strong inputs -> mu modestly above 8.5 -> a *plausible* Over edge.
        inputs = {"home_off": 1.08, "away_off": 1.08, "home_sp": 1.04, "away_sp": 1.04,
                  "home_field": 0.1}
        m = st.model_probabilities(
            line, over_price, 100.0, inputs, league_baseline=8.5, dispersion=2.0)
        self.assertTrue(m["used_external"])
        self.assertGreater(m["mu"], 9.0)              # offense raised the mean
        self.assertGreater(m["p_over"], over_price)   # -> positive Over edge
        mkt = totals_market(line, over_price, 1 - over_price)
        chosen, _ = st.pick_side(line, ou_of(mkt), m["p_over"], m["p_under"], 0.0, 1.60, 3.00)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["side"], "OVER")
        self.assertEqual(chosen["token"], "o1")

    def test_weather_only_anchors_to_market(self):
        # The col-oak bug: weather alone must NOT override the market's expected total.
        line, over_price = 13.5, 0.475
        m = st.model_probabilities(line, over_price, 97.0,
                                   {"temp_f": 68.4, "wind_out_mph": 4.5},
                                   league_baseline=8.5, dispersion=2.0)
        self.assertFalse(m["used_external"])           # weather is not a strong input
        self.assertAlmostEqual(m["mu"], m["market_mu"], places=6)
        self.assertAlmostEqual(m["p_over"], over_price, delta=2e-3)  # ~zero edge

    def test_market_anchor_caps_mu_deviation(self):
        # Extreme inputs no longer let mu fade the market wholesale: the anchor shrinks
        # + caps the deviation (the -0.66-run backtest bias). |mu - market_mu| <= cap.
        import run_distribution as rd
        line, over_price = 12.5, 0.385
        inputs = {"home_off": 0.7, "away_off": 0.7, "home_sp": 0.8, "away_sp": 0.8}
        m = st.model_probabilities(line, over_price, 97.0, inputs,
                                   league_baseline=8.5, dispersion=2.0)
        self.assertLess(m["model_mu"], m["market_mu"])                 # raw factors go under
        self.assertLessEqual(abs(m["mu"] - m["market_mu"]), rd.MAX_MU_DEVIATION + 1e-9)
        self.assertLess(abs(m["mu"] - m["market_mu"]),
                        abs(m["model_mu"] - m["market_mu"]))           # anchored toward market

    def test_implausible_edge_guard(self):
        # MAX_PLAUSIBLE_EDGE still rejects a side whose post-fee edge exceeds the cap
        # (defense in depth; tested directly since the anchor rarely produces one).
        line = 8.5
        mkt = totals_market(line, over=0.40, under=0.60)
        chosen, notes = st.pick_side(line, ou_of(mkt), 0.95, 0.05, 0.0, 1.60, 3.00)
        over = next(n for n in notes if n["side"] == "OVER")
        self.assertTrue(over["implausible"])            # 0.95 - 0.40 = 0.55 > 0.15
        self.assertIsNone(chosen)


class TestOddsFilter(unittest.TestCase):
    def test_price_outside_band_excluded(self):
        # Over priced at 0.70 (payout 1.43x) is below the 1.60x floor -> excluded.
        line = 8.5
        mkt = totals_market(line, over=0.70, under=0.30)
        # Force a positive model edge on Over.
        chosen, notes = st.pick_side(line, ou_of(mkt), 0.80, 0.20, 0.0, 1.60, 3.00)
        self.assertIsNone(chosen)  # 0.70 fails odds band, 0.20 under has negative edge
        over_note = next(n for n in notes if n["side"] == "OVER")
        self.assertFalse(over_note["in_odds_band"])

    def test_price_inside_band_kept(self):
        line = 8.5
        mkt = totals_market(line, over=0.55, under=0.45)
        chosen, _ = st.pick_side(line, ou_of(mkt), 0.66, 0.34, 0.0, 1.60, 3.00)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["side"], "OVER")


class TestDecisionTree(unittest.TestCase):
    def test_low_volume_skips(self):
        mkt = totals_market(vol=500)
        chosen = {"side": "OVER", "token": "o1", "price": 0.55, "p_model": 0.66, "edge": 0.10}
        passed, reason = st.decision_tree(chosen, mkt, ou_of(mkt),
                                          min_volume=10000, min_edge=0.05)
        self.assertFalse(passed)
        self.assertIn("volume", reason)

    def test_small_edge_skips(self):
        mkt = totals_market()
        chosen = {"side": "OVER", "token": "o1", "price": 0.55, "p_model": 0.57, "edge": 0.02}
        passed, reason = st.decision_tree(chosen, mkt, ou_of(mkt),
                                          min_volume=10000, min_edge=0.05)
        self.assertFalse(passed)
        self.assertIn("edge", reason)

    def test_good_trade_passes(self):
        mkt = totals_market()
        chosen = {"side": "OVER", "token": "o1", "price": 0.55, "p_model": 0.66, "edge": 0.10}
        passed, reason = st.decision_tree(chosen, mkt, ou_of(mkt),
                                          min_volume=10000, min_edge=0.05)
        self.assertTrue(passed, reason)


class TestSizing(unittest.TestCase):
    def test_first_trade_cap_1pct(self):
        kh = st.advisor_kelly_half()
        # Big edge -> Kelly large, but first-trade cap clamps to 1%.
        size_pct, size_usd, kelly = st.size_position(0.75, 0.50, 10000, True, kh)
        self.assertGreater(kelly, 0.01)
        self.assertAlmostEqual(size_pct, 0.01, places=9)
        self.assertAlmostEqual(size_usd, 100.0, places=6)

    def test_established_cap_2pct(self):
        kh = st.advisor_kelly_half()
        size_pct, _u, _k = st.size_position(0.75, 0.50, 10000, False, kh)
        self.assertAlmostEqual(size_pct, 0.02, places=9)

    def test_no_edge_zero_kelly(self):
        kh = st.advisor_kelly_half()
        size_pct, size_usd, kelly = st.size_position(0.50, 0.50, 10000, True, kh)
        self.assertEqual(kelly, 0)
        self.assertEqual(size_pct, 0)


class TestProjectionCSV(unittest.TestCase):
    def test_load_factor_and_rate_columns(self):
        csv_text = ("team,off_factor,pitch_factor\n"
                    "kc,1.10,0.95\n"
                    "hou,1.05,1.02\n")
        rate_text = ("abbr,rs_per_game,ra_per_game\n"
                     "col,5.10,4.80\n")
        with tempfile.TemporaryDirectory() as d:
            p1 = os.path.join(d, "f.csv")
            with open(p1, "w", encoding="utf-8") as fh:
                fh.write(csv_text)
            table = data_inputs.load_projection_factors(p1)
            self.assertAlmostEqual(table["kc"]["off_factor"], 1.10)
            self.assertAlmostEqual(table["hou"]["pitch_factor"], 1.02)

            p2 = os.path.join(d, "r.csv")
            with open(p2, "w", encoding="utf-8") as fh:
                fh.write(rate_text)
            t2 = data_inputs.load_projection_factors(p2)
            self.assertAlmostEqual(t2["col"]["off_factor"], 5.10 / data_inputs.LEAGUE_RPG, places=6)

    def test_get_game_inputs_from_csv_offline(self):
        csv_text = ("team,off_factor,pitch_factor\n"
                    "kc,1.10,0.95\nhou,1.05,1.02\n")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "f.csv")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(csv_text)
            # api=None is fine: weather/probables are network and will be skipped.
            inputs = data_inputs.get_game_inputs(
                _NoNetAPI(), "mlb-hou-kc-2026-06-13", "2026-06-13", projections_csv=p)
            self.assertAlmostEqual(inputs["home_off"], 1.10)   # kc home
            self.assertAlmostEqual(inputs["away_off"], 1.05)   # hou away
            self.assertIn("home_field", inputs)


class TestFirstTrade(unittest.TestCase):
    def test_no_db_is_first_trade(self):
        self.assertTrue(data_inputs.is_first_trade("mlb-totals-negbin", None))
        self.assertTrue(data_inputs.is_first_trade("mlb-totals-negbin", "/no/such/file.db"))


class TestPredictionsDB(unittest.TestCase):
    """The predictions store: record (PENDENTE) -> settle (ACERTO/ERRO/ANULADO)."""

    def _pred(self, **kw):
        base = dict(game_slug="mlb-hou-kc-2026-06-13", game_date="2026-06-13",
                    market_question="total runs over/under 8.5?",
                    condition_id="0xabc", token_id="o1", line=8.5, side="OVER",
                    entry_price=0.50, decimal_odds=2.0, model_prob=0.60, edge=0.10,
                    mu=9.3, variance=18.6, dispersion=2.0, park_factor=104.0,
                    confidence=0.6, size_pct=0.01, size_usd=100.0,
                    kelly_fraction=0.2, used_external=True, fee_rate=0.0,
                    strategy="mlb-totals-negbin",
                    stats={"model": "negative_binomial", "mu": 9.3, "inputs": {}})
        base.update(kw)
        return base

    def test_record_is_pendente_with_stats_log(self):
        import predictions_db as pdb
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "p.db")
            pid = pdb.record_prediction(self._pred(), db)
            rows = pdb.get_predictions(db)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "PENDENTE")
            self.assertEqual(rows[0]["id"], pid)
            self.assertEqual(json.loads(rows[0]["stats_log"])["model"], "negative_binomial")

    def test_upsert_keeps_one_row_while_pending(self):
        import predictions_db as pdb
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "p.db")
            pdb.record_prediction(self._pred(entry_price=0.50), db)
            pdb.record_prediction(self._pred(entry_price=0.48), db)  # line moved
            rows = pdb.get_predictions(db)
            self.assertEqual(len(rows), 1)
            self.assertAlmostEqual(rows[0]["entry_price"], 0.48)  # refreshed snapshot

    def test_settle_hit_and_miss(self):
        import predictions_db as pdb
        self.assertEqual(pdb.compute_status("OVER", 8.5, 9), "ACERTO")
        self.assertEqual(pdb.compute_status("OVER", 8.5, 8), "ERRO")
        self.assertEqual(pdb.compute_status("UNDER", 8.5, 8), "ACERTO")
        self.assertEqual(pdb.compute_status("UNDER", 8.5, 9), "ERRO")
        self.assertEqual(pdb.compute_status("OVER", 9.0, 9), "ANULADO")  # push

    def test_settle_game_updates_status_and_summary(self):
        import predictions_db as pdb
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "p.db")
            pdb.record_prediction(self._pred(side="OVER", line=8.5), db)
            pdb.record_prediction(self._pred(side="OVER", line=9.5,
                                             game_slug="mlb-nyy-bos-2026-06-13"), db)
            # First game totals 10 -> OVER 8.5 is ACERTO.
            res = pdb.settle_game("mlb-hou-kc-2026-06-13", 10, db)
            self.assertEqual(res[0]["status"], "ACERTO")
            s = pdb.summary(db)
            self.assertEqual(s["acerto"], 1)
            self.assertEqual(s["pendente"], 1)   # the other game still pending
            self.assertEqual(s["win_rate"], 1.0)
            # A settled row is immutable on re-record.
            pdb.record_prediction(self._pred(side="OVER", line=8.5, entry_price=0.9), db)
            row = [r for r in pdb.get_predictions(db) if r["line"] == 8.5][0]
            self.assertEqual(row["status"], "ACERTO")
            self.assertAlmostEqual(row["actual_total"], 10.0)


class TestBestLineOneBetPerGame(unittest.TestCase):
    def test_only_best_line_recorded_others_shadowed(self):
        import suggest_totals as st
        import predictions_db as pdb
        # Two lines of the SAME matchup, both passing the edge gate (8.5 has more edge).
        markets = [
            _rich_market("mlb-hou-kc-2026-06-14-total-8pt5", "HOU vs KC O/U 8.5",
                         ["Over 8.5", "Under 8.5"], [0.52, 0.48], ["o1", "u1"]),
            _rich_market("mlb-hou-kc-2026-06-14-total-9pt5", "HOU vs KC O/U 9.5",
                         ["Over 9.5", "Under 9.5"], [0.44, 0.56], ["o2", "u2"]),
        ]
        st.discover_markets = lambda *a, **k: ("mlb", markets)
        st.game_date = lambda m: "2026-06-14"
        st.APIClient = _NoNetAPI
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "p.db")
            csv = os.path.join(d, "proj.csv")
            with open(csv, "w", encoding="utf-8") as fh:
                # Strong enough that the OVER edge survives the market anchor (cap 0.75).
                fh.write("team,off_factor,pitch_factor\nkc,1.22,1.14\nhou,1.22,1.14\n")
            result = st.run(_Args(date="2026-06-14", projections_csv=csv, use_external=True,
                                  record=True, predictions_db=db, best_line_only=True))
            # Exactly one bet recorded for the matchup (the higher-edge 8.5 line).
            self.assertEqual(result["counts"]["suggestions"], 1)
            preds = pdb.get_predictions(db)
            self.assertEqual(len(preds), 1)
            self.assertEqual(preds[0]["line"], 8.5)
            # Both lines are shadow-logged; the non-best one is bet=0 "not best line".
            mlog = {m["line"]: m for m in pdb.get_model_log(db)}
            self.assertEqual(mlog[8.5]["bet"], 1)
            self.assertEqual(mlog[9.5]["bet"], 0)
            self.assertIn("not best line", mlog[9.5]["skip_reason"])


class _NoNetAPI:
    """Stub APIClient whose .get always fails (simulates blocked network)."""
    def __init__(self, *a, **k):
        pass

    def get(self, url, params=None):
        raise RuntimeError("network disabled in test")


def _rich_market(event_slug, question, outcomes, prices, tokens, vol=50000):
    return {
        "event_slug": event_slug, "slug": event_slug, "question": question,
        "outcomes": outcomes, "outcome_prices": prices, "token_ids": tokens,
        "volume_24h": vol, "end_date": "2027-01-01T00:00:00Z",
        "accepting_orders": True, "game_start_time": "2026-06-14T23:00:00Z",
    }


class TestEndToEndRun(unittest.TestCase):
    """Full run() with injected synthetic games (no network)."""

    def setUp(self):
        self._orig = (st.discover_markets, st.game_date, st.APIClient)

    def tearDown(self):
        st.discover_markets, st.game_date, st.APIClient = self._orig

    def test_run_produces_suggestion_and_paper_dryrun(self):
        markets = [
            # Game A run-total event (own -total- slug), priced 0.50 (payout 2.0x, in band).
            _rich_market("mlb-hou-kc-2026-06-14-total-8pt5",
                         "Houston Astros vs. Kansas City Royals: O/U 8.5",
                         ["Over 8.5", "Under 8.5"], [0.50, 0.50], ["A_over", "A_under"]),
            # Moneyline event -> dropped as non-run-total (not skipped).
            _rich_market("mlb-hou-kc-2026-06-14",
                         "Will the Astros beat the Royals?",
                         ["Astros", "Royals"], [0.55, 0.45], ["A_ml1", "A_ml2"]),
            # Strikeout prop with Over/Under -> must NOT be modeled as a run total.
            _rich_market("mlb-hou-kc-2026-06-14-k-someone-5pt5",
                         "Someone: Strikeouts O/U 5.5",
                         ["Over 5.5", "Under 5.5"], [0.50, 0.50], ["K_over", "K_under"]),
            # Soccer (World Cup) total-GOALS -> filtered by the mlb- prefix.
            _rich_market("fifwc-ger-kor-2026-06-14-total-2pt5",
                         "Germany vs. Korea: O/U 2.5",
                         ["Over 2.5", "Under 2.5"], [0.50, 0.50], ["S_over", "S_under"]),
        ]
        st.discover_markets = lambda *a, **k: ("mlb", markets)
        st.game_date = lambda m: "2026-06-14"
        st.APIClient = _NoNetAPI

        with tempfile.TemporaryDirectory() as d:
            csv_path = os.path.join(d, "proj.csv")
            with open(csv_path, "w", encoding="utf-8") as fh:
                # Strong offenses/pitching -> Over edge survives the market anchor (cap 0.75).
                fh.write("team,off_factor,pitch_factor\nkc,1.22,1.14\nhou,1.22,1.14\n")

            preds_db = os.path.join(d, "preds.db")
            args = _Args(date="2026-06-14", projections_csv=csv_path,
                         use_external=True, paper=True, paper_execute=False,
                         record=True, predictions_db=preds_db)
            result = st.run(args)

            # Only the run-total event survives the filters.
            self.assertEqual(result["counts"]["games"], 1)             # the -total-8pt5 event
            self.assertEqual(result["counts"]["filtered_non_mlb"], 1)  # fifwc dropped
            self.assertEqual(result["counts"]["filtered_non_total"], 2)  # moneyline + K-prop
            # Soccer + strikeout prop never modeled or listed.
            self.assertFalse(any("fifwc" in s["game"] for s in result["suggestions"] + result["skipped"]))
            self.assertFalse(any("-k-" in s["game"] for s in result["suggestions"] + result["skipped"]))
            self.assertGreaterEqual(result["counts"]["suggestions"], 1)
            sug = result["suggestions"][0]
            rec = sug["recommendation"]
            self.assertEqual(rec["token_id"], "A_over")     # bet Over on its token
            self.assertEqual(rec["side"], "YES")
            self.assertEqual(rec["strategy"], "mlb-totals-negbin")
            self.assertLessEqual(rec["size_pct"], 0.01)     # first-trade cap
            self.assertTrue(rd.passes_odds_filter(rec["price"]))
            # --paper dry-run produced a result per suggestion.
            self.assertIn("paper_results", result)
            self.assertEqual(len(result["paper_results"]), result["counts"]["suggestions"])

            # Prediction was recorded with PENDENTE status + a stats audit log.
            import predictions_db as pdb
            self.assertIsNotNone(sug["prediction_id"])
            rows = pdb.get_predictions(preds_db, status="PENDENTE")
            self.assertGreaterEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["side"], "OVER")
            self.assertEqual(row["game_slug"], "mlb-hou-kc-2026-06-14-total-8pt5")
            stats = json.loads(row["stats_log"])
            self.assertEqual(stats["model"], "negative_binomial")
            self.assertIn("mu", stats)
            self.assertIn("inputs", stats)         # math/stats audit present

            # Shadow model_log: every modeled game logged, the bet one flagged bet=1.
            mlog = pdb.get_model_log(preds_db)
            self.assertGreaterEqual(len(mlog), 1)
            bet_rows = [r for r in mlog if r["bet"] == 1]
            self.assertGreaterEqual(len(bet_rows), 1)
            self.assertEqual(bet_rows[0]["ref_side"], "OVER")
            self.assertIsNotNone(bet_rows[0]["ref_prob"])
            self.assertIn("mu", json.loads(bet_rows[0]["model_params"]))


class _Args:
    """Minimal args namespace with pipeline defaults overridable by kwargs."""
    def __init__(self, **kw):
        defaults = dict(date=None, min_volume=1000.0, min_edge=0.05, min_sharp_edge=0.01,
                        min_hours=0.0, best_line_only=True,
                        odds_min=1.60, odds_max=3.00, dispersion=2.0,
                        league_baseline=8.5, league_prefix="mlb-",
                        fee_rate=0.0, use_external=True,
                        projections_csv=None, refresh_prices=False,
                        portfolio_value=10000.0, portfolio_db=None,
                        record=False, predictions_db=None,
                        paper=False, paper_execute=False, rate_limit=0,
                        verbose=False, debug=False)
        defaults.update(kw)
        self.__dict__.update(defaults)


class TestSharpVeto(unittest.TestCase):
    """Model drives the prediction; the sharp is an edge-sign veto (suggest only if +EV)."""

    def setUp(self):
        self._orig = (st.discover_markets, st.game_date, st.APIClient,
                      st._load_sharp_lookup, st.sharp_odds.sharp_ref, st.data_inputs.get_game_inputs)

    def tearDown(self):
        (st.discover_markets, st.game_date, st.APIClient, st._load_sharp_lookup,
         st.sharp_odds.sharp_ref, st.data_inputs.get_game_inputs) = self._orig

    def _run(self, sharp_over):
        # Modest-offense game: the model forecasts Over with a plausible edge (~7%, below the
        # 15% implausible cap) at price 0.50.
        markets = [_rich_market("mlb-aaa-bbb-2026-06-14-total-8pt5",
                                "AAA vs. BBB: O/U 8.5", ["Over 8.5", "Under 8.5"],
                                [0.50, 0.50], ["o1", "u1"])]
        st.discover_markets = lambda *a, **k: ("mlb", markets)
        st.game_date = lambda m: "2026-06-14"
        st.APIClient = _NoNetAPI
        st._load_sharp_lookup = lambda args, target, vlog: {"slate": 1}     # truthy
        st.sharp_odds.sharp_ref = lambda lk, date, away, home: (8.5, sharp_over)
        st.data_inputs.get_game_inputs = lambda *a, **k: {
            "home_off": 1.10, "away_off": 1.10, "home_sp": 1.10, "away_sp": 1.10, "home_field": 0.0}
        return st.run(_Args(date="2026-06-14", use_external=True, record=False,
                            sharp_discovery=False))

    def test_sharp_confirms_positive_edge_suggests(self):
        # Sharp fair Over ~0.60 @ 8.5 -> sharp edge on Over = 0.60-0.50 = +0.10 -> confirm.
        result = self._run(sharp_over=0.60)
        self.assertEqual(len(result["suggestions"]), 1)
        sug = result["suggestions"][0]
        self.assertGreater(sug["sharp_edge"], 0)
        self.assertEqual(sug["recommendation"]["token_id"], "o1")    # bet Over

    def test_sharp_vetoes_negative_edge_skips(self):
        # Sharp fair Over ~0.45 @ 8.5 -> sharp edge on Over = 0.45-0.50 = -0.05 -> veto, even
        # though the model loves the Over.
        result = self._run(sharp_over=0.45)
        self.assertEqual(len(result["suggestions"]), 0)
        self.assertTrue(any("sharp edge" in s["reason"] and "< 1.0% min" in s["reason"]
                            for s in result["skipped"]))

    def test_sharp_vetoes_positive_but_subthreshold_skips(self):
        # Sharp fair Over ~0.505 @ 8.5 -> sharp edge = +0.5%: positive, but below the 1% min,
        # so the hardened veto treats it as "no opinion" and skips (the old > 0 gate passed it).
        result = self._run(sharp_over=0.505)
        self.assertEqual(len(result["suggestions"]), 0)
        self.assertTrue(any("sharp edge" in s["reason"] and "< 1.0% min" in s["reason"]
                            for s in result["skipped"]))


class TestForecastLayer(unittest.TestCase):
    """The forecast layer predicts EVERY game, independent of the trade filters."""

    def _totals_event(self, slug, line=8.5, over=0.52):
        m = totals_market(line=line, over=over, under=round(1 - over, 2))
        m["slug"] = slug
        return m

    def _moneyline_event(self, slug):
        return {"slug": slug, "question": "Who wins?", "outcomes": ["AAA", "BBB"],
                "outcome_prices": [0.5, 0.5], "token_ids": ["a", "b"],
                "volume_24h": 500, "accepting_orders": True}

    def test_market_basis_forecast(self):
        # No external inputs, no sharp -> the forecast falls back to the market price.
        args = _Args(use_external=False)
        events = {"mlb-ari-stl-2026-06-23": [self._totals_event("mlb-ari-stl-2026-06-23-total-8pt5")]}
        out = st.forecast_all_mlb(None, events, {}, "2026-06-23", args, lambda *a, **k: None)
        self.assertEqual(len(out), 1)
        r = out[0]
        self.assertEqual(r["basis"], "market")
        self.assertTrue(r["has_market"])
        self.assertIsNotNone(r["forecast"])
        self.assertAlmostEqual(r["forecast"]["p_over"] + r["forecast"]["p_under"], 1.0, places=6)
        self.assertEqual(len(r["forecast"]["pi80"]), 2)          # an 80% interval is present
        self.assertGreater(r["forecast"]["pi80"][1] - r["forecast"]["pi80"][0], 4)  # wide, honest

    def test_game_without_market_and_no_inputs_has_no_basis(self):
        args = _Args(use_external=False)
        events = {"mlb-ccc-ddd-2026-06-23": [self._moneyline_event("mlb-ccc-ddd-2026-06-23")]}
        out = st.forecast_all_mlb(None, events, {}, "2026-06-23", args, lambda *a, **k: None)
        self.assertEqual(len(out), 1)
        self.assertIsNone(out[0]["forecast"])                    # nothing to model from
        self.assertFalse(out[0]["has_market"])

    def test_factors_basis_without_any_market(self):
        # A game with only a moneyline market is still forecast from team factors (no Polymarket
        # totals market needed) — the core of "predict every game".
        args = _Args(use_external=True)
        events = {"mlb-eee-fff-2026-06-23": [self._moneyline_event("mlb-eee-fff-2026-06-23")]}
        orig = st.data_inputs.get_game_inputs
        st.data_inputs.get_game_inputs = lambda *a, **k: {
            "home_off": 1.10, "away_off": 1.05, "home_sp": 0.95, "away_sp": 1.0}
        try:
            out = st.forecast_all_mlb(None, events, {}, "2026-06-23", args, lambda *a, **k: None)
        finally:
            st.data_inputs.get_game_inputs = orig
        self.assertEqual(out[0]["basis"], "factors")
        self.assertIsNotNone(out[0]["forecast"])
        self.assertFalse(out[0]["has_market"])
        self.assertIsNone(out[0]["edge_vs_market"])              # no price -> no edge


if __name__ == "__main__":
    unittest.main(verbosity=2)
