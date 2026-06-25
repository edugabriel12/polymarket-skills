#!/usr/bin/env python3
"""Offline tests for the API-Football adapter (no network)."""

import unittest

import _bootstrap  # noqa: F401

import apifootball_source as apif


def _standings(rows):
    """Build an API-Football /standings payload from [(name, gf, ga, played)]."""
    return {"response": [{"league": {"standings": [[
        {"team": {"name": n}, "all": {"played": p, "goals": {"for": gf, "against": ga}}}
        for (n, gf, ga, p) in rows]]}}]}


class TestSeason(unittest.TestCase):
    def test_calendar_year_league(self):
        self.assertEqual(apif.season_for("bra2", "2026-06-14"), 2026)

    def test_cross_year_league(self):
        self.assertEqual(apif.season_for("epl", "2026-06-14"), 2025)  # June -> prior season
        self.assertEqual(apif.season_for("epl", "2026-09-14"), 2026)  # Sept -> new season


class TestMatchTeam(unittest.TestCase):
    def setUp(self):
        self.names = ["Novorizontino", "Nautico", "Red Bull Bragantino", "Vila Nova"]

    def test_unique_prefix(self):
        self.assertEqual(apif.match_team("nov", self.names), "Novorizontino")
        self.assertEqual(apif.match_team("nau", self.names), "Nautico")  # accent-insensitive

    def test_acronym(self):
        self.assertEqual(apif.match_team("rbb", self.names), "Red Bull Bragantino")

    def test_ambiguous_prefix_returns_none(self):
        self.assertIsNone(apif.match_team("n", ["Nautico", "Novorizontino"]))

    def test_no_match_returns_none(self):
        self.assertIsNone(apif.match_team("xyz", self.names))
        self.assertIsNone(apif.match_team("", self.names))


class TestTableAndInputs(unittest.TestCase):
    def test_table_and_league_avg(self):
        rows = apif.parse_standings(_standings([
            ("Alpha", 20, 10, 10), ("Beta", 10, 20, 10)]))
        table, avg = apif.table_from_rows(rows, min_played=5)
        self.assertAlmostEqual(table["Alpha"]["gf_pg"], 2.0)
        self.assertAlmostEqual(table["Alpha"]["ga_pg"], 1.0)
        self.assertAlmostEqual(avg, 1.5)  # 30 goals / 20 team-games

    def test_too_few_matches_rejected(self):
        rows = apif.parse_standings(_standings([("Alpha", 4, 2, 2), ("Beta", 2, 4, 2)]))
        self.assertEqual(apif.table_from_rows(rows, min_played=5), (None, None))

    def test_compute_inputs_supremacy_direction(self):
        # Strong attack/weak defense (Alpha) at home vs weak/leaky (Beta).
        rows = apif.parse_standings(_standings([
            ("Alpha", 20, 8, 10), ("Beta", 8, 20, 10)]))
        table, avg = apif.table_from_rows(rows)
        out = apif.compute_inputs(table, avg, "alp", "bet")
        self.assertIn("total_xg", out)
        self.assertGreater(out["supremacy_xg"], 0.0)        # home favored
        self.assertGreater(out["total_xg"], 0.0)
        self.assertEqual(out["_resolved"], ["Alpha", "Beta"])

    def test_home_tilt_creates_edge_for_equal_teams(self):
        rows = apif.parse_standings(_standings([("Alpha", 15, 15, 10), ("Beta", 15, 15, 10)]))
        table, avg = apif.table_from_rows(rows)
        out = apif.compute_inputs(table, avg, "alp", "bet")
        self.assertGreater(out["supremacy_xg"], 0.0)        # home tilt only
        self.assertLess(out["supremacy_xg"], 0.5)

    def test_unresolved_team_returns_empty(self):
        rows = apif.parse_standings(_standings([("Alpha", 15, 15, 10), ("Beta", 15, 15, 10)]))
        table, avg = apif.table_from_rows(rows)
        self.assertEqual(apif.compute_inputs(table, avg, "zzz", "bet"), {})

    def test_league_baseline_is_twice_team_avg(self):
        rows = apif.parse_standings(_standings([("Alpha", 20, 10, 10), ("Beta", 10, 20, 10)]))
        _table, avg = apif.table_from_rows(rows)
        self.assertAlmostEqual(2.0 * avg, 3.0)              # avg total goals/game


class TestSerieBWiring(unittest.TestCase):
    """The Série B (bra2) strength path is fully wired — only APIFOOTBALL_KEY is needed."""

    def test_bra2_league_id_and_season(self):
        self.assertEqual(apif.api_league_id("bra2"), 72)
        self.assertEqual(apif.season_for("bra2", "2026-06-25"), 2026)   # calendar-year league

    def test_cuiaba_londrina_resolve_to_inputs(self):
        rows = apif.parse_standings(_standings([
            ("Cuiabá", 14, 10, 12), ("Londrina", 9, 13, 12),
            ("Goiás", 18, 8, 12), ("Novorizontino", 11, 11, 12)]))
        table, avg = apif.table_from_rows(rows, min_played=1)
        self.assertEqual(apif.match_team("cui", table.keys()), "Cuiabá")
        self.assertEqual(apif.match_team("lon", table.keys()), "Londrina")
        out = apif.compute_inputs(table, avg, "cui", "lon")
        self.assertTrue(out)                                # external=True would fire
        self.assertGreater(out["supremacy_xg"], 0)          # Cuiabá (home, better) favored


class TestNoNetwork(unittest.TestCase):
    def test_team_inputs_no_key(self):
        self.assertEqual(apif.team_inputs("nov", "nau", "bra2", "2026-06-14", key=""), {})

    def test_missing_key_warns_once_per_league(self):
        import io, os, contextlib
        env = os.environ.pop("APIFOOTBALL_KEY", None)
        apif._KEY_WARNED.discard("bra2")
        try:
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                apif.team_inputs("cui", "lon", "bra2", "2026-06-25", key="")
                apif.team_inputs("cui", "lon", "bra2", "2026-06-25", key="")   # 2nd: no repeat
            log = buf.getvalue()
            self.assertIn("APIFOOTBALL_KEY not set", log)
            self.assertIn("bra2", log)
            self.assertEqual(log.count("APIFOOTBALL_KEY not set"), 1)           # deduped
        finally:
            if env is not None:
                os.environ["APIFOOTBALL_KEY"] = env

    def test_team_inputs_unknown_league(self):
        self.assertEqual(apif.team_inputs("aa", "bb", "cs2", "2026-06-14", key="x"), {})

    def test_league_baseline_no_key(self):
        self.assertIsNone(apif.league_baseline("bra2", "2026-06-14", key=""))


if __name__ == "__main__":
    unittest.main()
