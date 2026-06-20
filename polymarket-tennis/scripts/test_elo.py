#!/usr/bin/env python3
"""Offline tests for the surface-aware Elo engine + ratings resolution.

Run: python polymarket-tennis/scripts/test_elo.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import elo  # noqa: E402
import ratings as rmod  # noqa: E402
import ratings_source  # noqa: E402


class TestElo(unittest.TestCase):
    def test_equal_elo_is_coin_flip(self):
        self.assertAlmostEqual(elo.expected(1500, 1500), 0.5, places=9)

    def test_higher_elo_favored_and_symmetric(self):
        p = elo.expected(1600, 1500)
        self.assertGreater(p, 0.5)
        self.assertAlmostEqual(p + elo.expected(1500, 1600), 1.0, places=9)

    def test_400_point_gap_is_10_to_1(self):
        # The defining property of the Elo logistic: +400 -> ~10x odds (~0.909).
        self.assertAlmostEqual(elo.expected(1900, 1500), 10 / 11, places=6)

    def test_k_factor_shrinks_with_experience(self):
        self.assertGreater(elo.k_factor(0), elo.k_factor(50))
        self.assertAlmostEqual(elo.k_factor(0), 250 / (5 ** 0.4), places=6)

    def test_update_moves_toward_outcome(self):
        before = 1500.0
        after_win = elo.update(before, elo.k_factor(20), 1.0, elo.expected(before, 1500))
        after_loss = elo.update(before, elo.k_factor(20), 0.0, elo.expected(before, 1500))
        self.assertGreater(after_win, before)
        self.assertLess(after_loss, before)

    def test_surface_blend(self):
        # 50/50 blend of overall 1500 and clay 1700 -> 1600.
        self.assertAlmostEqual(elo.blend(1500, 1700, 0.5), 1600.0, places=9)
        self.assertAlmostEqual(elo.blend(1500, None, 0.5), 1500.0, places=9)  # no surface

    def test_blended_elo_uses_surface(self):
        r = {"elo": 1500, "clay": 1800, "hard": 1500}
        # Clay specialist looks much stronger on clay than on hard.
        self.assertGreater(elo.blended_elo(r, "clay"), elo.blended_elo(r, "hard"))

    def test_match_win_prob_surface_sensitive(self):
        clay_spec = {"elo": 1600, "clay": 1850, "hard": 1550}
        hard_spec = {"elo": 1600, "clay": 1500, "hard": 1850}
        self.assertGreater(elo.match_win_prob(clay_spec, hard_spec, "clay"), 0.5)
        self.assertLess(elo.match_win_prob(clay_spec, hard_spec, "hard"), 0.5)


class TestPricing(unittest.TestCase):
    def test_decimal_odds_and_band(self):
        self.assertAlmostEqual(elo.decimal_odds(0.5), 2.0)
        self.assertTrue(elo.passes_odds_band(0.5))
        self.assertFalse(elo.passes_odds_band(0.95))   # too short (below 1.10x)
        self.assertFalse(elo.passes_odds_band(0.10))   # too long (above 5.0x)

    def test_kelly_positive_only_with_edge(self):
        self.assertGreater(elo.kelly_fraction(0.60, 0.50), 0.0)   # model > price -> bet
        self.assertEqual(elo.kelly_fraction(0.40, 0.50), 0.0)     # model < price -> no bet
        self.assertAlmostEqual(elo.half_kelly(0.60, 0.50),
                               elo.kelly_fraction(0.60, 0.50) / 2)

    def test_devig_normalizes(self):
        fair = elo.devig_two_way(0.55, 0.52)            # book sums to 1.07
        self.assertIsNotNone(fair)
        self.assertAlmostEqual(sum(fair), 1.0, places=9)
        self.assertGreater(fair[0], fair[1])


class TestRatings(unittest.TestCase):
    def _csv(self, d):
        path = os.path.join(d, "r.csv")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("player,elo,hard,clay,grass\n"
                     "Carlos Alcaraz,2250,2240,2300,2210\n"
                     "Jannik Sinner,2230,2260,2150,2180\n")
        return path

    def test_resolve_by_full_and_surname_and_accent(self):
        with tempfile.TemporaryDirectory() as d:
            rt = rmod.load_ratings(self._csv(d))
            self.assertIsNotNone(rmod.resolve("Carlos Alcaraz", rt))
            self.assertIsNotNone(rmod.resolve("alcaraz", rt))        # surname
            self.assertIsNotNone(rmod.resolve("C. Alcaraz", rt))     # initial + surname
            self.assertEqual(rmod.resolve("Alcaraz", rt)["clay"], 2300.0)
            self.assertIsNone(rmod.resolve("Roger Federer", rt))     # uncovered -> None

    def test_uncovered_player_is_none(self):
        self.assertIsNone(rmod.resolve("anyone", {}))


class TestEloBuilder(unittest.TestCase):
    def test_consistent_winner_rises_above_loser(self):
        # 'winner' beats 'loser' 10x on hard -> higher overall and hard Elo.
        matches = [{"date": f"2026010{i}", "surface": "hard",
                    "winner": "Win Player", "loser": "Lose Player"} for i in range(1, 10)]
        r = ratings_source.build_elo_from_matches(matches)
        self.assertGreater(r["win player"]["elo"], r["lose player"]["elo"])
        self.assertGreater(r["win player"]["elo"], elo.START_ELO)
        self.assertGreater(r["win player"]["hard"], elo.START_ELO)
        self.assertIsNone(r["win player"]["clay"])     # never played clay

    def test_surface_specific_isolated_from_overall(self):
        matches = [
            {"date": "20260101", "surface": "clay", "winner": "Clay King", "loser": "B"},
            {"date": "20260102", "surface": "clay", "winner": "Clay King", "loser": "C"},
            {"date": "20260103", "surface": "hard", "winner": "D", "loser": "Clay King"},
        ]
        r = ratings_source.build_elo_from_matches(matches)
        # Clay rating reflects 2 wins; hard rating reflects a loss -> clay > hard.
        self.assertGreater(r["clay king"]["clay"], r["clay king"]["hard"])

    def test_parse_match_row(self):
        row = {"winner_name": "Daniel Altmaier", "loser_name": "Frances Tiafoe",
               "surface": "Clay", "tourney_date": "20260601"}
        pm = ratings_source.parse_match_row(row)
        self.assertEqual(pm["surface"], "clay")
        self.assertEqual(pm["winner"], "Daniel Altmaier")
        self.assertIsNone(ratings_source.parse_match_row({"winner_name": "", "loser_name": "x"}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
