#!/usr/bin/env python3
"""Soccer league baselines + game-slug parsing for Polymarket.

Pure stdlib. Provides: which slug prefixes are soccer, each league's average
total goals/game (sets the model's baseline total), whether a competition is
played at a neutral venue (no home advantage), and how to read the two teams
from a game slug.

Polymarket lists sports games as `<league>-<a>-<b>-YYYY-MM-DD[...]`. The two team
abbreviations follow the league prefix. We treat the FIRST team token as home and
the SECOND as away by default (`home_first=True`); for neutral competitions the
home/away distinction is ignored (home advantage = 0). Both are configurable in
the pipeline since Polymarket's ordering convention is not guaranteed.
"""

from __future__ import annotations

import re

# league prefix -> average total goals per game (recent seasons; tunable).
LEAGUE_BASELINES: dict[str, float] = {
    "epl": 2.93, "premier-league": 2.93,
    "laliga": 2.62, "la-liga": 2.62,
    "seriea": 2.56, "serie-a": 2.56,
    "bundesliga": 3.14,
    "ligue1": 2.96, "ligue-1": 2.96,
    "eredivisie": 3.10,
    "primeira": 2.70, "liga-portugal": 2.70,
    "mls": 3.00,
    "ucl": 2.90, "champions-league": 2.90,
    "uel": 2.85, "europa-league": 2.85,
    "fifwc": 2.55, "world-cup": 2.55, "wc": 2.55,
    "eur'": 2.45, "euro": 2.45,
    "brasileirao": 2.30, "brasil": 2.30,
}
DEFAULT_BASELINE = 2.70

# Competitions played at neutral venues (no home advantage).
NEUTRAL_PREFIXES = {"fifwc", "world-cup", "wc", "euro", "eur", "ucl-final"}

# Prefixes we treat as soccer for discovery (a game slug must start with one).
SOCCER_PREFIXES = tuple(sorted(set(LEAGUE_BASELINES) | NEUTRAL_PREFIXES))

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# League prefix -> Polymarket /sports/<path>/ URL segment (best-effort).
LEAGUE_URL_PATH = {
    "fifwc": "world-cup", "world-cup": "world-cup", "wc": "world-cup",
    "euro": "euro", "eur": "euro",
    "epl": "epl", "premier-league": "epl",
    "laliga": "la-liga", "la-liga": "la-liga",
    "seriea": "serie-a", "serie-a": "serie-a",
    "bundesliga": "bundesliga", "ligue1": "ligue-1", "ligue-1": "ligue-1",
    "ucl": "champions-league", "champions-league": "champions-league",
    "uel": "europa-league", "mls": "mls", "eredivisie": "eredivisie",
}

# Strip a market suffix ("-total-2pt5", "-btts", "-spread-...") to the base game slug.
_MARKET_SUFFIX_RE = re.compile(
    r"-(?:total-\d{1,2}(?:pt5)?|btts|both-teams-to-score|gg|spread-[a-z0-9-]+)$")


def base_game_slug(slug: str) -> str:
    return _MARKET_SUFFIX_RE.sub("", slug or "")


def game_url(slug: str) -> str:
    """Direct Polymarket link: /sports/<path>/<base-game> when the league is known,
    else the canonical /event/<base-game>."""
    base = base_game_slug(slug)
    path = LEAGUE_URL_PATH.get(league_prefix(slug) or "")
    if path:
        return f"https://polymarket.com/sports/{path}/{base}"
    return f"https://polymarket.com/event/{base}"


def league_prefix(slug: str) -> str | None:
    """Return the league prefix of a game slug (the part before the first team)."""
    if not slug:
        return None
    tokens = slug.lower().split("-")
    # Try the longest matching known multi-token prefix first.
    for size in (3, 2, 1):
        cand = "-".join(tokens[:size])
        if cand in LEAGUE_BASELINES or cand in NEUTRAL_PREFIXES:
            return cand
    return tokens[0] if tokens else None


def is_soccer_slug(slug: str) -> bool:
    p = league_prefix(slug)
    return bool(p) and (p in LEAGUE_BASELINES or p in NEUTRAL_PREFIXES
                        or any(slug.lower().startswith(sp + "-") for sp in SOCCER_PREFIXES))


def league_baseline(slug: str) -> float:
    p = league_prefix(slug)
    return LEAGUE_BASELINES.get(p or "", DEFAULT_BASELINE)


def is_neutral(slug: str) -> bool:
    return (league_prefix(slug) or "") in NEUTRAL_PREFIXES


def parse_teams(slug: str, home_first: bool = True) -> tuple[str | None, str | None]:
    """Return (home_abbr, away_abbr) from a game slug, or (None, None).

    Takes the two team tokens immediately after the league prefix.
    """
    p = league_prefix(slug)
    if not p:
        return None, None
    rest = slug.lower()[len(p):].strip("-")
    tokens = [t for t in rest.split("-") if t and not _DATE_RE.fullmatch(t) and not t.isdigit()]
    if len(tokens) < 2:
        return None, None
    a, b = tokens[0], tokens[1]
    return (a, b) if home_first else (b, a)
