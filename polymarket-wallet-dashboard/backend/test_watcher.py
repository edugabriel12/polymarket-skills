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
                # first poll: a $45k market -> Alta entry
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
