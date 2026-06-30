#!/usr/bin/env python3
"""Offline tests for the shared per-wallet filter predicate (used by both the watcher's
forwarding and the wallet's Resultados)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wallet_filters as wf  # noqa: E402


def _bet(cat, sub, conf, status="WON"):
    return {"category": cat, "subcategory": sub, "confidence": conf, "status": status}


class TestPassesFilter(unittest.TestCase):
    def test_none_passes_all(self):
        self.assertTrue(wf.passes_filter(None, "Soccer", "Over/Under gols", "Alta"))

    def test_empty_dict_passes_nothing(self):
        self.assertFalse(wf.passes_filter({}, "Soccer", "Over/Under gols", "Alta"))

    def test_category_subcategory_and_confidence_gate(self):
        f = {"Soccer": {"Over/Under gols": ["Alta", "Média"]}}
        self.assertTrue(wf.passes_filter(f, "Soccer", "Over/Under gols", "Alta"))
        self.assertTrue(wf.passes_filter(f, "Soccer", "Over/Under gols", "Média"))
        self.assertFalse(wf.passes_filter(f, "Soccer", "Over/Under gols", "Baixa"))   # conf off
        self.assertFalse(wf.passes_filter(f, "Soccer", "Ambas Marcam", "Alta"))       # sub off
        self.assertFalse(wf.passes_filter(f, "Tennis", "Over/Under gols", "Alta"))    # cat off


class TestFilterBets(unittest.TestCase):
    def _bets(self):
        return [
            _bet("Soccer", "Over/Under gols", "Alta"),
            _bet("Soccer", "Over/Under gols", "Média"),
            _bet("Soccer", "Ambas Marcam", "Alta"),
            _bet("Tennis", "Vencedor da partida", "Alta"),
        ]

    def test_none_keeps_all(self):
        self.assertEqual(len(wf.filter_bets(None, self._bets())), 4)

    def test_empty_keeps_none(self):
        self.assertEqual(wf.filter_bets({}, self._bets()), [])

    def test_subset_keeps_only_matching_triples(self):
        f = {"Soccer": {"Over/Under gols": ["Alta"]}}
        kept = wf.filter_bets(f, self._bets())
        self.assertEqual(len(kept), 1)
        self.assertEqual((kept[0]["category"], kept[0]["subcategory"], kept[0]["confidence"]),
                         ("Soccer", "Over/Under gols", "Alta"))

    def test_handles_empty_and_missing_fields(self):
        # rows with no category never match a non-null filter (and don't crash)
        rows = [{"status": "WON"}, _bet("Soccer", "Over/Under gols", "Alta")]
        self.assertEqual(len(wf.filter_bets({"Soccer": {"Over/Under gols": ["Alta"]}}, rows)), 1)
        self.assertEqual(wf.filter_bets(None, None), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
