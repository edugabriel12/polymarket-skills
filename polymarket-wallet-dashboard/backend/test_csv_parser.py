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


class TestClassifierHardening(unittest.TestCase):
    """Market-type + O/U line-magnitude precedence — the cases that the soccer `o/u` signal and
    team-nickname collisions used to mis-route (validated against the DaBossHogg history)."""

    def test_tennis_total_games_and_set_handicap(self):
        self.assertEqual(cp.classify_event("Cobolli vs. Zverev: Match O/U 36.5", "OVER"), "Tennis")
        self.assertEqual(cp.classify_event("Maria vs. Keys: Match O/U 21.5", "OVER"), "Tennis")
        self.assertEqual(cp.classify_event("Set Handicap: Osaka (-1.5) vs Wang (+1.5)", "OSAKA"), "Tennis")

    def test_combat_rounds_and_method(self):
        self.assertEqual(cp.classify_event("O/U 1.5 Rounds", "UNDER"), "Combat Sports")
        self.assertEqual(cp.classify_event("Will Josh Hokit win by KO or TKO?", "YES"), "Combat Sports")

    def test_soccer_goalscorer_props(self):
        self.assertEqual(cp.classify_event("Harry Kane: 1+ goals", "YES"), "Soccer")
        self.assertEqual(cp.classify_event("Erling Haaland: Anytime Goalscorer", "YES"), "Soccer")

    def test_basketball_player_props(self):
        self.assertEqual(cp.classify_event("Donovan Mitchell: Points O/U 26.5", "YES"), "Basketball")
        self.assertEqual(cp.classify_event("Victor Wembanyama: Rebounds O/U 12.5", "YES"), "Basketball")

    def test_line_magnitude_beats_nickname_collisions(self):
        # College games whose nicknames collide with MLB/NHL must stay Basketball (line >= 100).
        self.assertEqual(cp.classify_event("Louisville Cardinals vs. Michigan State Spartans: O/U 151.5", "OVER"), "Basketball")
        self.assertEqual(cp.classify_event("Utah State Aggies vs. Arizona Wildcats: O/U 154.5", "OVER"), "Basketball")
        self.assertEqual(cp.classify_event("Spurs vs. Knicks: 1H O/U 112.5", "UNDER"), "Basketball")

    def test_ou_line_routes_other_sports(self):
        self.assertEqual(cp.classify_event("Los Angeles Dodgers vs. Minnesota Twins: O/U 9.5", "OVER"), "Baseball")
        self.assertEqual(cp.classify_event("Golden Knights vs. Hurricanes: O/U 5.5", "OVER"), "Hockey")
        self.assertEqual(cp.classify_event("Morocco vs. Haiti: O/U 5.5", "OVER"), "Soccer")

    def test_soccer_corners_not_baseball(self):
        self.assertEqual(cp.classify_event("Arsenal FC vs. Burnley FC: O/U 10.5 Total Corners", "UNDER"), "Soccer")

    def test_college_moneyline_without_line(self):
        self.assertEqual(cp.classify_event("Houston Cougars vs. Arizona Wildcats", "ARIZONA WILDCATS"), "Basketball")

    def test_wbc_final_stage(self):
        self.assertEqual(cp.classify_event("Final Stage: Dominican Rep. vs. USA", "USA"), "Baseball")

    def test_crypto(self):
        self.assertEqual(cp.classify_event("Bitcoin Up or Down - March 19, 10:20PM-10:25PM ET", "UP"), "Crypto")


class TestAmericanFootballAndCollege(unittest.TestCase):
    """American football (NFL + college) as a category, pro/college nickname-collision fixes, and
    Olympic/NRFI handling — validated against the Latina history."""

    def test_nfl_moneyline_spread_total(self):
        self.assertEqual(cp.classify_event("Ravens vs. Steelers", "STEELERS"), "American Football")
        self.assertEqual(cp.classify_event("Spread: Falcons (-3.5)", "SAINTS"), "American Football")
        self.assertEqual(cp.classify_event("Buccaneers vs. Rams: O/U 49.5", "OVER"), "American Football")

    def test_spread_no_longer_blanket_soccer(self):
        self.assertEqual(cp.classify_event("Spread: Cowboys (-3.5)", "COWBOYS"), "American Football")
        self.assertEqual(cp.classify_event("Spread: France (-1.5)", "FRANCE"), "Soccer")
        self.assertEqual(cp.classify_event("Spread: Chelsea FC (-1.5)", "CHELSEA FC"), "Soccer")

    def test_college_football_vs_basketball_by_date(self):
        self.assertEqual(cp.classify_event("Michigan vs. Texas", "MICHIGAN", 12), "American Football")
        self.assertEqual(cp.classify_event("USC vs. TCU", "USC", 12), "American Football")
        self.assertEqual(cp.classify_event("Connecticut Huskies vs. Duke Blue Devils", "DUKE BLUE DEVILS", 3), "Basketball")
        # FBS school written with its MASCOT => hoops, even in CFB season
        self.assertEqual(cp.classify_event("Michigan Wolverines vs. Auburn Tigers", "MICHIGAN WOLVERINES", 11), "Basketball")

    def test_college_ou_line_magnitude(self):
        self.assertEqual(cp.classify_event("Louisville Cardinals vs. Michigan State: O/U 151.5", "OVER", 11), "Basketball")
        self.assertEqual(cp.classify_event("Boise State vs. Memphis: O/U 52.5", "OVER", 12), "American Football")

    def test_nickname_collisions_resolved(self):
        self.assertEqual(cp.classify_event("Louisville Cardinals vs. Miami Hurricanes", "MIAMI HURRICANES", 3), "Basketball")
        self.assertEqual(cp.classify_event("Spread: Oregon Ducks (-3.5)", "OREGON DUCKS", 3), "Basketball")
        self.assertEqual(cp.classify_event("Spread: Vermont Catamounts (-3.5)", "PRINCETON TIGERS", 12), "Basketball")
        # real NHL/MLB are unaffected
        self.assertEqual(cp.classify_event("Golden Knights vs. Hurricanes: O/U 5.5", "OVER"), "Hockey")
        self.assertEqual(cp.classify_event("Chicago Cubs vs. St. Louis Cardinals", "ST. LOUIS CARDINALS"), "Baseball")

    def test_olympic_hockey_and_nrfi(self):
        self.assertEqual(cp.classify_event("Men's Group C - Germany vs. Latvia", "GERMANY"), "Hockey")
        self.assertEqual(cp.classify_event("NRFI: Texas Rangers vs. Baltimore Orioles", "YES"), "Baseball")
        self.assertEqual(
            cp.classify_event("Will there be a run scored in the first inning?: Minnesota Twins vs. Kansas City Royals",
                              "NO RUN"), "Baseball")

    def test_tennis_and_soccer_unaffected(self):
        self.assertEqual(cp.classify_event("Roland Garros ATP: Frances Tiafoe vs Matteo Arnaldi", "FRANCES TIAFOE"), "Tennis")
        self.assertEqual(cp.classify_event("Will Morocco win on 2026-06-24?", "YES"), "Soccer")
        self.assertEqual(cp.classify_event("Morocco vs. Haiti: O/U 2.5", "OVER"), "Soccer")


if __name__ == "__main__":
    unittest.main(verbosity=2)
