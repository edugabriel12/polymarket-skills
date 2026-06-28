#!/usr/bin/env python3
"""Offline tests for the watcher detection (synthetic positions, no network)."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import watcher as w  # noqa: E402
import wallets_store as ws  # noqa: E402
import entries as en  # noqa: E402

# A wallet with clean per-tier floors (Oneger-like).
WALLET = {
    "id": 1, "name": "Oneger", "address": "0x" + "ab" * 20,
    "thresholds": {
        "Alta": {"floor": 40000, "unit": 1.0}, "Média": {"floor": 15000, "unit": 0.5},
        "Baixa": {"floor": 1000, "unit": 0.25},
    },
}


# Same wallet but only forwarding Soccer "Over/Under gols" at Alta confidence.
FILTERED = {**WALLET, "filters": {"Soccer": {"Over/Under gols": ["Alta"]}}}


def _pos(cond, invested, *, title="Arsenal vs. Chelsea", outcome="OVER",
         avg=0.56, cur=0.56, cashPnl=0.0, redeemable=False, endDate=None,
         slug="epl-ars-che-2026-06-25-total-2pt5"):
    return {"conditionId": cond, "initialValue": invested, "title": title,
            "outcome": outcome, "avgPrice": avg, "curPrice": cur, "cashPnl": cashPnl,
            "redeemable": redeemable, "endDate": endDate, "slug": slug}


class TestDetectEntries(unittest.TestCase):
    def test_below_floor_no_entry(self):
        ents, persist = w.detect_entries(WALLET, [_pos("c1", 300)], {})
        self.assertEqual(ents, [])
        self.assertEqual(persist, [])

    def test_crossing_emits_with_unit_and_category(self):
        ents, persist = w.detect_entries(WALLET, [_pos("c1", 16000)], {})
        self.assertEqual(len(ents), 1)
        e = ents[0]
        self.assertEqual(e["confidence"], "Média")
        self.assertEqual(e["unit"], 0.5)
        self.assertEqual(e["category"], "Soccer")
        self.assertEqual(e["subcategory"], "Over/Under gols")
        self.assertEqual(e["side"], "OVER")
        self.assertEqual(persist, [("c1", "Média")])

    def test_same_tier_not_repeated(self):
        ents, _ = w.detect_entries(WALLET, [_pos("c1", 16000)], {"c1": "Média"})
        self.assertEqual(ents, [])

    def test_upgrade_re_emits(self):
        # already alerted Média; position grew into Alta -> re-emit
        ents, persist = w.detect_entries(WALLET, [_pos("c1", 45000)], {"c1": "Média"})
        self.assertEqual(len(ents), 1)
        self.assertEqual(ents[0]["confidence"], "Alta")
        self.assertEqual(ents[0]["unit"], 1.0)
        self.assertEqual(persist, [("c1", "Alta")])

    def test_no_downgrade(self):
        # was Alta; a (spurious) smaller reading must NOT downgrade-alert
        ents, _ = w.detect_entries(WALLET, [_pos("c1", 16000)], {"c1": "Alta"})
        self.assertEqual(ents, [])

    def test_one_entry_per_market_key(self):
        e = w.detect_entries(WALLET, [_pos("c1", 45000)], {})[0][0]
        # key is per (wallet, market) — independent of tier
        self.assertEqual(e["key"], en.make_key(WALLET["address"], "c1"))


class TestDetectSettlements(unittest.TestCase):
    def test_resolved_alerted_market_settles_won(self):
        seen = {"c1": "Alta"}
        pos = _pos("c1", 45000, redeemable=True, cashPnl=120.0)
        ents, persist = w.detect_settlements(WALLET, [pos], set(), seen)
        self.assertEqual(len(ents), 1)
        self.assertEqual(ents[0]["status"], "WON")
        self.assertEqual(ents[0]["pnl"], 120.0)
        self.assertEqual(ents[0]["unit"], 1.0)
        self.assertEqual(persist, ["c1"])

    def test_lost_and_void(self):
        seen = {"c1": "Baixa", "c2": "Média"}
        lost = _pos("c1", 2000, redeemable=True, cashPnl=-50.0)
        void = _pos("c2", 16000, redeemable=True, cashPnl=0.0)
        ents, _ = w.detect_settlements(WALLET, [lost, void], set(), seen)
        by = {e["status"] for e in ents}
        self.assertEqual(by, {"LOST", "VOID"})

    def test_unresolved_or_unalerted_skipped(self):
        seen = {"c1": "Alta"}
        open_pos = _pos("c1", 45000, redeemable=False, cur=0.6)        # not resolved
        other = _pos("c9", 45000, redeemable=True, cashPnl=10.0)       # never alerted
        ents, _ = w.detect_settlements(WALLET, [open_pos, other], set(), seen)
        self.assertEqual(ents, [])

    def test_already_settled_skipped(self):
        seen = {"c1": "Alta"}
        pos = _pos("c1", 45000, redeemable=True, cashPnl=120.0)
        ents, _ = w.detect_settlements(WALLET, [pos], {"c1"}, seen)
        self.assertEqual(ents, [])


class TestPollWalletPersists(unittest.TestCase):
    def test_poll_persists_and_dedups(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "w.db")
            ws.add_wallet(WALLET["name"], WALLET["address"], {"overall": {}, "n_markets": 0},
                          WALLET["thresholds"], db_path=db)
            wid = ws.list_wallets(db)[0]["id"]
            wallet = ws.get_wallet(wid, db)

            class _Api:  # returns the wallet's positions
                def __init__(self, positions):
                    self._p = positions

            captured = {}

            def fake_fetch(api, addr):
                return api._p
            orig = w.wa.fetch_positions
            w.wa.fetch_positions = fake_fetch
            try:
                # first poll only snapshots the baseline (wallet held nothing) -> emits nothing
                self.assertEqual(w.poll_wallet(_Api([]), wallet, db), [])
                self.assertTrue(ws.baseline_established(wid, db))
                # a NEW $45k market opens after add -> Alta entry
                ents = w.poll_wallet(_Api([_pos("c1", 45000)]), wallet, db)
                self.assertEqual(len(ents), 1)
                self.assertEqual(ents[0]["confidence"], "Alta")
                # second poll, same position -> no repeat (persisted)
                ents2 = w.poll_wallet(_Api([_pos("c1", 45000)]), wallet, db)
                self.assertEqual(ents2, [])
                # the position resolves -> settlement pushed once
                ents3 = w.poll_wallet(_Api([_pos("c1", 45000, redeemable=True, cashPnl=99.0)]),
                                      wallet, db)
                self.assertEqual(len(ents3), 1)
                self.assertEqual(ents3[0]["status"], "WON")
                ents4 = w.poll_wallet(_Api([_pos("c1", 45000, redeemable=True, cashPnl=99.0)]),
                                      wallet, db)
                self.assertEqual(ents4, [])
            finally:
                w.wa.fetch_positions = orig
            _ = captured


class TestPersistBets(unittest.TestCase):
    def test_persist_open_then_settled(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "w.db")
            ws.add_wallet(WALLET["name"], WALLET["address"], {"n_markets": 0},
                          WALLET["thresholds"], db_path=db)
            wallet = ws.get_wallet(ws.list_wallets(db)[0]["id"], db)
            # open at Alta -> wallet_bets row OPEN
            w.persist_bets(wallet, [_pos("c1", 45000)], db)
            bets = ws.list_bets(wallet["id"], db)
            self.assertEqual(len(bets), 1)
            self.assertEqual(bets[0]["status"], "OPEN")
            self.assertEqual(bets[0]["confidence"], "Alta")
            self.assertAlmostEqual(bets[0]["total_position"], 45000.0)
            # resolves -> same row updated to WON with pnl
            w.persist_bets(wallet, [_pos("c1", 45000, redeemable=True, cashPnl=120.0)], db)
            bets = ws.list_bets(wallet["id"], db)
            self.assertEqual(len(bets), 1)                       # upsert, not a new row
            self.assertEqual(bets[0]["status"], "WON")
            self.assertAlmostEqual(bets[0]["pnl"], 120.0)
            # below-floor positions are not tracked
            w.persist_bets(wallet, [_pos("c2", 300)], db)
            self.assertEqual(len(ws.list_bets(wallet["id"], db)), 1)

    def test_settled_bets_pagination_and_fields(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "w.db")
            ws.add_wallet(WALLET["name"], WALLET["address"], {"n_markets": 0},
                          WALLET["thresholds"], db_path=db)
            wallet = ws.get_wallet(ws.list_wallets(db)[0]["id"], db)
            wid = wallet["id"]
            # 3 settled soccer + 1 open
            for i in range(3):
                w.persist_bets(wallet, [_pos(f"c{i}", 45000, redeemable=True, cashPnl=10.0)], db)
            w.persist_bets(wallet, [_pos("open", 45000)], db)   # OPEN -> not in settled
            self.assertEqual(ws.count_settled_bets(wid, "Soccer", db), 3)
            self.assertEqual(ws.count_settled_bets(wid, None, db), 3)
            page = ws.list_settled_bets(wid, "Soccer", 0, 2, db)
            self.assertEqual(len(page), 2)
            self.assertTrue(all(b["event"] and b["market_url"] for b in page))   # event/url stored

    def test_open_bets_pagination_and_fields(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "w.db")
            ws.add_wallet(WALLET["name"], WALLET["address"], {"n_markets": 0},
                          WALLET["thresholds"], db_path=db)
            wallet = ws.get_wallet(ws.list_wallets(db)[0]["id"], db)
            wid = wallet["id"]
            # 3 open soccer + 1 settled (settled must NOT appear in open)
            for i in range(3):
                w.persist_bets(wallet, [_pos(f"o{i}", 45000)], db)                       # OPEN
            w.persist_bets(wallet, [_pos("done", 45000, redeemable=True, cashPnl=10.0)], db)  # settled
            self.assertEqual(ws.count_open_bets(wid, "Soccer", db), 3)
            self.assertEqual(ws.count_open_bets(wid, None, db), 3)
            page = ws.list_open_bets(wid, "Soccer", 0, 2, db)
            self.assertEqual(len(page), 2)
            self.assertTrue(all(b["status"] == "OPEN" for b in page))
            self.assertTrue(all(b["event"] and b["market_url"] for b in page))   # event/url stored


class TestPassesFilter(unittest.TestCase):
    def test_none_forwards_all(self):
        self.assertTrue(w.passes_filter(None, "Soccer", "Over/Under gols", "Alta"))

    def test_empty_dict_forwards_nothing(self):
        # explicit {} = "forward nothing" (distinct from None = no restriction)
        self.assertFalse(w.passes_filter({}, "Soccer", "Over/Under gols", "Alta"))

    def test_category_not_selected(self):
        f = {"Tennis": {"Vencedor da partida": ["Alta"]}}
        self.assertFalse(w.passes_filter(f, "Soccer", "Over/Under gols", "Alta"))

    def test_subcategory_not_selected(self):
        f = {"Soccer": {"Ambas Marcam": ["Alta"]}}
        self.assertFalse(w.passes_filter(f, "Soccer", "Over/Under gols", "Alta"))

    def test_confidence_not_listed(self):
        f = {"Soccer": {"Over/Under gols": ["Alta"]}}
        self.assertFalse(w.passes_filter(f, "Soccer", "Over/Under gols", "Média"))

    def test_exact_match(self):
        f = {"Soccer": {"Over/Under gols": ["Alta", "Média"]}}
        self.assertTrue(w.passes_filter(f, "Soccer", "Over/Under gols", "Média"))


class TestDetectEntriesFilter(unittest.TestCase):
    def test_media_dropped_when_only_alta_selected(self):
        # $16k -> Média; filter forwards only Alta -> nothing emitted AND nothing persisted
        # (so the market never enters seen_alerts and can never settle).
        ents, persist = w.detect_entries(FILTERED, [_pos("c1", 16000)], {})
        self.assertEqual(ents, [])
        self.assertEqual(persist, [])

    def test_alta_forwarded(self):
        ents, persist = w.detect_entries(FILTERED, [_pos("c1", 45000)], {})
        self.assertEqual(len(ents), 1)
        self.assertEqual(ents[0]["confidence"], "Alta")
        self.assertEqual(persist, [("c1", "Alta")])

    def test_unfiltered_wallet_unchanged(self):
        # WALLET has no "filters" key -> forward everything (legacy behavior preserved).
        ents, _ = w.detect_entries(WALLET, [_pos("c1", 16000)], {})
        self.assertEqual(len(ents), 1)


class TestFilterSettleConsistency(unittest.TestCase):
    def test_filtered_market_never_settles(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "w.db")
            ws.add_wallet(WALLET["name"], WALLET["address"], {"n_markets": 0},
                          WALLET["thresholds"],
                          filters={"Soccer": {"Over/Under gols": ["Alta"]}}, db_path=db)
            wallet = ws.get_wallet(ws.list_wallets(db)[0]["id"], db)
            orig = w.wa.fetch_positions
            w.wa.fetch_positions = lambda api, addr: api          # `api` is the positions list
            try:
                # first poll snapshots an (empty) baseline
                self.assertEqual(w.poll_wallet([], wallet, db), [])
                # a NEW Média position opens -> filtered out -> not alerted
                self.assertEqual(w.poll_wallet([_pos("c1", 16000)], wallet, db), [])
                # It resolves: because it was never alerted, no orphan settle is emitted
                resolved = [_pos("c1", 16000, redeemable=True, cashPnl=50.0)]
                self.assertEqual(w.poll_wallet(resolved, wallet, db), [])
            finally:
                w.wa.fetch_positions = orig


class TestBaseline(unittest.TestCase):
    """The first poll snapshots a wallet's pre-existing holdings (open OR already settled) and
    ignores them forever — only positions opened AFTER the wallet is added are tracked."""

    def setUp(self):
        self._orig = w.wa.fetch_positions
        w.wa.fetch_positions = lambda api, addr: api      # `api` is the positions list

    def tearDown(self):
        w.wa.fetch_positions = self._orig

    def _new_wallet(self, d):
        db = os.path.join(d, "w.db")
        ws.add_wallet(WALLET["name"], WALLET["address"], {"n_markets": 0},
                      WALLET["thresholds"], db_path=db)
        wid = ws.list_wallets(db)[0]["id"]
        return db, wid, ws.get_wallet(wid, db)

    def test_pre_existing_holdings_ignored_forever(self):
        with tempfile.TemporaryDirectory() as d:
            db, wid, wallet = self._new_wallet(d)
            pre = [_pos("old_open", 45000),                                   # open at add
                   _pos("old_done", 45000, redeemable=True, cashPnl=120.0)]   # ALREADY settled
            # first poll: baseline only — nothing emitted, nothing persisted
            self.assertEqual(w.poll_wallet(pre, wallet, db), [])
            self.assertTrue(ws.baseline_established(wid, db))
            self.assertEqual(ws.list_bets(wid, db), [])                       # no pre-add bets
            self.assertEqual(ws.baseline_markets(wid, db), {"old_open", "old_done"})
            # the open one later resolves -> STILL ignored (it pre-dated watching)
            later = [_pos("old_open", 45000, redeemable=True, cashPnl=77.0),
                     _pos("old_done", 45000, redeemable=True, cashPnl=120.0)]
            self.assertEqual(w.poll_wallet(later, wallet, db), [])
            self.assertEqual(ws.list_bets(wid, db), [])

    def test_new_position_after_baseline_is_tracked(self):
        with tempfile.TemporaryDirectory() as d:
            db, wid, wallet = self._new_wallet(d)
            self.assertEqual(w.poll_wallet([_pos("old", 45000)], wallet, db), [])  # baseline
            ents = w.poll_wallet([_pos("old", 45000), _pos("new", 45000)], wallet, db)
            self.assertEqual(len(ents), 1)
            self.assertEqual(ents[0]["key"], en.make_key(WALLET["address"], "new"))
            self.assertEqual({b["condition_id"] for b in ws.list_bets(wid, db)}, {"new"})

    def test_empty_first_poll_then_first_position_is_new(self):
        # cold start: wallet holds nothing at add -> empty baseline -> first position counts
        with tempfile.TemporaryDirectory() as d:
            db, wid, wallet = self._new_wallet(d)
            self.assertEqual(w.poll_wallet([], wallet, db), [])
            self.assertTrue(ws.baseline_established(wid, db))
            ents = w.poll_wallet([_pos("c1", 45000)], wallet, db)
            self.assertEqual(len(ents), 1)
            self.assertEqual(ents[0]["confidence"], "Alta")

    def test_reset_tracking_reseeds_baseline(self):
        with tempfile.TemporaryDirectory() as d:
            db, wid, wallet = self._new_wallet(d)
            w.poll_wallet([], wallet, db)                          # baseline (empty)
            w.poll_wallet([_pos("c1", 45000)], wallet, db)        # c1 tracked
            self.assertTrue(ws.list_bets(wid, db))
            removed = ws.reset_tracking(wid, db)
            self.assertGreaterEqual(removed["wallet_bets"], 1)
            self.assertFalse(ws.baseline_established(wid, db))     # must re-baseline
            self.assertEqual(ws.list_bets(wid, db), [])
            # the wallet config is untouched by a reset
            self.assertEqual(ws.get_wallet(wid, db)["name"], WALLET["name"])
            # current holdings become the NEW baseline -> ignored from now on
            self.assertEqual(w.poll_wallet([_pos("c1", 45000)], wallet, db), [])
            self.assertEqual(ws.list_bets(wid, db), [])


class TestTagCategory(unittest.TestCase):
    """Tags-first resolver: Polymarket's own category (Gamma tags) wins, cached per event,
    with the keyword/structural classifier as the fallback."""

    def _patch_tags(self, tags):
        calls = []
        self._orig = w.wa.fetch_event_tags
        w.wa.fetch_event_tags = lambda api, slug: (calls.append(slug), tags)[1]
        return calls

    def tearDown(self):
        if hasattr(self, "_orig"):
            w.wa.fetch_event_tags = self._orig

    def test_resolve_prefers_specific_over_generic_and_caches(self):
        calls = self._patch_tags(["sports", "tennis"])     # generic + specific -> specific wins
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "w.db")
            self.assertEqual(w.resolve_category(object(), "atp-foo", db), "Tennis")
            self.assertEqual(w.resolve_category(object(), "atp-foo", db), "Tennis")   # cached
            self.assertEqual(len(calls), 1)                # fetched once, then served from cache

    def test_miss_is_cached_not_refetched(self):
        calls = self._patch_tags([])                       # no mapped tag
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "w.db")
            self.assertIsNone(w.resolve_category(object(), "mystery", db))
            self.assertIsNone(w.resolve_category(object(), "mystery", db))
            self.assertEqual(len(calls), 1)                # known miss cached -> keyword fallback

    def test_no_event_slug_no_network(self):
        calls = self._patch_tags(["tennis"])
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(w.resolve_category(object(), None, os.path.join(d, "w.db")))
        self.assertEqual(calls, [])                         # no slug -> never calls Gamma

    def test_market_fields_prefers_tag_category(self):
        pos = _pos("c1", 45000, title="Felix Balshaw vs Martin Krumich", slug="atp-targu-mures")
        pos["_category"] = "Tennis"                         # as stamped by _enrich_categories
        f = w._market_fields(pos)
        self.assertEqual(f["category"], "Tennis")
        self.assertEqual(f["subcategory"], "Vencedor da partida")

    def test_market_fields_falls_back_to_keyword(self):
        f = w._market_fields(_pos("c1", 45000))            # default slug epl-… -> Soccer
        self.assertEqual(f["category"], "Soccer")


if __name__ == "__main__":
    unittest.main(verbosity=2)
