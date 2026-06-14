#!/usr/bin/env python3
"""Resolve the home park (and its run factor) from a Polymarket MLB game slug.

Polymarket lists a game as `mlb-<away>-<home>-YYYY-MM-DD`
(e.g. `mlb-hou-kc-2026-06-13` => away HOU at home KC). The home team's park sets
the run environment, so park-factor adjustment keys off the HOME abbreviation.

Pure stdlib; uses the static table in ballparks.py. data_inputs.py may override
the returned factor with a live Statcast value.
"""

from __future__ import annotations

import re

import ballparks

_LEAGUE_PREFIXES = ("mlb",)
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse_slug_teams(slug: str) -> tuple[str | None, str | None]:
    """Return (away_abbr, home_abbr) from a game slug, or (None, None).

    Takes the two tokens immediately after the league prefix; tolerant of any
    trailing date / doubleheader suffix.
    """
    if not slug:
        return None, None
    tokens = [t for t in slug.split("-") if t]
    if not tokens:
        return None, None
    has_prefix = tokens[0].lower() in _LEAGUE_PREFIXES
    start = 1 if has_prefix else 0
    teams = [t for t in tokens[start:] if not _DATE_RE.fullmatch(t) and not t.isdigit()]
    if len(teams) < 2:
        return None, None
    away, home = teams[0].lower(), teams[1].lower()
    # Accept only if it looks like a real game slug: has the league prefix, or
    # both tokens resolve to known teams. Avoids misreading arbitrary slugs.
    if not has_prefix and not (ballparks.park_for(away) and ballparks.park_for(home)):
        return None, None
    return away, home


def home_abbr(slug: str) -> str | None:
    """Home team abbreviation from a game slug (second team token)."""
    return parse_slug_teams(slug)[1]


def park_factor_for_slug(slug: str) -> float:
    """Run park factor of the home team's park for a game slug.

    Falls back to the neutral factor (100) when the home team can't be resolved.
    """
    home = home_abbr(slug)
    return ballparks.park_factor(home) if home else ballparks.NEUTRAL_PARK_FACTOR


def home_coords_for_slug(slug: str):
    """(lat, lon) of the home park for a game slug, or None."""
    home = home_abbr(slug)
    return ballparks.coords(home) if home else None
