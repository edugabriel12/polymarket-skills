#!/usr/bin/env python3
"""Offline tests for league-baseline calibration (no network)."""

import unittest

import _bootstrap  # noqa: F401

import baselines_source as bsrc
import leagues


def _payload(scores, status="FINISHED"):
    return {"matches": [
        {"status": status, "score": {"fullTime": {"home": h, "away": a}}}
        for (h, a) in scores]}


class TestParse(unittest.TestCase):
    def test_avg_and_count(self):
        # totals 3,2,4 -> avg 3.0 over 3 games
        avg, n = bsrc.avg_goals_from_matches(_payload([(2, 1), (1, 1), (3, 1)]))
        self.assertEqual(n, 3)
        self.assertAlmostEqual(avg, 3.0)

    def test_ignores_unfinished_and_missing(self):
        data = {"matches": [
            {"status": "FINISHED", "score": {"fullTime": {"home": 2, "away": 2}}},
            {"status": "SCHEDULED", "score": {"fullTime": {"home": 5, "away": 5}}},
            {"status": "FINISHED", "score": {"fullTime": {"home": None, "away": 1}}},
        ]}
        avg, n = bsrc.avg_goals_from_matches(data)
        self.assertEqual(n, 1)
        self.assertAlmostEqual(avg, 4.0)

    def test_empty(self):
        self.assertEqual(bsrc.avg_goals_from_matches({"matches": []}), (None, 0))
        self.assertEqual(bsrc.avg_goals_from_matches({}), (None, 0))


class TestCodes(unittest.TestCase):
    def test_fd_code_known_and_alias(self):
        self.assertEqual(bsrc.fd_code("epl"), "PL")
        self.assertEqual(bsrc.fd_code("premier-league"), "PL")
        self.assertEqual(bsrc.fd_code("seriea"), "SA")
        self.assertEqual(bsrc.fd_code("fifwc"), "WC")
        self.assertIsNone(bsrc.fd_code("cs2"))
        self.assertIsNone(bsrc.fd_code(None))

    def test_min_matches_world_cup_lower(self):
        self.assertEqual(bsrc.min_matches("epl"), bsrc.MIN_MATCHES_DEFAULT)
        self.assertLess(bsrc.min_matches("fifwc"), bsrc.MIN_MATCHES_DEFAULT)


class TestBaselineFor(unittest.TestCase):
    def test_uses_calibrated_when_present(self):
        slug = "epl-ars-che-2026-06-14-total-2pt5"
        self.assertAlmostEqual(bsrc.baseline_for(slug, {"epl": 3.31}), 3.31)

    def test_falls_back_to_static(self):
        slug = "epl-ars-che-2026-06-14-total-2pt5"
        self.assertAlmostEqual(bsrc.baseline_for(slug, {}),
                               leagues.LEAGUE_BASELINES["epl"])
        self.assertAlmostEqual(bsrc.baseline_for(slug, None),
                               leagues.LEAGUE_BASELINES["epl"])

    def test_unknown_league_uses_default(self):
        self.assertAlmostEqual(bsrc.baseline_for("zzz-aa-bb-2026-06-14", {}),
                               leagues.DEFAULT_BASELINE)


class TestCalibrateNoNetwork(unittest.TestCase):
    def test_no_token_returns_empty(self):
        # No token -> never touches the network, returns {}.
        self.assertEqual(bsrc.calibrate_baselines(["epl", "seriea"], token=""), {})

    def test_fetch_no_token_or_code_returns_none(self):
        self.assertIsNone(bsrc.fetch_league_baseline("epl", token=""))
        self.assertIsNone(bsrc.fetch_league_baseline("cs2", token="x"))


if __name__ == "__main__":
    unittest.main()
