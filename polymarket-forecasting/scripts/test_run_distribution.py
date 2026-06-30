#!/usr/bin/env python3
"""Offline unit tests for the deterministic distribution core (stdlib unittest, no pytest).

Run: python polymarket-forecasting/scripts/test_run_distribution.py
Covers run_distribution.py (NegBin pmf, P(Over), market-implied mu, odds filter, market
anchor) — the sport-agnostic totals math reused by the soccer model.
"""

from __future__ import annotations

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run_distribution as rd  # noqa: E402


def mean_of(pmf):
    return math.fsum(k * pmf[k] for k in range(len(pmf)))


def var_of(pmf):
    mu = mean_of(pmf)
    return math.fsum((k - mu) ** 2 * pmf[k] for k in range(len(pmf)))


class TestNegBinPMF(unittest.TestCase):
    MUS = [6.0, 8.5, 9.0, 11.0]

    def test_pmf_sums_to_one_and_nonneg(self):
        for mu in self.MUS:
            pmf = rd.negbin_total_runs_pmf(mu, 2 * mu)
            self.assertAlmostEqual(math.fsum(pmf), 1.0, places=9)
            self.assertTrue(all(x >= 0 for x in pmf))

    def test_mean_recovers_mu(self):
        for mu in self.MUS:
            pmf = rd.negbin_total_runs_pmf(mu, 2 * mu, kmax=80)
            self.assertAlmostEqual(mean_of(pmf), mu, delta=0.05)

    def test_variance_recovers_2mu(self):
        # The headline research fact: variance ~ 2x mean (overdispersion).
        for mu in self.MUS:
            pmf = rd.negbin_total_runs_pmf(mu, 2 * mu, kmax=120)
            self.assertAlmostEqual(var_of(pmf), 2 * mu, delta=0.25)

    def test_params_from_moments(self):
        r, p = rd.negbin_params_from_moments(9.0, 18.0)  # var = 2*mu
        self.assertAlmostEqual(p, 0.5, places=9)
        self.assertAlmostEqual(r, 9.0, places=9)

    def test_underdispersion_raises(self):
        with self.assertRaises(ValueError):
            rd.negbin_params_from_moments(9.0, 9.0)   # var == mu
        with self.assertRaises(ValueError):
            rd.negbin_params_from_moments(9.0, 5.0)   # var < mu


class TestProbOver(unittest.TestCase):
    def test_half_line_no_push(self):
        pmf = rd.negbin_total_runs_pmf(8.5, 17.0)
        res = rd.prob_over(8.5, pmf)
        self.assertEqual(res["p_push"], 0.0)
        self.assertAlmostEqual(res["p_over"] + res["p_under"], 1.0, places=9)
        self.assertAlmostEqual(res["p_over"], math.fsum(pmf[9:]), places=12)
        self.assertEqual(res["need"], 9)

    def test_integer_line_push_renormalizes(self):
        pmf = rd.negbin_total_runs_pmf(9.0, 18.0)
        res = rd.prob_over(9.0, pmf)
        self.assertAlmostEqual(res["p_push"], pmf[9], places=12)
        self.assertAlmostEqual(res["p_over_eff"] + res["p_under_eff"], 1.0, places=9)
        self.assertAlmostEqual(res["p_over_eff"], res["p_over"] / (1 - res["p_push"]), places=12)

    def test_monotonic_in_mu(self):
        prev = -1.0
        for mu in [7.0, 8.0, 9.0, 10.0, 11.0]:
            pmf = rd.negbin_total_runs_pmf(mu, 2 * mu)
            p_over = rd.prob_over(8.5, pmf)["p_over"]
            self.assertGreater(p_over, prev)
            prev = p_over

    def test_edge_sign_sanity(self):
        # mu chosen so P(Over 8.5) > 0.5; a low price -> positive edge, high -> negative.
        pmf = rd.negbin_total_runs_pmf(9.6, 19.2)
        p_over = rd.prob_over(8.5, pmf)["p_over_eff"]
        self.assertGreater(p_over - 0.50, 0)
        self.assertLess(p_over - 0.70, 0)


