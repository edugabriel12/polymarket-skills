#!/usr/bin/env python3
"""Offline tests for the sub-category (market-type) classifier."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import subcategory as sc  # noqa: E402


class TestSoccer(unittest.TestCase):
    def test_btts(self):
        self.assertEqual(sc.classify("Soccer", "Both teams to score", "epl-ars-che-2026-02-01-btts"),
                         "Ambas Marcam")

    def test_totals(self):
        self.assertEqual(sc.classify("Soccer", "Over/Under 2.5 goals",
                                     "epl-mci-tot-2026-02-02-total-2pt5"), "Over/Under gols")

    def test_corners_are_their_own_subcategory(self):
        # corners O/U must NOT be lumped into the goals over/under bucket
        self.assertEqual(sc.classify("Soccer", "Japan vs. Sweden: O/U 9.5 Total Corners UNDER"),
                         "Escanteios")
        self.assertEqual(sc.classify("Soccer", "Tunisia vs. Japan",
                                     "wc-tun-jpn-2026-06-21-total-corners-8pt5"), "Escanteios")
        # a real goals O/U still classifies as goals (no regression)
        self.assertEqual(sc.classify("Soccer", "Brazil vs. Egypt: O/U 3.5", ""), "Over/Under gols")

    def test_moneyline_default(self):
        self.assertEqual(sc.classify("Soccer", "Arsenal vs Chelsea", "epl-ars-che-2026-02-01"),
                         "Moneyline (1X2)")

    def test_outright(self):
        self.assertEqual(sc.classify("Soccer", "Manchester City to win the Premier League",
                                     "epl-2026-winner"), "Outright")


class TestTennis(unittest.TestCase):
    def test_match_winner_default(self):
        self.assertEqual(sc.classify("Tennis", "Alcaraz to win the match vs Sinner",
                                     "atp-alcaraz-sinner-2026-02-04"), "Vencedor da partida")

    def test_set_betting(self):
        self.assertEqual(sc.classify("Tennis", "Set betting 2-0 Swiatek",
                                     "wta-swiatek-sabalenka-set-2-0"), "Set betting")


class TestBaseball(unittest.TestCase):
    def test_moneyline(self):
        self.assertEqual(sc.classify("Baseball", "Yankees moneyline", "mlb-nyy-bos-2026-04-01"),
                         "Moneyline")

    def test_run_line(self):
        self.assertEqual(sc.classify("Baseball", "Run line -1.5 Dodgers", "mlb-lad-sd-run-line"),
                         "Run line")

    def test_totals(self):
        self.assertEqual(sc.classify("Baseball", "Over 8.5 runs", "mlb-nyy-bos-total-8pt5"),
                         "Over/Under")


class TestEsports(unittest.TestCase):
    def test_series_default(self):
        self.assertEqual(sc.classify("League of Legends", "T1 to win the series vs Gen.G",
                                     "lol-t1-geng-series"), "Vencedor (série)")

    def test_map_winner(self):
        self.assertEqual(sc.classify("League of Legends", "Map 1 winner G2", "lol-g2-fnc-map-1"),
                         "Vencedor de mapa")

    def test_total_maps(self):
        self.assertEqual(sc.classify("Counter-Strike", "Total maps over 2.5",
                                     "cs2-faze-navi-maps-over"), "Total de mapas")

    def test_cs2_outright(self):
        self.assertEqual(sc.classify("Counter-Strike", "FaZe to win the Major", "cs2-major-winner"),
                         "Outright (torneio)")


class TestOtherCategories(unittest.TestCase):
    def test_basketball_spread(self):
        self.assertEqual(sc.classify("Basketball", "Lakers spread -5.5", "nba-lal-bos-spread"),
                         "Spread")

    def test_crypto_price(self):
        self.assertEqual(sc.classify("Crypto", "Bitcoin above $120,000 by March",
                                     "crypto-btc-120k"), "Alvo de preço")

    def test_politics_outright(self):
        self.assertEqual(sc.classify("Politics", "2028 Presidential election winner",
                                     "politics-2028-president-winner"), "Outright")

    def test_unknown_category_universal_fallback(self):
        # A category with no overlay falls through to the universal classifier.
        self.assertEqual(sc.classify("Golf", "Over/Under 70.5 strokes", "pga-total-70pt5"),
                         "Totals")
        self.assertEqual(sc.classify("Golf", "Scottie Scheffler to win the Masters",
                                     "pga-masters-winner"), "Outright")

    def test_truly_unmatched_is_outro(self):
        self.assertEqual(sc.classify("Other", "Some weird prop", "misc-weird-2026"), "Outro")


if __name__ == "__main__":
    unittest.main(verbosity=2)
