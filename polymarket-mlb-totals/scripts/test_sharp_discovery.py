#!/usr/bin/env python3
"""Offline tests for sharp-source-driven MLB discovery (no network).

Covers the two bugs it fixes:
  - MATCHING: sharp team identifiers (abbrev OR full name) normalize to the
    Polymarket slug abbreviation, so a sharp game keys identically to its market.
  - COVERAGE: the sharp slate becomes the authoritative game list; each game's
    Polymarket markets are fetched by event slug (both home/away orderings tried).

Run: python polymarket-mlb-totals/scripts/test_sharp_discovery.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sharp_odds as so  # noqa: E402
import sharp_discovery as sd  # noqa: E402


class _FakeAPI:
    """Stand-in APIClient: serves a fixed {event_slug: [raw market rows]} map."""

    def __init__(self, events_by_slug):
        self.events_by_slug = events_by_slug
        self.calls = []

    def get(self, url, params=None):
        params = params or {}
        slug = params.get("slug")
        self.calls.append(slug)
        markets = self.events_by_slug.get(slug)
        if markets is None:
            return []
        return [{"slug": slug, "markets": markets}]


def _raw_total(slug, line_suffix):
    """A raw Gamma market row (pre-parse) for a game-total market."""
    return {
        "slug": f"{slug}-total-{line_suffix}", "question": "Total runs O/U",
        "outcomes": '["Over", "Under"]', "outcomePrices": '["0.5", "0.5"]',
        "clobTokenIds": '["t1", "t2"]', "conditionId": f"0x{slug}",
        "volume24hr": "1500", "active": True, "acceptingOrders": True,
    }


class TestTeamNormalization(unittest.TestCase):
    def test_abbreviations_pass_through(self):
        self.assertEqual(so.normalize_team("CHC"), "chc")
        self.assertEqual(so.normalize_team("nym"), "nym")

    def test_full_names_map_to_abbrev(self):
        self.assertEqual(so.normalize_team("Chicago Cubs"), "chc")
        self.assertEqual(so.normalize_team("New York Mets"), "nym")
        self.assertEqual(so.normalize_team("Athletics"), "oak")
        self.assertEqual(so.normalize_team("St. Louis Cardinals"), "stl")

    def test_aliases(self):
        self.assertEqual(so.normalize_team("CHW"), "cws")   # ballparks alias
        self.assertEqual(so.normalize_team("AZ"), "ari")

    def test_key_matches_across_sources(self):
        # A CSV game keyed from abbrevs and an Odds-API game keyed from full names
        # must produce the SAME lookup key (this is what fixes "0 of N matched").
        self.assertEqual(so._key("2026-06-23", "CHC", "NYM"),
                         so._key("2026-06-23", "Chicago Cubs", "New York Mets"))
        # And both match what parse_slug_teams yields from a Polymarket slug.
        self.assertEqual(so._key("2026-06-23", "chc", "nym"),
                         so._key("2026-06-23", "Chicago Cubs", "New York Mets"))


class TestSlugConstruction(unittest.TestCase):
    def test_both_orderings(self):
        self.assertEqual(sd.candidate_event_slugs("chc", "nym", "2026-06-23"),
                         ["mlb-chc-nym-2026-06-23", "mlb-nym-chc-2026-06-23"])

    def test_games_from_lookup_dated_only(self):
        lookup = {
            so._key("2026-06-23", "CHC", "NYM"): {"line": 9.0},
            so._key("2026-06-23", "PHI", "WSH"): {"line": 8.5},
            so._key("2026-06-24", "SEA", "OAK"): {"line": 7.5},
        }
        games = sd.games_from_lookup(lookup, "2026-06-23")
        self.assertEqual(len(games), 2)
        self.assertIn(("chc", "nym"), games)
        self.assertIn(("phi", "wsh"), games)


class TestDiscoverFromSharp(unittest.TestCase):
    def test_recovers_full_slate_and_tries_both_orderings(self):
        # Sharp slate has 2 games. Polymarket lists chc-nym as away-home (first ordering
        # hits) and wsh-phi reversed (so the SECOND ordering must be tried).
        events = {
            "mlb-chc-nym-2026-06-23": [_raw_total("mlb-chc-nym-2026-06-23", "9")],
            "mlb-wsh-phi-2026-06-23": [_raw_total("mlb-wsh-phi-2026-06-23", "8pt5")],
        }
        api = _FakeAPI(events)
        lookup = {
            so._key("2026-06-23", "Chicago Cubs", "New York Mets"): {"line": 9.0},
            so._key("2026-06-23", "PHI", "WSH"): {"line": 8.5},
        }
        markets = sd.discover_from_sharp(api, lookup, "2026-06-23")
        # event_slug is the total MARKET's own slug (encodes the line) so the downstream
        # `-total-` filter keeps it.
        slugs = sorted(m["event_slug"] for m in markets)
        self.assertEqual(len(markets), 2)
        self.assertIn("mlb-chc-nym-2026-06-23-total-9", slugs)
        self.assertIn("mlb-wsh-phi-2026-06-23-total-8pt5", slugs)
        # phi-wsh ordering was tried and missed, then wsh-phi tried and hit.
        self.assertIn("mlb-phi-wsh-2026-06-23", api.calls)
        self.assertIn("mlb-wsh-phi-2026-06-23", api.calls)

    def test_total_market_slug_survives_run_total_filter(self):
        # Regression: the event_slug must carry the -total-<line> suffix, or the run-total
        # filter (-\d{4}-\d{2}-\d{2}-total-\d) drops every sharp-discovered total.
        import re
        events = {"mlb-bal-laa-2026-06-23": [_raw_total("mlb-bal-laa-2026-06-23", "8pt5")]}
        lookup = {so._key("2026-06-23", "bal", "laa"): {"line": 8.5}}
        m = sd.discover_from_sharp(_FakeAPI(events), lookup, "2026-06-23")[0]
        self.assertRegex(m["event_slug"], r"-\d{4}-\d{2}-\d{2}-total-\d{1,2}(?:pt5)?$")

    def test_markets_are_parsed_shape(self):
        events = {"mlb-chc-nym-2026-06-23": [_raw_total("mlb-chc-nym-2026-06-23", "9")]}
        api = _FakeAPI(events)
        lookup = {so._key("2026-06-23", "chc", "nym"): {"line": 9.0}}
        m = sd.discover_from_sharp(api, lookup, "2026-06-23")[0]
        self.assertEqual(m["event_slug"], "mlb-chc-nym-2026-06-23-total-9")
        self.assertEqual(m["token_ids"], ["t1", "t2"])
        self.assertEqual(m["outcomes"], ["Over", "Under"])
        self.assertTrue(m["slug"].endswith("-total-9"))

    def test_missing_game_is_skipped(self):
        api = _FakeAPI({})   # Polymarket has nothing
        lookup = {so._key("2026-06-23", "chc", "nym"): {"line": 9.0}}
        self.assertEqual(sd.discover_from_sharp(api, lookup, "2026-06-23"), [])

    def test_empty_lookup_no_calls(self):
        api = _FakeAPI({"mlb-chc-nym-2026-06-23": []})
        self.assertEqual(sd.discover_from_sharp(api, {}, "2026-06-23"), [])
        self.assertEqual(api.calls, [])


class TestEndToEndMatch(unittest.TestCase):
    def test_discovered_game_resolves_its_sharp_ref(self):
        # The whole point: a game discovered FROM the sharp slate must then resolve a
        # non-None sharp P(Over) via sharp_over_prob keyed off the Polymarket slug teams.
        import park_factors as pf
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.csv")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("date,away,home,total_line,over_odds,under_odds\n")
                fh.write("2026-06-23,CHC,NYM,9.0,-110,-110\n")
            lookup = so.load_sharp_csv(p)
        events = {"mlb-chc-nym-2026-06-23": [_raw_total("mlb-chc-nym-2026-06-23", "9")]}
        markets = sd.discover_from_sharp(_FakeAPI(events), lookup, "2026-06-23")
        slug = markets[0]["event_slug"]
        away, home = pf.parse_slug_teams(slug)
        self.assertEqual((away, home), ("chc", "nym"))
        sharp_over = so.sharp_over_prob(lookup, "2026-06-23", away, home, 9.0)
        self.assertAlmostEqual(sharp_over, 0.5, places=6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
