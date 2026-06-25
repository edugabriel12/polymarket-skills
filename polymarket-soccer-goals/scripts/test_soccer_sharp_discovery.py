#!/usr/bin/env python3
"""Offline tests for sharp-source-driven soccer discovery (no network).

Covers the coverage fix: the volume-ranked Gamma tag truncates low-volume leagues
(e.g. Brazilian Série B), so the sharp slate is used as the authoritative game list and
each missed game is fetched by event slug. Validates the slug construction (national FIFA
codes vs club prefixes, both orderings), the league->prefix map, and the recover/skip/
not-found behavior of discover_from_sharp against a fake Gamma API.

Run: python polymarket-soccer-goals/scripts/test_soccer_sharp_discovery.py
"""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
import contextlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import soccer_sharp_discovery as sd  # noqa: E402
from sharp_odds_soccer import _key  # noqa: E402


def _market(slug, outcomes, prices):
    return {"slug": slug, "question": f"{slug}: O/U",
            "outcomes": json.dumps(outcomes), "outcomePrices": json.dumps(prices),
            "clobTokenIds": json.dumps(["t1", "t2"]), "conditionId": "c", "active": True,
            "acceptingOrders": True}


class _FakeAPI:
    """Serves a fixed {event_slug: [Gamma market rows]} map via Gamma /events?slug=."""

    def __init__(self, events_by_slug):
        self.events_by_slug = events_by_slug
        self.slugs_queried = []

    def get(self, url, params=None):
        slug = (params or {}).get("slug")
        self.slugs_queried.append(slug)
        markets = self.events_by_slug.get(slug)
        if markets is None:
            return []
        return [{"slug": slug, "markets": markets}]


class TestPrefixForLeague(unittest.TestCase):
    def test_mapped_keys(self):
        self.assertEqual(sd.prefix_for_league("soccer_brazil_serie_b"), "bra2")
        self.assertEqual(sd.prefix_for_league("soccer_fifa_world_cup"), "fifwc")
        self.assertEqual(sd.prefix_for_league("soccer_epl"), "epl")

    def test_unmapped_falls_back_to_slugified_tail(self):
        self.assertEqual(sd.prefix_for_league("soccer_narnia_premier"), "narnia-premier")
        self.assertIsNone(sd.prefix_for_league(None))


class TestTeamTokens(unittest.TestCase):
    def test_national_code_first(self):
        self.assertEqual(sd.team_tokens("Germany")[0], "ger")
        self.assertEqual(sd.team_tokens("Netherlands")[0], "nld")    # not a truncation
        self.assertEqual(sd.team_tokens("Cote DIvoire")[0], "civ")

    def test_club_prefix_and_full(self):
        toks = sd.team_tokens("Cuiaba")
        self.assertIn("cui", toks)
        self.assertIn("cuiaba", toks)

    def test_empty(self):
        self.assertEqual(sd.team_tokens(""), [])


class TestCandidateSlugs(unittest.TestCase):
    def test_both_orderings_and_prefix(self):
        slugs = sd.candidate_event_slugs("bra2", "cuiaba", "londrina", "2026-06-25")
        self.assertIn("bra2-cui-lon-2026-06-25", slugs)
        self.assertIn("bra2-lon-cui-2026-06-25", slugs)
        self.assertLessEqual(len(slugs), sd._MAX_CANDIDATES)
        self.assertEqual(len(slugs), len(set(slugs)))        # de-duplicated

    def test_no_prefix_no_slugs(self):
        self.assertEqual(sd.candidate_event_slugs("", "a", "b", "2026-06-25"), [])


class TestFetchEventMarkets(unittest.TestCase):
    def test_parses_and_sets_event_slug(self):
        api = _FakeAPI({"bra2-cui-lon-2026-06-25-total-2pt5":
                        [_market("bra2-cui-lon-2026-06-25-total-2pt5",
                                 ["Over 2.5", "Under 2.5"], ["0.5", "0.5"])]})
        out = sd.fetch_event_markets(api, "bra2-cui-lon-2026-06-25-total-2pt5")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["slug"], "bra2-cui-lon-2026-06-25-total-2pt5")
        self.assertEqual(out[0]["event_slug"], "bra2-cui-lon-2026-06-25-total-2pt5")
        self.assertEqual(out[0]["token_ids"], ["t1", "t2"])

    def test_miss_returns_empty(self):
        self.assertEqual(sd.fetch_event_markets(_FakeAPI({}), "nope"), [])


class TestDiscoverFromSharp(unittest.TestCase):
    def _lookup(self):
        # One Série B game in the sharp slate, carrying its odds-api league key.
        return {_key("2026-06-25", "Cuiaba", "Londrina"):
                {"total_line": 2.5, "over_fair": 0.5, "under_fair": 0.5,
                 "home": "cuiaba", "away": "londrina", "league": "soccer_brazil_serie_b"}}

    def test_recovers_truncated_game(self):
        api = _FakeAPI({"bra2-cui-lon-2026-06-25":
                        [_market("bra2-cui-lon-2026-06-25-total-2pt5",
                                 ["Over 2.5", "Under 2.5"], ["0.55", "0.45"])]})
        out = sd.discover_from_sharp(api, self._lookup(), "2026-06-25", existing_team_sets=set())
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["slug"].startswith("bra2-cui-lon-2026-06-25-total"))

    def test_skips_already_discovered_game(self):
        api = _FakeAPI({"bra2-cui-lon-2026-06-25": [_market("x", ["Over"], ["0.5"])]})
        existing = {frozenset({"cuiaba", "londrina"})}
        out = sd.discover_from_sharp(api, self._lookup(), "2026-06-25", existing_team_sets=existing)
        self.assertEqual(out, [])
        self.assertEqual(api.slugs_queried, [])            # never probed

    def test_not_found_is_logged(self):
        api = _FakeAPI({})                                  # Polymarket has nothing
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            out = sd.discover_from_sharp(api, self._lookup(), "2026-06-25",
                                         existing_team_sets=set(),
                                         vlog=lambda m: print(m, file=sys.stderr))
        self.assertEqual(out, [])
        log = buf.getvalue()
        self.assertIn("NOT FOUND on Polymarket", log)
        self.assertIn("prefix=bra2", log)
        self.assertTrue(api.slugs_queried)                  # it DID try candidate slugs

    def test_other_date_ignored(self):
        api = _FakeAPI({"bra2-cui-lon-2026-06-25": [_market("x", ["Over"], ["0.5"])]})
        out = sd.discover_from_sharp(api, self._lookup(), "2026-06-26", existing_team_sets=set())
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
