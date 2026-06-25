#!/usr/bin/env python3
"""Offline tests for the strength-source resolvers (no network)."""

import unittest

import _bootstrap  # noqa: F401

import ratings_sources as rs


class TestClubEloNameCandidates(unittest.TestCase):
    """Club Elo endpoint names: curated alias first, then a name-derived guess for clubs the
    alias map doesn't list yet — so more European leagues resolve from the full club name."""

    def test_alias_first(self):
        # A mapped code yields its curated Club Elo name.
        self.assertEqual(rs.clubelo_name_candidates("ars", None)[0], "Arsenal")

    def test_name_derived_when_alias_missing(self):
        # Unknown code + a full name -> a CamelCase spaceless Club Elo endpoint guess.
        self.assertEqual(rs.clubelo_name_candidates("zzz", "Augsburg"), ["Augsburg"])
        self.assertEqual(rs.clubelo_name_candidates("zzz", "bayer leverkusen"),
                         ["BayerLeverkusen"])

    def test_alias_and_name_both_tried(self):
        cands = rs.clubelo_name_candidates("ars", "Arsenal FC")
        self.assertEqual(cands[0], "Arsenal")          # alias wins first
        self.assertIn("ArsenalFc", cands)              # name guess also offered

    def test_empty_when_nothing_resolves(self):
        self.assertEqual(rs.clubelo_name_candidates("zzz", None), [])
        self.assertEqual(rs.clubelo_name_candidates(None, ""), [])


class TestNationalElo(unittest.TestCase):
    def test_unknown_code_is_none(self):
        self.assertIsNone(rs.national_elo("zzz"))
        self.assertIsNone(rs.national_elo(None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
