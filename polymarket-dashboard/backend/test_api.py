#!/usr/bin/env python3
"""Offline tests for Polymarket Sports — the storefront (ingest → entries/results/Telegram)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp()
os.environ["SPORTS_ENTRIES_DB"] = os.path.join(_TMP, "entries.db")
os.environ.pop("TELEGRAM_BOT_TOKEN", None)   # keep Telegram unconfigured (no network) in tests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fastapi.testclient import TestClient  # noqa: E402
import app as backend  # noqa: E402
import entries_store as es  # noqa: E402
import results_combined as rc  # noqa: E402
import telegram_notify as tg  # noqa: E402

client = TestClient(backend.app)


def _entry(key, *, category="Soccer", subcategory="Over/Under gols", side="OVER",
           odds=1.8, unit=1.0, confidence="Alta", live="PRÉ-LIVE", status="OPEN", pnl=None,
           event="ARS vs CHE"):
    return {"key": key, "event": event, "category": category, "subcategory": subcategory,
            "side": side, "odds": odds, "entry_price": round(1 / odds, 4), "unit": unit,
            "confidence": confidence, "live": live, "market_url": "https://polymarket.com/x",
            "game_start": None, "status": status, "pnl": pnl}


def _reset():
    con = es.connect()
    with con:
        con.execute("DELETE FROM entries")
    con.close()


class TestIngestAndEntries(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_new_then_cards_by_category(self):
        r = client.post("/api/copy/ingest", json={"entries": [
            _entry("k1"), _entry("k2", category="Baseball", subcategory="Moneyline")]})
        body = r.json()
        self.assertEqual(body["ingested"], 2)
        self.assertEqual(body["new"], 2)
        cards = client.get("/api/entries").json()
        cats = {c["category"] for c in cards["categories"]}
        self.assertIn("Soccer", cats)
        self.assertIn("Baseball", cats)
        self.assertEqual(cards["n_open"], 2)

    def test_upgrade_then_settle(self):
        client.post("/api/copy/ingest", json={"entries": [_entry("u1", unit=0.5, confidence="Média")]})
        up = client.post("/api/copy/ingest", json={"entries": [
            _entry("u1", unit=1.0, confidence="Alta")]}).json()
        self.assertEqual(up["upgrade"], 1)
        # same unit again -> unchanged
        same = client.post("/api/copy/ingest", json={"entries": [
            _entry("u1", unit=1.0, confidence="Alta")]}).json()
        self.assertEqual(same["unchanged"], 1)
        # settle it
        st = client.post("/api/copy/ingest", json={"entries": [
            _entry("u1", unit=1.0, status="WON", pnl=80.0)]}).json()
        self.assertEqual(st["settled"], 1)
        # no longer open; shows in results
        self.assertNotIn("u1", [e["key"] for c in client.get("/api/entries").json()["categories"]
                                for e in c["entries"]])
        res = client.get("/api/results").json()
        self.assertGreaterEqual(res["overall"]["wins"], 1)


class TestAuth(unittest.TestCase):
    def test_token_required_when_set(self):
        backend.COPY_INGEST_TOKEN = "secret"
        try:
            bad = client.post("/api/copy/ingest", json={"entries": [_entry("a1")]})
            self.assertEqual(bad.json().get("error"), "unauthorized")
            ok = client.post("/api/copy/ingest", json={"entries": [_entry("a1")]},
                             headers={"Authorization": "Bearer secret"})
            self.assertEqual(ok.json()["ingested"], 1)
        finally:
            backend.COPY_INGEST_TOKEN = ""


class TestResultsUnits(unittest.TestCase):
    def test_unit_based_metrics(self):
        # 1U won at 1.8 → +0.8U ; 1U lost → −1U  ⇒ pnl=−0.2U over 2U staked, win_rate 0.5
        entries = [_entry("w", unit=1.0, odds=1.8, status="WON"),
                   _entry("l", unit=1.0, odds=2.0, status="LOST")]
        out = rc.combined(entries)
        self.assertEqual(out["overall"]["wins"], 1)
        self.assertEqual(out["overall"]["losses"], 1)
        self.assertAlmostEqual(out["overall"]["pnl_u"], -0.2, places=3)
        self.assertAlmostEqual(out["overall"]["staked_u"], 2.0, places=3)
        self.assertAlmostEqual(out["overall"]["win_rate"], 0.5, places=3)
        self.assertAlmostEqual(out["overall"]["roi"], -0.1, places=3)
        # by_unit present with the 1U bucket
        labels = {b["unit_label"] for b in out["by_unit"]}
        self.assertIn("1U", labels)


class TestTelegramFormat(unittest.TestCase):
    def test_format_no_wallet_no_position(self):
        msg = tg.format_entry(_entry("k", live="PRÉ-LIVE"))
        self.assertIn("1U", msg)
        self.assertIn("PRÉ-LIVE", msg)
        self.assertIn("ARS vs CHE", msg)
        self.assertNotIn("0x", msg)        # no wallet
        self.assertNotIn("$", msg)         # no position size

    def test_send_uses_bot_api(self):
        class _Resp:
            def raise_for_status(self): pass

        class _Fake:
            def __init__(self): self.calls = []
            def post(self, url, json=None, timeout=None):
                self.calls.append((url, json)); return _Resp()
        c = _Fake()
        ok = tg.send("hi", token="T", chat_id="C", client=c)
        self.assertTrue(ok)
        self.assertIn("/botT/sendMessage", c.calls[0][0])
        self.assertEqual(c.calls[0][1]["chat_id"], "C")

    def test_send_unconfigured_skips(self):
        self.assertFalse(tg.send("hi", token="", chat_id=""))


class TestHealth(unittest.TestCase):
    def test_health(self):
        b = client.get("/api/health").json()
        self.assertEqual(b["status"], "ok")
        self.assertIn("telegram", b)


if __name__ == "__main__":
    unittest.main(verbosity=2)
