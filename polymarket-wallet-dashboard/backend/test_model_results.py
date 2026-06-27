#!/usr/bin/env python3
"""Offline tests for the model-results aggregation."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import model_results as mr  # noqa: E402


class TestAggregate(unittest.TestCase):
    def test_pnl_winrate_roi(self):
        rows = [
            {"status": "ACERTO", "size_usd": 100.0, "entry_price": 0.5},   # +100
            {"status": "ERRO", "size_usd": 100.0, "entry_price": 0.5},     # -100
            {"status": "ACERTO", "size_usd": 50.0, "entry_price": 0.25},   # +150
            {"status": "PENDENTE", "size_usd": 999.0, "entry_price": 0.5}, # excluded
            {"status": "ANULADO", "size_usd": 999.0, "entry_price": 0.5},  # excluded
        ]
        a = mr.aggregate(rows, "Futebol")
        self.assertEqual(a["category"], "Futebol")
        self.assertEqual(a["n_bets"], 3)
        self.assertEqual(a["wins"], 2)
        self.assertEqual(a["losses"], 1)
        self.assertAlmostEqual(a["win_rate"], 2 / 3, places=4)
        self.assertAlmostEqual(a["invested"], 250.0)
        self.assertAlmostEqual(a["total_pnl"], 150.0)       # +100 -100 +150
        self.assertAlmostEqual(a["roi"], 150.0 / 250.0, places=4)

    def test_empty(self):
        a = mr.aggregate([], "Tênis")
        self.assertEqual(a["n_bets"], 0)
        self.assertIsNone(a["win_rate"])
        self.assertIsNone(a["roi"])

    def test_model_results_shape(self):
        out = mr.model_results()
        self.assertEqual(out["entity"], "Modelo")
        self.assertIn("by_category", out)
        self.assertIsNone(out["by_confidence"])   # Modelo has no confidence axis


class TestModelBets(unittest.TestCase):
    def test_row_to_bet_maps_status_and_subcategory(self):
        b = mr._row_to_bet(
            {"game_slug": "epl-ars-che-2026-06-25-total-2pt5", "side": "OVER",
             "entry_price": 0.56, "decimal_odds": 1.79, "status": "ACERTO",
             "settled_at": "2026-06-25", "market_url": "u"}, "Futebol", "Soccer")
        self.assertEqual(b["status"], "WON")
        self.assertEqual(b["subcategory"], "Over/Under gols")
        self.assertEqual(b["event"], "ARS vs CHE")
        self.assertEqual(b["unit"], 1.0)
        self.assertAlmostEqual(b["odds"], 1.79)

    def test_model_bets_unknown_category_empty(self):
        out = mr.model_bets("Basketball", 0, 20)   # model only does Futebol/Tênis
        self.assertEqual(out, {"total": 0, "bets": []})


class TestModelOpenBets(unittest.TestCase):
    def test_row_to_open_bet_shape(self):
        b = mr._row_to_open_bet(
            {"game_slug": "epl-ars-che-2026-06-26-total-2pt5", "side": "OVER",
             "entry_price": 0.5, "decimal_odds": 2.0, "status": "PENDENTE",
             "updated_at": "2026-06-26", "market_url": "u"}, "Futebol", "Soccer")
        self.assertEqual(b["status"], "OPEN")     # pending, not settled
        self.assertIsNone(b["pnl"])               # no result yet
        self.assertEqual(b["live"], "PRÉ-LIVE")
        self.assertEqual(b["subcategory"], "Over/Under gols")
        self.assertEqual(b["event"], "ARS vs CHE")
        self.assertEqual(b["unit"], 1.0)
        self.assertAlmostEqual(b["odds"], 2.0)

    def test_model_open_bets_unknown_category_empty(self):
        out = mr.model_open_bets("Basketball", 0, 20)   # model only does Futebol/Tênis
        self.assertEqual(out, {"total": 0, "bets": []})


class TestSettlePendingModels(unittest.TestCase):
    """Liquidação on-demand: orquestra soccer+tennis e isola a falha de um esporte."""

    def test_best_effort_isolates_failures(self):
        import soccer_results
        import tennis_results
        orig_s, orig_t = soccer_results.settle_pending, tennis_results.settle_pending
        soccer_results.settle_pending = lambda *a, **k: {"games_matched": 2, "settled": [1, 2]}
        tennis_results.settle_pending = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            out = mr.settle_pending_models()                  # não deve lançar
        finally:
            soccer_results.settle_pending = orig_s
            tennis_results.settle_pending = orig_t
        self.assertEqual(out["soccer"]["settled_rows"], 2)    # soccer liquidou
        self.assertNotIn("tennis", out)                       # tennis falhou, mas isolado

    def test_model_results_triggers_settle(self):
        calls = []
        orig = mr.settle_pending_models
        mr.settle_pending_models = lambda: calls.append(1)
        try:
            mr.model_results()                                # abrir Resultados deve liquidar antes
        finally:
            mr.settle_pending_models = orig
        self.assertEqual(len(calls), 1)

    def test_model_open_bets_settles_only_on_first_page(self):
        calls = []
        orig = mr.settle_pending_models
        mr.settle_pending_models = lambda: calls.append(1)
        try:
            mr.model_open_bets(None, 0, 20)                   # abrir Pendentes (1ª página) -> liquida
            mr.model_open_bets(None, 20, 20)                  # página 2 -> NÃO liquida de novo
        finally:
            mr.settle_pending_models = orig
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
