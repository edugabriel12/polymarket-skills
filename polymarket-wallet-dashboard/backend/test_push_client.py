#!/usr/bin/env python3
"""Offline tests for the push client (no network — injected fake HTTP client)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import push_client as pc  # noqa: E402
import entries as en  # noqa: E402


class _Resp:
    def raise_for_status(self):
        pass


class _FakeClient:
    def __init__(self):
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return _Resp()


class _DeadClient:
    def post(self, *a, **k):
        raise RuntimeError("connection refused")


def _entry(source="model"):
    return en.make_entry(key="k1", event="ARS vs CHE", category="Soccer",
                         subcategory="Over/Under gols", side="OVER", odds=1.79,
                         entry_price=0.56, unit=1.0, confidence="Alta", live=en.PRELIVE,
                         source=source)


class TestPush(unittest.TestCase):
    def test_sends_public_view_with_token(self):
        c = _FakeClient()
        out = pc.push_entries([_entry()], base_url="http://sports:8000", token="secret", client=c)
        self.assertEqual(out["sent"], 1)
        self.assertEqual(len(c.calls), 1)
        call = c.calls[0]
        self.assertEqual(call["url"], "http://sports:8000/api/copy/ingest")
        self.assertEqual(call["headers"]["Authorization"], "Bearer secret")
        # source stripped — no model/wallet origin reaches Sports
        sent = call["json"]["entries"][0]
        self.assertNotIn("source", sent)
        self.assertEqual(sent["unit"], 1.0)
        self.assertEqual(sent["live"], en.PRELIVE)

    def test_no_base_url_skips(self):
        out = pc.push_entries([_entry()], base_url="", token="x", client=_FakeClient())
        self.assertEqual(out["sent"], 0)
        self.assertIn("error", out)

    def test_empty_is_noop(self):
        out = pc.push_entries([], base_url="http://x", token="t", client=_FakeClient())
        self.assertEqual(out, {"sent": 0, "skipped": 0})

    def test_failure_is_swallowed(self):
        out = pc.push_entries([_entry()], base_url="http://x", token="t", client=_DeadClient())
        self.assertEqual(out["sent"], 0)
        self.assertEqual(out["skipped"], 1)
        self.assertIn("error", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
