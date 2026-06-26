#!/usr/bin/env python3
"""Offline tests for tennis settlement — winner matching across feeds + the source chain.

The live bug: settlement fetched ONLY Sackmann, which 404'd for the season, so 0 pending
settled. Now it uses ratings_source.fetch_matches (Sackmann -> tennis-data fallback), and the
winner lookup matches by full-name OR surname pair so tennis-data's "Surname I." names still
settle predictions stored with full names.

Run: python polymarket-tennis/scripts/test_tennis_results.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tennis_results as tr  # noqa: E402
import tennis_predictions as tdb  # noqa: E402
import ratings_source  # noqa: E402


class TestWinnerLookup(unittest.TestCase):
    def test_full_name_feed(self):
        lk = tr.build_winner_lookup([
            {"winner": "Carlos Alcaraz", "loser": "Jannik Sinner"}])
        self.assertEqual(tr.lookup_winner(lk, "Carlos Alcaraz", "Jannik Sinner"), "Carlos Alcaraz")

    def test_surname_initial_feed_matches_full_name_prediction(self):
        # tennis-data.co.uk writes "Surname I." — must still settle a full-name prediction.
        lk = tr.build_winner_lookup([
            {"winner": "Alcaraz C.", "loser": "Sinner J."},
            {"winner": "Bautista Agut R.", "loser": "Norrie C."}])
        self.assertEqual(tr.lookup_winner(lk, "Carlos Alcaraz", "Jannik Sinner"), "Alcaraz C.")
        self.assertEqual(tr.lookup_winner(lk, "alcaraz", "sinner"), "Alcaraz C.")    # surname slug
        self.assertEqual(tr.lookup_winner(lk, "Roberto Bautista Agut", "Cameron Norrie"),
                         "Bautista Agut R.")

    def test_unknown_pair_returns_none(self):
        lk = tr.build_winner_lookup([{"winner": "Alcaraz C.", "loser": "Sinner J."}])
        self.assertIsNone(tr.lookup_winner(lk, "Rafael Nadal", "Novak Djokovic"))

    def test_ambiguous_surname_not_aliased(self):
        # Two matches with a shared surname pair -> the surname alias is unsafe, so omitted.
        lk = tr.build_winner_lookup([
            {"winner": "Williams S.", "loser": "Williams V."},
            {"winner": "Williams V.", "loser": "Williams S."}])
        # Full-name pairs still resolve; the bare-surname pair must NOT (ambiguous).
        self.assertIsNone(lk.get(frozenset({"williams", "williams"})))


class TestSettlePendingChain(unittest.TestCase):
    def setUp(self):
        self._orig = ratings_source.fetch_matches

    def tearDown(self):
        ratings_source.fetch_matches = self._orig

    def test_settles_from_surname_feed_via_chain(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "t.db")
            tdb.record_prediction({
                "match_slug": "atp-alcaraz-sinner-2026-06-25", "match_date": "2026-06-25",
                "tour": "atp", "surface": "clay", "side": "Carlos Alcaraz",
                "opponent": "Jannik Sinner", "entry_price": 0.5, "decimal_odds": 2.0,
                "model_prob": 0.6, "edge": 0.1, "confidence": 0.6, "size_pct": 0.02,
                "size_usd": 200.0, "kelly_fraction": 0.04, "used_external": 1, "fee_rate": 0.0,
                "strategy": "divergence", "market_url": "https://polymarket.com/x"}, db)
            self.assertEqual(len(tdb.get_predictions(db, status="PENDENTE")), 1)

            # Simulate Sackmann 404 -> tennis-data fallback returning "Surname I." names.
            ratings_source.fetch_matches = lambda tour, years=None, debug=False: [
                {"date": "20260625", "surface": "clay", "winner": "Alcaraz C.",
                 "loser": "Sinner J."}]
            out = tr.settle_pending(db, tour="atp")

            self.assertEqual(out["checked"], 1)
            self.assertEqual(len(out["settled"]), 1)
            self.assertEqual(out["games_matched"], 1)
            # The feed labels the winner "Alcaraz C." but the bet was on "Carlos Alcaraz" —
            # settlement must resolve to ACERTO, not ANULADO (exact-match would have voided it).
            self.assertEqual(out["settled"][0]["status"], "ACERTO")
            self.assertEqual(len(tdb.get_predictions(db, status="PENDENTE")), 0)   # no longer pending

    def test_no_feed_keeps_pending(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "t.db")
            tdb.record_prediction({
                "match_slug": "atp-a-b-2026-06-25", "match_date": "2026-06-25", "tour": "atp",
                "surface": "hard", "side": "A", "opponent": "B", "entry_price": 0.5,
                "decimal_odds": 2.0, "model_prob": 0.6, "edge": 0.1, "confidence": 0.6,
                "size_pct": 0.02, "size_usd": 200.0, "kelly_fraction": 0.04, "used_external": 0,
                "fee_rate": 0.0, "strategy": "x", "market_url": "u"}, db)
            ratings_source.fetch_matches = lambda tour, years=None, debug=False: []
            out = tr.settle_pending(db, tour="atp")
            self.assertEqual(out["checked"], 1)
            self.assertEqual(out["settled"], [])
            self.assertEqual(out["finals_found"], 0)
            self.assertTrue(any("EMPTY feed" in d for d in out["diagnostics"]))
            self.assertEqual(len(tdb.get_predictions(db, status="PENDENTE")), 1)   # stays pending

    def test_unsettled_diagnostic_explains_why(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "t.db")
            tdb.record_prediction({
                "match_slug": "atp-alcaraz-sinner-2026-06-25", "match_date": "2026-06-25",
                "tour": "atp", "surface": "clay", "side": "Carlos Alcaraz",
                "opponent": "Jannik Sinner", "entry_price": 0.5, "decimal_odds": 2.0,
                "model_prob": 0.6, "edge": 0.1, "confidence": 0.6, "size_pct": 0.02,
                "size_usd": 200.0, "kelly_fraction": 0.04, "used_external": 1, "fee_rate": 0.0,
                "strategy": "x", "market_url": "u"}, db)
            # Feed has OTHER matches (neither player) -> must explain "neither player in the feed".
            ratings_source.fetch_matches = lambda tour, years=None, debug=False: [
                {"date": "20260625", "surface": "clay", "winner": "Rafael Nadal",
                 "loser": "Novak Djokovic"}]
            out = tr.settle_pending(db, tour="atp")
            self.assertEqual(out["settled"], [])
            self.assertEqual(out["finals_found"], 1)
            joined = " ".join(out["diagnostics"])
            self.assertIn("UNSETTLED", joined)
            self.assertIn("neither player is in the feed", joined)
            self.assertIn("feed[atp] surname sample", joined)

    def test_each_prediction_settles_against_its_own_tour_feed(self):
        # The live bug: settlement queried only the ATP feed, so WTA predictions never settled.
        # Each prediction must settle against the feed of ITS OWN tour (atp-… vs wta-… slug).
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "t.db")
            tdb.record_prediction({
                "match_slug": "atp-alcaraz-sinner-2026-06-25", "match_date": "2026-06-25",
                "tour": "atp", "surface": "clay", "side": "Carlos Alcaraz",
                "opponent": "Jannik Sinner", "entry_price": 0.5, "decimal_odds": 2.0,
                "model_prob": 0.6, "edge": 0.1, "confidence": 0.6, "size_pct": 0.02,
                "size_usd": 200.0, "kelly_fraction": 0.04, "used_external": 1, "fee_rate": 0.0,
                "strategy": "x", "market_url": "u"}, db)
            tdb.record_prediction({
                "match_slug": "wta-swiatek-sabalenka-2026-06-25", "match_date": "2026-06-25",
                "tour": "wta", "surface": "clay", "side": "Iga Swiatek",
                "opponent": "Aryna Sabalenka", "entry_price": 0.5, "decimal_odds": 2.0,
                "model_prob": 0.6, "edge": 0.1, "confidence": 0.6, "size_pct": 0.02,
                "size_usd": 200.0, "kelly_fraction": 0.04, "used_external": 1, "fee_rate": 0.0,
                "strategy": "x", "market_url": "u"}, db)

            # Each tour's feed carries ONLY its own match — settlement must query both.
            feeds = {
                "atp": [{"date": "20260625", "surface": "clay", "winner": "Alcaraz C.",
                         "loser": "Sinner J."}],
                "wta": [{"date": "20260625", "surface": "clay", "winner": "Swiatek I.",
                         "loser": "Sabalenka A."}],
            }
            ratings_source.fetch_matches = lambda tour, years=None, debug=False: feeds[tour]
            out = tr.settle_pending(db)

            self.assertEqual(out["checked"], 2)
            self.assertEqual(out["games_matched"], 2)            # both tours settled
            self.assertEqual(len(out["settled"]), 2)
            self.assertEqual(len(tdb.get_predictions(db, status="PENDENTE")), 0)
            joined = " ".join(out["diagnostics"])
            self.assertIn("tour(s) ['atp', 'wta']", joined)


class _FakeGamma:
    """Serves Gamma /markets by condition_ids OR slug from {condition_id|slug: market_dict}."""

    def __init__(self, by_cid=None, by_slug=None):
        self.by_cid = by_cid or {}
        self.by_slug = by_slug or {}
        self.calls = 0
        self.slug_calls = 0
        self.seen_params = []

    def get(self, url, params=None):
        self.calls += 1
        params = params or {}
        self.seen_params.append(params)
        if "slug" in params:
            self.slug_calls += 1
            m = self.by_slug.get(params["slug"])
            return [m] if m else []
        ids = params.get("condition_ids")
        ids = ids if isinstance(ids, list) else [ids]
        return [self.by_cid[c] for c in ids if c in self.by_cid]


def _resolved_market(cid, tokens, outcomes, prices, slug=None):
    m = {"conditionId": cid, "closed": True, "umaResolutionStatus": "resolved",
         "clobTokenIds": json.dumps(tokens), "outcomes": json.dumps(outcomes),
         "outcomePrices": json.dumps(prices)}
    if slug:
        m["slug"] = slug
    return m


class TestWinnerFromMarket(unittest.TestCase):
    def test_our_token_won(self):
        pred = {"side": "Xiaodi You", "opponent": "Leolia Jeanjean", "token_id": "tok_you"}
        m = _resolved_market("c1", ["tok_you", "tok_jean"],
                             ["Xiaodi You", "Leolia Jeanjean"], ["1", "0"])
        self.assertEqual(tr.winner_from_market(pred, m), "Xiaodi You")

    def test_our_token_lost(self):
        pred = {"side": "Xiaodi You", "opponent": "Leolia Jeanjean", "token_id": "tok_you"}
        m = _resolved_market("c1", ["tok_you", "tok_jean"],
                             ["Xiaodi You", "Leolia Jeanjean"], ["0", "1"])
        self.assertEqual(tr.winner_from_market(pred, m), "Leolia Jeanjean")

    def test_open_market_returns_none(self):
        pred = {"side": "A", "opponent": "B", "token_id": "ta"}
        m = {"conditionId": "c", "closed": False, "umaResolutionStatus": "",
             "clobTokenIds": json.dumps(["ta", "tb"]), "outcomes": json.dumps(["A", "B"]),
             "outcomePrices": json.dumps(["0.6", "0.4"])}
        self.assertIsNone(tr.winner_from_market(pred, m))

    def test_resolved_but_nondefinitive_returns_none(self):
        # Closed but ~0.5/0.5 (e.g. a void) -> don't guess a winner.
        pred = {"side": "A", "opponent": "B", "token_id": "ta"}
        m = _resolved_market("c", ["ta", "tb"], ["A", "B"], ["0.5", "0.5"])
        self.assertIsNone(tr.winner_from_market(pred, m))

    def test_token_missing_falls_back_to_outcome_surname(self):
        # No usable token -> map the winning outcome name to side/opponent by surname.
        pred = {"side": "Xiaodi You", "opponent": "Leolia Jeanjean", "token_id": ""}
        m = _resolved_market("c", [], ["You X.", "Jeanjean L."], ["1", "0"])
        self.assertEqual(tr.winner_from_market(pred, m), "Xiaodi You")


class TestSettleFromMarket(unittest.TestCase):
    def _seed(self, db):
        # A WTA qualifying match no tour feed carries — only the market can settle it.
        tdb.record_prediction({
            "match_slug": "wta-you-jeanjea-2026-06-23", "match_date": "2026-06-23",
            "tour": "wta", "surface": "grass", "side": "Xiaodi You",
            "opponent": "Leolia Jeanjean", "condition_id": "c_you", "token_id": "tok_you",
            "entry_price": 0.45, "decimal_odds": 2.2, "model_prob": 0.55, "edge": 0.1,
            "confidence": 0.6, "size_pct": 0.02, "size_usd": 200.0, "kelly_fraction": 0.04,
            "used_external": 1, "fee_rate": 0.0, "strategy": "divergence",
            "market_url": "https://polymarket.com/x"}, db)

    def test_settles_qualifying_match_from_market(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "t.db")
            self._seed(db)
            api = _FakeGamma({"c_you": _resolved_market(
                "c_you", ["tok_you", "tok_jean"], ["Xiaodi You", "Leolia Jeanjean"], ["1", "0"])})
            out = tr.settle_pending_from_market(api, db)
            self.assertEqual(len(out["settled"]), 1)
            self.assertEqual(out["settled"][0]["status"], "ACERTO")
            self.assertEqual(len(tdb.get_predictions(db, status="PENDENTE")), 0)

    def test_settle_pending_uses_market_then_skips_feed(self):
        # With the market path resolving everything, the (expensive) feed must NOT be fetched.
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "t.db")
            self._seed(db)
            api = _FakeGamma({"c_you": _resolved_market(
                "c_you", ["tok_you", "tok_jean"], ["Xiaodi You", "Leolia Jeanjean"], ["0", "1"])})
            calls = {"n": 0}
            orig = ratings_source.fetch_matches
            ratings_source.fetch_matches = lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or []
            try:
                out = tr.settle_pending(db, api=api)
            finally:
                ratings_source.fetch_matches = orig
            self.assertEqual(len(out["settled"]), 1)
            self.assertEqual(out["settled"][0]["status"], "ERRO")    # opponent won
            self.assertEqual(calls["n"], 0)                          # feed never fetched
            self.assertEqual(len(tdb.get_predictions(db, status="PENDENTE")), 0)

    def test_market_unresolved_falls_through_to_feed(self):
        # Market still open -> market path settles nothing, feed path takes over.
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "t.db")
            tdb.record_prediction({
                "match_slug": "atp-alcaraz-sinner-2026-06-25", "match_date": "2026-06-25",
                "tour": "atp", "surface": "clay", "side": "Carlos Alcaraz",
                "opponent": "Jannik Sinner", "condition_id": "c_acs", "token_id": "tok_alc",
                "entry_price": 0.5, "decimal_odds": 2.0, "model_prob": 0.6, "edge": 0.1,
                "confidence": 0.6, "size_pct": 0.02, "size_usd": 200.0, "kelly_fraction": 0.04,
                "used_external": 1, "fee_rate": 0.0, "strategy": "x", "market_url": "u"}, db)
            api = _FakeGamma({"c_acs": {  # open market, no definitive price
                "conditionId": "c_acs", "closed": False, "umaResolutionStatus": "",
                "clobTokenIds": json.dumps(["tok_alc", "tok_sin"]),
                "outcomes": json.dumps(["Carlos Alcaraz", "Jannik Sinner"]),
                "outcomePrices": json.dumps(["0.55", "0.45"])}})
            orig = ratings_source.fetch_matches
            ratings_source.fetch_matches = lambda tour, years=None, debug=False: [
                {"date": "20260625", "surface": "clay", "winner": "Alcaraz C.",
                 "loser": "Sinner J."}]
            try:
                out = tr.settle_pending(db, api=api)
            finally:
                ratings_source.fetch_matches = orig
            self.assertEqual(len(out["settled"]), 1)                 # settled via the feed
            self.assertEqual(out["settled"][0]["status"], "ACERTO")
            self.assertEqual(len(tdb.get_predictions(db, status="PENDENTE")), 0)

    def test_settles_by_slug_when_condition_query_returns_nothing(self):
        # The live failure: the condition_ids query indexed 0 markets. The bet must still
        # settle via its slug (match_slug is the real Polymarket market slug).
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "t.db")
            self._seed(db)
            api = _FakeGamma(by_cid={}, by_slug={"wta-you-jeanjea-2026-06-23": _resolved_market(
                "c_you", ["tok_you", "tok_jean"], ["Xiaodi You", "Leolia Jeanjean"], ["1", "0"],
                slug="wta-you-jeanjea-2026-06-23")})
            out = tr.settle_pending_from_market(api, db)
            self.assertEqual(len(out["settled"]), 1)
            self.assertEqual(out["settled"][0]["status"], "ACERTO")
            self.assertGreaterEqual(api.slug_calls, 1)                  # used the slug fallback
            self.assertTrue(any("found by slug fallback" in x for x in out["diagnostics"]))

    def test_condition_query_requests_closed_markets(self):
        # A settled match's market is closed; Gamma's /markets defaults to active-only, so the
        # query MUST pass closed=true or resolved markets come back empty (the live failure).
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "t.db")
            self._seed(db)
            api = _FakeGamma({"c_you": _resolved_market(
                "c_you", ["tok_you", "tok_jean"], ["Xiaodi You", "Leolia Jeanjean"], ["1", "0"])})
            tr.settle_pending_from_market(api, db)
            self.assertTrue(any(p.get("closed") == "true" and "condition_ids" in p
                                for p in api.seen_params))

    def test_found_but_open_market_stays_pending_with_note(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "t.db")
            self._seed(db)
            open_m = {"conditionId": "c_you", "slug": "wta-you-jeanjea-2026-06-23",
                      "closed": False, "umaResolutionStatus": "",
                      "clobTokenIds": json.dumps(["tok_you", "tok_jean"]),
                      "outcomes": json.dumps(["Xiaodi You", "Leolia Jeanjean"]),
                      "outcomePrices": json.dumps(["0.7", "0.3"])}
            api = _FakeGamma(by_cid={"c_you": open_m})
            out = tr.settle_pending_from_market(api, db)
            self.assertEqual(out["settled"], [])
            self.assertEqual(len(tdb.get_predictions(db, status="PENDENTE")), 1)
            self.assertTrue(any("still open" in x for x in out["diagnostics"]))

    def test_offline_market_noop_keeps_pending(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "t.db")
            self._seed(db)

            class _Dead:
                def get(self, *a, **k):
                    raise RuntimeError("offline")
            out = tr.settle_pending_from_market(_Dead(), db)
            self.assertEqual(out["settled"], [])
            self.assertEqual(len(tdb.get_predictions(db, status="PENDENTE")), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
