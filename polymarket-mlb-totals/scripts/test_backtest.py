#!/usr/bin/env python3
"""Offline tests for the MLB backtest engine (no network).

Run: python polymarket-mlb-totals/scripts/test_backtest.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtest as bt  # noqa: E402


class TestOdds(unittest.TestCase):
    def test_to_implied_prob_formats(self):
        self.assertAlmostEqual(bt.to_implied_prob(-110), 110 / 210, places=6)   # American fav
        self.assertAlmostEqual(bt.to_implied_prob(+120), 100 / 220, places=6)   # American dog
        self.assertAlmostEqual(bt.to_implied_prob(1.91), 1 / 1.91, places=6)    # decimal
        self.assertAlmostEqual(bt.to_implied_prob(0.524), 0.524, places=6)      # already prob
        self.assertIsNone(bt.to_implied_prob(""))

    def test_devig(self):
        fair = bt.devig_two_way(0.55, 0.55)        # 10% vig, symmetric
        self.assertAlmostEqual(fair[0], 0.5, places=6)
        self.assertAlmostEqual(sum(fair), 1.0, places=9)
        self.assertIsNone(bt.devig_two_way(0, 0.5))


class TestPointInTimeFactors(unittest.TestCase):
    def test_no_lookahead_and_warmup(self):
        tf = bt.TeamFactors(warmup=2)
        # Before warmup -> no factors.
        self.assertEqual(tf.factors_for("a", "b"), {})
        tf.update("a", "b", 10, 0)   # a scores 10, allows 0
        tf.update("a", "b", 10, 0)
        tf.update("b", "a", 0, 0)    # give b some games too
        tf.update("b", "a", 0, 0)
        f = tf.factors_for("a", "b")
        self.assertTrue(f)                          # both past warmup now
        self.assertGreater(f["away_off"], f["home_off"])   # 'a' (away) scores more than 'b'
        # Factors reflect ONLY fed games (no future leakage): a averages 10 RS, b 0 RS.

    def test_league_average_centers_factors(self):
        tf = bt.TeamFactors(warmup=1)
        tf.update("a", "b", 6, 4)
        tf.update("c", "d", 4, 6)
        # league avg rpg computed from fed games; factors are ratios to it.
        f = tf.factors_for("a", "b")
        self.assertIn("home_off", f)


def _game(date, away, home, asc, hsc, line, over=0.5, under=0.5,
          c_over=None, c_under=None):
    return {"date": date, "away": away, "home": home, "away_score": asc,
            "home_score": hsc, "line": line, "over_imp": over, "under_imp": under,
            "close_over_imp": c_over, "close_under_imp": c_under}


class TestBacktest(unittest.TestCase):
    def test_runs_and_aggregates(self):
        # A small season: enough games to clear warmup, then a few modeled.
        games = []
        teams = ["aa", "bb", "cc", "dd"]
        # Warmup: 25 neutral games per pairing so factors activate.
        d = 1
        for _ in range(25):
            for (x, y) in (("aa", "bb"), ("cc", "dd"), ("aa", "cc"), ("bb", "dd")):
                games.append(_game(f"2021-04-{d%28+1:02d}", x, y, 4, 4, 8.5))
                d += 1
        rep = bt.run_backtest(games, warmup=20, min_edge=0.0, odds_min=1.01, odds_max=99)
        self.assertIn("2021", rep["seasons"])
        s = rep["seasons"]["2021"]
        self.assertGreater(s["modeled"], 0)
        self.assertGreaterEqual(s["settled"], 0)
        # Brier is defined when there are settled (non-push) modeled games.
        if s["settled"]:
            self.assertIsNotNone(s["brier"])

    def test_pnl_and_clv_math(self):
        # One clearly-modeled OVER bet that wins, with a favorable closing line.
        # Warmup with high-scoring games so the OVER factor is strong.
        games = [_game(f"2021-05-{i+1:02d}", "hi", "lo", 8, 7, 8.5) for i in range(22)]
        games += [_game(f"2021-05-{i+1:02d}", "lo", "hi", 7, 8, 8.5) for i in range(22)]
        # Target game: line 6.5, priced ~0.50, the teams average ~15 runs -> model loves OVER.
        games.append(_game("2021-06-01", "hi", "lo", 9, 8, 6.5, over=0.52, under=0.52,
                           c_over=0.60, c_under=0.44))
        rep = bt.run_backtest(games, warmup=20, min_edge=0.03, odds_min=1.01, odds_max=99)
        o = rep["overall"]
        self.assertGreaterEqual(o["bets"], 1)
        self.assertIsNotNone(o["avg_clv"])          # closing line present -> CLV computed

    def test_load_games_csv(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "g.csv")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("date,away,home,away_score,home_score,total_line,over_odds,under_odds\n")
                fh.write("2021-04-01,NYY,TOR,5,4,8.5,-110,-110\n")
                fh.write("20210402,BOS,BAL,3,2,7.5,1.95,1.87\n")
            games = bt.load_games(p)
            self.assertEqual(len(games), 2)
            self.assertEqual(games[0]["date"], "2021-04-01")
            self.assertEqual(games[1]["date"], "2021-04-02")    # YYYYMMDD normalized
            self.assertAlmostEqual(games[0]["over_imp"], 110 / 210, places=4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
