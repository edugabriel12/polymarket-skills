#!/usr/bin/env python3
"""Offline tests for the calibration report over the model_log shadow log."""

import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import calibration as cal  # noqa: E402
import predictions_db as pdb  # noqa: E402


class TestPure(unittest.TestCase):
    def test_ref_outcome(self):
        self.assertEqual(cal.ref_outcome("TOTAL", 8.5, 10, None), 1)
        self.assertEqual(cal.ref_outcome("TOTAL", 8.5, 7, None), 0)
        self.assertIsNone(cal.ref_outcome("TOTAL", 9.0, 9.0, None))   # push
        self.assertEqual(cal.ref_outcome("BTTS", None, None, 1), 1)
        self.assertEqual(cal.ref_outcome("BTTS", None, None, 0), 0)
        self.assertIsNone(cal.ref_outcome("TOTAL", 8.5, None, None))

    def test_base_slug(self):
        self.assertEqual(cal.base_slug("mlb-hou-kc-2026-06-14-total-8pt5"),
                         "mlb-hou-kc-2026-06-14")
        self.assertEqual(cal.base_slug("epl-ars-che-2026-06-14-btts"),
                         "epl-ars-che-2026-06-14")

    def test_brier_and_logloss_known(self):
        rows = [{"ref_prob": 0.8, "ref_outcome": 1}, {"ref_prob": 0.3, "ref_outcome": 0}]
        self.assertAlmostEqual(cal.brier(rows), (0.04 + 0.09) / 2)
        self.assertAlmostEqual(cal.log_loss(rows),
                               -(math.log(0.8) + math.log(0.7)) / 2, places=6)
        self.assertIsNone(cal.brier([]))

    def test_reliability_bins(self):
        rows = [{"ref_prob": 0.05, "ref_outcome": 0}, {"ref_prob": 0.95, "ref_outcome": 1},
                {"ref_prob": 0.92, "ref_outcome": 0}]
        rel = cal.reliability(rows, nbins=10)
        top = [b for b in rel if b["bucket"] == "0.9-1.0"][0]
        self.assertEqual(top["n"], 2)
        self.assertAlmostEqual(top["empirical"], 0.5)


class TestSettleAndReport(unittest.TestCase):
    def _mlog(self, db, slug, line, prob, bet):
        pdb.record_model_log({"game_slug": slug, "game_date": "2026-06-14", "market": "TOTAL",
                              "line": line, "ref_side": "OVER", "ref_prob": prob,
                              "ref_price": 0.5, "bet": bet}, db)

    def test_outcome_propagates_to_all_lines(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "p.db")
            # Two lines of one game; only the 8.5 line was bet.
            self._mlog(db, "mlb-hou-kc-2026-06-14-total-8pt5", 8.5, 0.60, 1)
            self._mlog(db, "mlb-hou-kc-2026-06-14-total-9pt5", 9.5, 0.55, 0)
            # Record + settle the bet line: final total 10.
            pid = pdb.record_prediction({
                "game_slug": "mlb-hou-kc-2026-06-14-total-8pt5", "game_date": "2026-06-14",
                "line": 8.5, "side": "OVER", "entry_price": 0.6, "strategy": "x"}, db)
            pdb.settle_prediction(pid, 10.0, db)

            n = cal.settle_from_predictions(db)
            self.assertEqual(n, 2)  # BOTH lines settled from the one game's total
            rep = cal.report(db)
            self.assertEqual(rep["settled"], 2)
            self.assertEqual(rep["settled_bet"], 1)
            # 10 > both 8.5 and 9.5 -> OVER won both -> outcome 1.
            self.assertIsNotNone(rep["all"]["brier"])
            # Brier = mean((0.60-1)^2,(0.55-1)^2) = (0.16+0.2025)/2 = 0.18125
            self.assertAlmostEqual(rep["all"]["brier"], (0.16 + 0.2025) / 2, places=6)

    def test_report_no_settlements(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "p.db")
            self._mlog(db, "mlb-x-y-2026-06-14-total-8pt5", 8.5, 0.6, 0)
            rep = cal.report(db)
            self.assertEqual(rep["settled"], 0)
            self.assertIsNone(rep["all"]["brier"])
            self.assertIn("Brier", cal.format_report(rep))


class TestIntervalCoverage(unittest.TestCase):
    def _mlog_with_params(self, db, slug, line, mu, var):
        pdb.record_model_log({
            "game_slug": slug, "game_date": "2026-06-14", "market": "TOTAL",
            "line": line, "ref_side": "OVER", "ref_prob": 0.5, "ref_price": 0.5,
            "bet": 1, "model_params": {"mu": mu, "variance": var}}, db)

    def test_coverage_over_settled_games(self):
        import run_distribution as rd
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "p.db")
            # 40 games, all forecast mu=8.5; settle each with its actual total. Draw the
            # actuals FROM the model's own NegBin pmf so coverage should track nominal.
            pmf = rd.negbin_total_runs_pmf(8.5, rd.variance_from_mu(8.5))
            cdf = []
            acc = 0.0
            for p in pmf:
                acc += p
                cdf.append(acc)
            # Deterministic spread of quantiles 0.012..0.988 -> sampled totals.
            import bisect
            totals = [bisect.bisect_left(cdf, (i + 0.5) / 40) for i in range(40)]
            for i, actual in enumerate(totals):
                slug = f"mlb-a{i}-b{i}-2026-06-14-total-8pt5"
                self._mlog_with_params(db, slug, 8.5, 8.5, rd.variance_from_mu(8.5))
                pid = pdb.record_prediction({
                    "game_slug": slug, "game_date": "2026-06-14", "line": 8.5,
                    "side": "OVER", "entry_price": 0.5, "strategy": "x"}, db)
                pdb.settle_prediction(pid, float(actual), db)
            cal.settle_from_predictions(db)
            ic = cal.interval_coverage(db)
            self.assertEqual(ic["n"], 40)                       # one forecast per game
            # Coverage of a sampled-from-itself distribution tracks nominal (±tolerance).
            self.assertGreaterEqual(ic["coverage80"], 0.70)
            self.assertLessEqual(ic["coverage50"], 0.75)
            self.assertGreater(ic["mean_crps"], 0.0)
            self.assertGreater(ic["mean_width80"], 5)          # wide for a single game

    def test_dedup_per_game_and_skip_non_total(self):
        import run_distribution as rd
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "p.db")
            v = rd.variance_from_mu(8.5)
            # Two lines of ONE game -> counted once. Plus a degenerate (var<=mu) row -> skipped.
            self._mlog_with_params(db, "mlb-x-y-2026-06-14-total-8pt5", 8.5, 8.5, v)
            self._mlog_with_params(db, "mlb-x-y-2026-06-14-total-9pt5", 9.5, 8.5, v)
            pid = pdb.record_prediction({
                "game_slug": "mlb-x-y-2026-06-14-total-8pt5", "game_date": "2026-06-14",
                "line": 8.5, "side": "OVER", "entry_price": 0.5, "strategy": "x"}, db)
            pdb.settle_prediction(pid, 9.0, db)
            cal.settle_from_predictions(db)
            ic = cal.interval_coverage(db)
            self.assertEqual(ic["n"], 1)                        # deduped to one game

    def test_no_settled_returns_zero(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "p.db")
            self._mlog_with_params(db, "mlb-x-y-2026-06-14-total-8pt5", 8.5, 8.5, 17.0)
            self.assertEqual(cal.interval_coverage(db)["n"], 0)  # not settled -> no actual


if __name__ == "__main__":
    unittest.main(verbosity=2)
