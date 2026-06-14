#!/usr/bin/env python3
"""Static ballpark table: team abbreviation -> park name, run park factor, coords.

Pure data, no imports. Used as the offline fallback for park-factor adjustment
(run_distribution.baseline_mu) and to geo-locate parks for the weather adapter.

Park factors are a runs index (100 = league average) and are APPROXIMATE,
multi-year ballpark estimates — they are meant to be overridden by live Statcast
park factors when data_inputs.py can fetch them. Coordinates are stadium
decimal lat/lon. Both are documented as tunable in references/data-sources.md.

Team keys are lowercase abbreviations as they appear in Polymarket game slugs
(e.g. "mlb-hou-kc-2026-06-13" -> away "hou", home "kc"). Common aliases included.
"""

from __future__ import annotations

# abbr -> (park name, run_park_factor, latitude, longitude)
BALLPARKS: dict[str, tuple[str, float, float, float]] = {
    "ari": ("Chase Field", 103.0, 33.4455, -112.0667),
    "atl": ("Truist Park", 101.0, 33.8907, -84.4677),
    "bal": ("Oriole Park at Camden Yards", 102.0, 39.2839, -76.6217),
    "bos": ("Fenway Park", 108.0, 42.3467, -71.0972),
    "chc": ("Wrigley Field", 101.0, 41.9484, -87.6553),  # wind-dependent; see weather
    "cws": ("Guaranteed Rate Field", 102.0, 41.8300, -87.6339),
    "cin": ("Great American Ball Park", 109.0, 39.0975, -84.5069),
    "cle": ("Progressive Field", 99.0, 41.4962, -81.6852),
    "col": ("Coors Field", 118.0, 39.7559, -104.9942),   # altitude; highest run env
    "det": ("Comerica Park", 97.0, 42.3390, -83.0485),
    "hou": ("Minute Maid Park", 101.0, 29.7572, -95.3556),
    "kc": ("Kauffman Stadium", 100.0, 39.0517, -94.4803),
    "laa": ("Angel Stadium", 99.0, 33.8003, -117.8827),
    "lad": ("Dodger Stadium", 98.0, 34.0739, -118.2400),
    "mia": ("loanDepot park", 97.0, 25.7780, -80.2197),
    "mil": ("American Family Field", 102.0, 43.0280, -87.9712),
    "min": ("Target Field", 100.0, 44.9817, -93.2776),
    "nym": ("Citi Field", 96.0, 40.7571, -73.8458),
    "nyy": ("Yankee Stadium", 105.0, 40.8296, -73.9262),
    "oak": ("Oakland Coliseum", 97.0, 37.7516, -122.2005),
    "phi": ("Citizens Bank Park", 103.0, 39.9061, -75.1665),
    "pit": ("PNC Park", 98.0, 40.4469, -80.0057),
    "sd": ("Petco Park", 95.0, 32.7073, -117.1566),
    "sf": ("Oracle Park", 92.0, 37.7786, -122.3893),     # marine layer; suppresses
    "sea": ("T-Mobile Park", 94.0, 47.5914, -122.3325),
    "stl": ("Busch Stadium", 99.0, 38.6226, -90.1928),
    "tb": ("Tropicana Field", 96.0, 27.7683, -82.6534),
    "tex": ("Globe Life Field", 101.0, 32.7473, -97.0847),
    "tor": ("Rogers Centre", 102.0, 43.6414, -79.3894),  # Canada (NWS won't cover)
    "wsh": ("Nationals Park", 100.0, 38.8730, -77.0074),
}

# Aliases -> canonical key (Polymarket / feeds vary on a few abbreviations).
ALIASES: dict[str, str] = {
    "chw": "cws", "sox": "cws",
    "ath": "oak", "ari_dbacks": "ari", "az": "ari",
    "was": "wsh", "was_nats": "wsh", "wsn": "wsh",
    "sfg": "sf", "sdp": "sd", "tbr": "tb", "kcr": "kc",
    "laa_angels": "laa", "ana": "laa",
}

NEUTRAL_PARK_FACTOR = 100.0


def _canon(abbr: str) -> str:
    a = (abbr or "").strip().lower()
    return ALIASES.get(a, a)


def park_for(abbr: str) -> tuple[str, float, float, float] | None:
    """Return (name, park_factor, lat, lon) for a team abbr, or None."""
    return BALLPARKS.get(_canon(abbr))


def park_factor(abbr: str) -> float:
    """Run park factor for a team abbr (NEUTRAL_PARK_FACTOR if unknown)."""
    rec = park_for(abbr)
    return rec[1] if rec else NEUTRAL_PARK_FACTOR


def coords(abbr: str) -> tuple[float, float] | None:
    """(lat, lon) for a team's park, or None if unknown."""
    rec = park_for(abbr)
    return (rec[2], rec[3]) if rec else None