class TestMarketImpliedMu(unittest.TestCase):
    def test_recovers_market_prob(self):
        # The anti-fabrication guard: implied mu reproduces the market P(Over).
        for line in [7.5, 8.5, 9.0, 10.5]:
            for target in [0.35, 0.45, 0.55, 0.65]:
                mu = rd.market_implied_mu(line, target)
                pmf = rd.negbin_total_runs_pmf(mu, rd.variance_from_mu(mu))
                got = rd.prob_over(line, pmf)["p_over_eff"]
                self.assertAlmostEqual(got, target, delta=1e-3)


class TestMuDerivation(unittest.TestCase):
    def test_baseline_scale(self):
        self.assertAlmostEqual(rd.baseline_mu(100, 8.5), 8.5, places=9)
        self.assertAlmostEqual(rd.baseline_mu(128, 8.5), 8.5 * 1.28, places=9)

    def test_adjust_all_none_is_identity(self):
        self.assertAlmostEqual(rd.adjust_mu(8.5), 8.5, places=9)

    def test_offense_and_wind_raise_mu(self):
        base = rd.adjust_mu(8.5)
        hotter = rd.adjust_mu(8.5, home_off=1.2, away_off=1.2)
        self.assertGreater(hotter, base)
        windy = rd.adjust_mu(8.5, wind_out_mph=20)
        self.assertGreater(windy, base)

    def test_factor_clamp(self):
        # Absurd inputs are clamped, not allowed to explode mu.
        mu = rd.adjust_mu(8.5, home_off=99, away_off=99)
        self.assertLessEqual(mu, rd._MU_HI)


class TestOddsFilter(unittest.TestCase):
    def test_boundaries(self):
        # Explicit bounds so the test is independent of the default odds band.
        self.assertFalse(rd.passes_odds_filter(0.333, 1.60, 3.0))     # just below 1/3
        self.assertTrue(rd.passes_odds_filter(0.3334, 1.60, 3.0))
        self.assertTrue(rd.passes_odds_filter(0.625, 1.60, 3.0))      # exactly 1/1.6
        self.assertFalse(rd.passes_odds_filter(0.6251, 1.60, 3.0))

    def test_default_band_is_1p50(self):
        self.assertAlmostEqual(rd.ODDS_MIN_DEFAULT, 1.50, places=9)
        self.assertTrue(rd.passes_odds_filter(0.66))    # 1.515x payout, in default band
        self.assertFalse(rd.passes_odds_filter(0.67))   # 1.493x payout, below 1.50x floor

    def test_decimal_odds_mapping(self):
        self.assertAlmostEqual(rd.decimal_odds(0.625), 1.60, places=6)
        self.assertAlmostEqual(rd.decimal_odds(1 / 3.0), 3.0, places=6)


class TestMarketAnchor(unittest.TestCase):
    def test_shrinks_toward_market(self):
        # weight<1 -> the anchored mu sits between model and market, closer to market.
        a = rd.anchor_to_market(10.0, 9.0, weight=0.6, cap=10.0)
        self.assertAlmostEqual(a, 9.0 + 0.6 * 1.0, places=9)   # 9.6
        self.assertLess(abs(a - 9.0), abs(10.0 - 9.0))

    def test_caps_deviation(self):
        # A huge raw gap is hard-capped at +/- cap runs from the market.
        self.assertAlmostEqual(rd.anchor_to_market(13.0, 9.0, weight=1.0, cap=0.75), 9.75)
        self.assertAlmostEqual(rd.anchor_to_market(5.0, 9.0, weight=1.0, cap=0.75), 8.25)

    def test_equal_is_noop(self):
        self.assertAlmostEqual(rd.anchor_to_market(9.0, 9.0), 9.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
