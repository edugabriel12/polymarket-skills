#!/usr/bin/env python3
"""Offline tests for the CSV bet-history parser, classifier, and confidence rollup."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import csv_parser as cp  # noqa: E402
import wallet_report as wr  # noqa: E402

_CSV = (
    "Data;Evento;Aposta;Conf.;Odd;Investido;ROI%;Lucro\n"
    '2026-06-25;"Curaçao vs. Côte d\'Ivoire: O/U 3.5";UNDER;Média;1,79;19999,96;78,6;15714,32\n'
    '2026-06-24;"Spread: Morocco (-1.5)";MOROCCO;Alta;1,54;19999,97;53,8;10769,26\n'
    '2026-06-23;"Will Algeria win on 2026-06-23?";YES;Baixa;1,54;5999,99;-100;-5999,99\n'
    '2026-06-22;"Knicks vs. Spurs";KNICKS;Alta;1,99;1000;89;890\n'
    '2026-06-21;"Boston Red Sox vs. Colorado Rockies";BOSTON RED SOX;Baixa;1,43;100;-27,3;-27,3\n'
    '2026-06-20;"Stars vs. Wild";STARS;Média;1,48;200;-49,3;-98,6\n'
    '2026-06-19;"UFC 328: Sean Strickland vs. Khamzat Chimaev";SEAN STRICKLAND;Alta;2,1;100;79,8;79,8\n'
)


class TestNumberAndConf(unittest.TestCase):
    def test_num(self):
        self.assertAlmostEqual(cp._num("19999,96"), 19999.96)
        self.assertAlmostEqual(cp._num("-100"), -100.0)
        self.assertAlmostEqual(cp._num("1,79"), 1.79)
        self.assertEqual(cp._num(""), 0.0)
        self.assertEqual(cp._num("x"), 0.0)

    def test_conf(self):
        self.assertEqual(cp._norm_conf("Alta"), "Alta")
        self.assertEqual(cp._norm_conf("Média"), "Média")
        self.assertEqual(cp._norm_conf("media"), "Média")
        self.assertEqual(cp._norm_conf("Baixa"), "Baixa")


class TestClassifyEvent(unittest.TestCase):
    def test_soccer(self):
        self.assertEqual(cp.classify_event("Curaçao vs. Côte d'Ivoire: O/U 3.5", "UNDER"), "Soccer")
        self.assertEqual(cp.classify_event("Will Algeria win on 2026-06-23?", "YES"), "Soccer")
        self.assertEqual(cp.classify_event("Spread: Morocco (-1.5)", "MOROCCO"), "Soccer")

    def test_us_leagues(self):
        self.assertEqual(cp.classify_event("Knicks vs. Spurs", "KNICKS"), "Basketball")
        self.assertEqual(cp.classify_event("Washington Mystics vs. New York Liberty", "LIBERTY"),
                         "Basketball")
        self.assertEqual(cp.classify_event("Boston Red Sox vs. Colorado Rockies", "BOSTON RED SOX"),
                         "Baseball")
        self.assertEqual(cp.classify_event("Stars vs. Wild", "STARS"), "Hockey")
        self.assertEqual(cp.classify_event("Hurricanes vs. Flyers", "FLYERS"), "Hockey")

    def test_texas_rangers_is_baseball_not_hockey(self):
        # Regression: "rangers" must NOT pull a Texas Rangers game into NHL.
        self.assertEqual(cp.classify_event("Texas Rangers vs. Boston Red Sox", "TEXAS RANGERS"),
                         "Baseball")

    def test_ufc(self):
        self.assertEqual(cp.classify_event("UFC 328: A vs. B (Lightweight)", "A"), "Combat Sports")


class TestSubcategoryFromCsv(unittest.TestCase):
    def test_market_types(self):
        recs = cp.parse_csv(_CSV)
        by = {(r["title"][:10], r["subcategory"]) for r in recs}
        subs = {r["subcategory"] for r in recs}
        self.assertIn("Over/Under gols", subs)     # O/U soccer
        self.assertIn("Handicap", subs)            # Spread: Morocco
        self.assertIn("Moneyline (1X2)", subs)     # Will Algeria win
        self.assertIn("Vencedor da luta", subs)    # UFC
        self.assertTrue(by)


class TestParseAndRollup(unittest.TestCase):
    def test_parse_row_count_and_metrics(self):
        recs = cp.parse_csv(_CSV)
        self.assertEqual(len(recs), 7)
        rep = wr.rollup_csv(recs)
        self.assertEqual(rep["n_markets"], 7)
        self.assertEqual(rep["source"], "csv")
        ov = rep["overall"]
        self.assertEqual(ov["markets"], 7)
        self.assertEqual(ov["wins"], 4)            # 4 positive-profit rows
        self.assertEqual(ov["losses"], 3)
        self.assertAlmostEqual(ov["win_rate"], 4 / 7, places=4)

    def test_by_confidence(self):
        rep = wr.rollup_csv(cp.parse_csv(_CSV))
        conf = {c["confidence"]: c for c in rep["by_confidence"]}
        self.assertEqual(set(conf), {"Alta", "Média", "Baixa"})
        # ordered Alta, Média, Baixa
        self.assertEqual([c["confidence"] for c in rep["by_confidence"]],
                         ["Alta", "Média", "Baixa"])
        # Alta: Morocco(win), Knicks(win), Strickland(win) -> 3 bets, 100% win
        self.assertEqual(conf["Alta"]["markets"], 3)
        self.assertEqual(conf["Alta"]["wins"], 3)
        self.assertAlmostEqual(conf["Alta"]["win_rate"], 1.0)

    def test_category_has_confidence_split(self):
        rep = wr.rollup_csv(cp.parse_csv(_CSV))
        soccer = next(c for c in rep["by_category"] if c["category"] == "Soccer")
        self.assertTrue(soccer["subcategories"])
        self.assertTrue(soccer["by_confidence"])
        # each subcategory now carries its OWN confidence split, and the report exposes the
        # filter_tree options (category -> subcategory -> [confidences]) for the Add screen.
        self.assertTrue(all("by_confidence" in s for s in soccer["subcategories"]))
        self.assertIn("filter_tree", rep)
        self.assertIn("Soccer", rep["filter_tree"])

    def test_empty_csv(self):
        self.assertEqual(cp.parse_csv("Data;Evento;Aposta;Conf.;Odd;Investido;ROI%;Lucro\n"), [])


class TestTennisHeadToHead(unittest.TestCase):
    """Obscure tennis (Challenger/ITF/qualifying) carries no tour keyword — only the
    '{Tournament}: Player vs Player' shape. It must still classify as Tennis, without
    stealing soccer/combat matches that are caught earlier or by keyword."""

    def test_obscure_tournament_matches_are_tennis(self):
        for ev in ["Targu Mures: Felix Balshaw vs Martin Krumich",
                   "Plovdiv: Andres Santamarta vs Ognjen Milic",
                   "Mallorca Championships: Ethan Quinn vs Vit Kopriva",
                   "Lexus Eastbourne Open: Gabriel Diallo vs Tomas Etcheverry",
                   "Bad Homburg Open: Iga Swiatek vs Emma Navarro"]:
            self.assertEqual(cp.classify_event(ev, "PLAYER"), "Tennis", ev)

    def test_keyworded_tennis_still_tennis(self):
        self.assertEqual(
            cp.classify_event("Wimbledon, Qualification ATP: Keegan Smith vs Moez Echargui",
                              "KEEGAN SMITH"), "Tennis")

    def test_soccer_and_combat_not_stolen(self):
        self.assertEqual(cp.classify_event("Champions League: Real Madrid vs Barcelona",
                                           "REAL MADRID"), "Soccer")           # keyword wins
        self.assertEqual(cp.classify_event("Arsenal vs Chelsea epl-ars-che-2026-06-25",
                                           "ARSENAL"), "Soccer")               # slug keyword
        self.assertEqual(cp.classify_event("UFC 328: Sean Strickland vs. Khamzat Chimaev",
                                           "SEAN STRICKLAND"), "Combat Sports")  # caught earlier

    def test_non_head_to_head_other_stays_other(self):
        self.assertEqual(cp.classify_event("Some random prop about a thing", "YES"), "Other")

    def test_obscure_tennis_counted_in_rollup(self):
        csv = ("Data;Evento;Aposta;Conf.;Odd;Investido;ROI%;Lucro\n"
               '2026-06-25;"Targu Mures: Felix Balshaw vs Martin Krumich";FELIX BALSHAW;Alta;1,75;100;75,1;75\n'
               '2026-06-25;"Plovdiv: Andres Santamarta vs Ognjen Milic";ANDRES SANTAMARTA;Baixa;1,62;100;62,1;62\n')
        rep = wr.rollup_csv(cp.parse_csv(csv))
        ten = next(c for c in rep["by_category"] if c["category"] == "Tennis")
        self.assertEqual(ten["markets"], 2)
        self.assertEqual([s["subcategory"] for s in ten["subcategories"]], ["Vencedor da partida"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
