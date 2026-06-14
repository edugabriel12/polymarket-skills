#!/usr/bin/env python3
"""Offline unit tests for the Dixon-Coles engine (stdlib unittest, no network).

Run: python polymarket-soccer-goals/scripts/test_dixon_coles.py
"""

from __future__ import annotations

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dixon_coles as dc  # noqa: E402


def total_mass(m):
    return math.fsum(m[i][j] for i in range(len(m)) for j in range(len(m)))


class TestScoreMatrix(unittest.TestCase):
    def test_sums_to_one_nonneg(self):
        for lh, la in [(1.5, 1.2), (0.8, 2.1), (1.35, 1.35)]:
            m = dc.score_matrix(lh, la)
            self.assertAlmostEqual(total_mass(m), 1.0, places=9)
            self.assertTrue(all(m[i][j] >= 0 for i in range(len(m)) for j in range(len(m))))

    def test_marginal_means_close_to_lambda(self):
        # With rho=0 (no correction) the marginals are exactly Poisson(lambda).
        lh, la = 1.6, 1.1
        m = dc.score_matrix(lh, la, rho=0.0)
        mean_home = math.fsum(i * sum(m[i]) for i in range(len(m)))
        mean_away = math.fsum(j * sum(m[i][j] for i in range(len(m))) for j in range(len(m)))
        self.assertAlmostEqual(mean_home, lh, delta=0.02)
        self.assertAlmostEqual(mean_away, la, delta=0.02)

    def test_negative_rho_raises_draw_prob(self):
        lh, la = 1.3, 1.3
        m0 = dc.score_matrix(lh, la, rho=0.0)
        mneg = dc.score_matrix(lh, la, rho=-0.12)
        draws0 = sum(m0[k][k] for k in range(len(m0)))
        draws_neg = sum(mneg[k][k] for k in range(len(mneg)))
        self.assertGreater(draws_neg, draws0)  # DC correction adds draws


class TestMarkets(unittest.TestCase):
    def test_prob_over_half_line(self):
        m = dc.score_matrix(1.4, 1.2)
        r = dc.prob_over(2.5, m)
        self.assertEqual(r["p_push"], 0.0)
        self.assertAlmostEqual(r["p_over"] + r["p_under"], 1.0, places=9)
        # Over 2.5 = mass with i+j >= 3
        manual = sum(m[i][j] for i in range(len(m)) for j in range(len(m)) if i + j >= 3)
        self.assertAlmostEqual(r["p_over"], manual, places=12)

    def test_prob_over_integer_push(self):
        m = dc.score_matrix(1.2, 1.0)
        r = dc.prob_over(2.0, m)
        manual_push = sum(m[i][j] for i in range(len(m)) for j in range(len(m)) if i + j == 2)
        self.assertAlmostEqual(r["p_push"], manual_push, places=12)
        self.assertAlmostEqual(r["p_over_eff"] + r["p_under_eff"], 1.0, places=9)

    def test_prob_over_increases_with_total(self):
        prev = -1
        for tot in [1.8, 2.4, 3.0, 3.6]:
            lh, la = dc.lambdas_from_total_supremacy(tot, 0.0)
            p = dc.prob_over(2.5, dc.score_matrix(lh, la))["p_over"]
            self.assertGreater(p, prev)
            prev = p

    def test_prob_btts(self):
        m = dc.score_matrix(1.5, 1.3)
        r = dc.prob_btts(m)
        manual = sum(m[i][j] for i in range(1, len(m)) for j in range(1, len(m)))
        self.assertAlmostEqual(r["p_yes"], manual, places=12)
        self.assertAlmostEqual(r["p_yes"] + r["p_no"], 1.0, places=12)

    def test_btts_decreases_with_supremacy(self):
        # Same total, more lopsided -> lower BTTS (one team's lambda shrinks).
        total = 2.8
        lh1, la1 = dc.lambdas_from_total_supremacy(total, 0.0)
        lh2, la2 = dc.lambdas_from_total_supremacy(total, 1.6)
        b1 = dc.prob_btts(dc.score_matrix(lh1, la1))["p_yes"]
        b2 = dc.prob_btts(dc.score_matrix(lh2, la2))["p_yes"]
        self.assertGreater(b1, b2)


class TestLambdas(unittest.TestCase):
    def test_total_supremacy_split(self):
        lh, la = dc.lambdas_from_total_supremacy(2.8, 0.6)
        self.assertAlmostEqual(lh, 1.7, places=6)
        self.assertAlmostEqual(la, 1.1, places=6)

    def test_supremacy_from_elo_sign(self):
        self.assertGreater(dc.supremacy_from_elo(1800, 1600), 0)  # stronger home
        self.assertLess(dc.supremacy_from_elo(1600, 1900), 0)
        # home advantage tilts an even matchup positive
        self.assertGreater(dc.supremacy_from_elo(1700, 1700), 0)

    def test_adjust_total(self):
        base = dc.adjust_total(2.7)
        hot = dc.adjust_total(2.7, att_home=1.3, att_away=1.3, def_home=1.2, def_away=1.2)
        self.assertGreater(hot, base)


class TestMarketImpliedFallback(unittest.TestCase):
    def test_recovers_over_price(self):
        for line, p in [(2.5, 0.45), (2.5, 0.58), (3.5, 0.30)]:
            lh, la = dc.market_implied_lambdas(line, p)
            got = dc.prob_over(line, dc.score_matrix(lh, la))["p_over_eff"]
            self.assertAlmostEqual(got, p, delta=2e-3)

    def test_recovers_over_and_btts(self):
        line, p_over, p_btts = 2.5, 0.52, 0.55
        lh, la = dc.market_implied_lambdas(line, p_over, p_btts)
        m = dc.score_matrix(lh, la)
        self.assertAlmostEqual(dc.prob_over(line, m)["p_over_eff"], p_over, delta=3e-3)
        self.assertAlmostEqual(dc.prob_btts(m)["p_yes"], p_btts, delta=5e-3)


class TestOdds(unittest.TestCase):
    def test_filter_boundaries(self):
        self.assertFalse(dc.passes_odds_filter(0.333))
        self.assertTrue(dc.passes_odds_filter(0.3334))
        self.assertTrue(dc.passes_odds_filter(0.625))
        self.assertFalse(dc.passes_odds_filter(0.6251))
        self.assertAlmostEqual(dc.decimal_odds(0.5), 2.0, places=9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
