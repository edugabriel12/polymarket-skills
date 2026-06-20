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

# Slug prefix -> average total goals per game (recent seasons; tunable). Keyed by
# every plausible event-slug prefix Polymarket might use for a league (short code,
# path slug, and descriptive alias), so is_soccer_slug/league_prefix recognize the
# game regardless of which form appears. Baselines for minor leagues are best-effort.
LEAGUE_BASELINES: dict[str, float] = {
    # --- England ---
    "epl": 2.93, "premier-league": 2.93,
    "efl": 2.50, "championship": 2.50, "elc": 2.50,          # Championship
    "eng1": 2.55, "league-one": 2.55,                         # League One
    "eng2": 2.60, "league-two": 2.60, "efl2": 2.60,           # League Two
    # --- Spain ---
    "laliga": 2.62, "la-liga": 2.62,
    "es2": 2.30, "laliga2": 2.30, "segunda": 2.30,            # La Liga 2 / Segunda
    # --- Italy ---
    "seriea": 2.56, "serie-a": 2.56, "sea": 2.56, "bkseriea": 2.56,
    "serieb": 2.45, "it2": 2.45,                              # Serie B (Italy)
    # --- Germany ---
    "bundesliga": 3.14, "bund": 3.14, "ger": 3.14,
    "bundesliga2": 3.05, "ger2": 3.05,                        # 2. Bundesliga
    # --- France ---
    "ligue1": 2.96, "ligue-1": 2.96,
    "ligue2": 2.35, "fr2": 2.35,                              # Ligue 2
    # --- Netherlands / Portugal ---
    "eredivisie": 3.10, "ned": 3.10,
    "primeira": 2.70, "liga-portugal": 2.70, "por": 2.70,
    # --- USA / Mexico ---
    "mls": 3.00,
    "ligamx": 2.75, "liga-mx": 2.75, "mex": 2.75,
    # --- South America ---
    "brasileirao": 2.40, "brasil": 2.40, "bra": 2.40,         # Brasileirão Série A
    "bra2": 2.20, "serie-b": 2.20,                            # Brasileirão Série B
    "argentina": 2.30, "arg": 2.30,
    "colombia": 2.35, "col": 2.35,
    "chile": 2.50, "chi": 2.50,
    "bolivia": 3.10, "bol": 3.10,                             # high altitude -> high-scoring
    "ecuador": 2.40, "ecu": 2.40,
    "peru": 2.50, "per": 2.50,
    "uruguay": 2.40, "uru": 2.40,
    "paraguay": 2.30, "par": 2.30,
    # --- Rest of Europe ---
    "allsvenskan": 3.00, "swe": 3.00,                         # Sweden
    "eliteserien": 3.05, "nor": 3.05,                         # Norway
    "superlig": 2.85, "tur": 2.85,                            # Turkey
    "belgium": 2.95, "bel": 2.95, "jpl": 2.95,
    "spfl": 2.75, "scotland": 2.75, "sco": 2.75,
    "swiss": 3.05, "sui": 3.05,
    "austria": 3.00, "aut": 3.00,
    "denmark": 2.85, "den": 2.85,
    "greece": 2.30, "gre": 2.30,
    "rpl": 2.50, "russia": 2.50,
    "mar1": 2.30, "morocco": 2.30, "botola": 2.30,           # Morocco Botola Pro
    # --- Asia ---
    "csl": 2.80, "china": 2.80,                               # Chinese Super League
    "jleague": 2.75, "j1": 2.75, "jpn": 2.75,                 # Japan J1
    "kleague": 2.55, "k1": 2.55, "kor": 2.55,                 # Korea K1
    "saudi": 2.85, "spl": 2.85, "ksa": 2.85,                  # Saudi Pro League
    # --- Continental clubs ---
    "ucl": 2.90, "champions-league": 2.90, "bkcl": 2.90,
    "uel": 2.85, "europa-league": 2.85,
    "uecl": 2.95, "conference": 2.95,                         # Conference League
    "libertadores": 2.45, "copa-libertadores": 2.45,
    "sudamericana": 2.45, "copa-sudamericana": 2.45,
    # --- International (national teams) ---
    "fifwc": 2.55, "world-cup": 2.55, "wc": 2.55,
    "euro": 2.45, "eur": 2.45,
    "copa": 2.30, "copa-america": 2.30, "conmebol": 2.30,
    "nations-league": 2.80, "unl": 2.80,
    "wcq": 2.80, "wc-qualifiers": 2.80,
    "friendlies": 2.80, "friendly": 2.80,
}
DEFAULT_BASELINE = 2.70

# Competitions played at neutral venues (no home advantage).
NEUTRAL_PREFIXES = {"fifwc", "world-cup", "wc", "euro", "eur", "ucl-final",
                    "copa", "copa-america", "conmebol"}

# Competitions between NATIONAL teams (use national-team Elo, not Club Elo).
INTERNATIONAL_PREFIXES = {"fifwc", "world-cup", "wc", "euro", "eur",
                          "copa", "copa-america", "conmebol", "nations-league",
                          "unl", "wcq", "wc-qualifiers", "friendlies", "friendly"}


def is_international(slug: str) -> bool:
    return (league_prefix(slug) or "") in INTERNATIONAL_PREFIXES

# Prefixes we treat as soccer for discovery (a game slug must start with one).
SOCCER_PREFIXES = tuple(sorted(set(LEAGUE_BASELINES) | NEUTRAL_PREFIXES))

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# League prefix -> Polymarket /sports/<path>/ URL segment (best-effort).
# Slug prefix -> Polymarket /sports/<path>/ segment, which is ALSO the Gamma tag
# slug used for discovery (the distinct values feed SOCCER_TAGS). Confirmed slugs
# come from polymarket.com/sports/<slug>/games URLs; minor leagues are best-effort.
LEAGUE_URL_PATH = {
    # International
    "fifwc": "world-cup", "world-cup": "world-cup", "wc": "world-cup",
    "euro": "euro", "eur": "euro",
    "copa": "copa-america", "copa-america": "copa-america", "conmebol": "copa-america",
    "nations-league": "nations-league", "unl": "nations-league",
    "wcq": "world-cup-qualifiers", "wc-qualifiers": "world-cup-qualifiers",
    "friendlies": "international-friendlies", "friendly": "international-friendlies",
    # England
    "epl": "epl", "premier-league": "epl",
    "efl": "elc", "championship": "elc", "elc": "elc",
    "eng1": "eng1", "league-one": "eng1",
    "eng2": "eng2", "league-two": "eng2", "efl2": "eng2",
    # Spain
    "laliga": "laliga", "la-liga": "laliga",
    "es2": "es2", "laliga2": "es2", "segunda": "es2",
    # Italy
    "seriea": "sea", "serie-a": "sea", "sea": "sea", "bkseriea": "sea",
    "serieb": "it2", "it2": "it2",
    # Germany / France
    "bundesliga": "bundesliga", "bund": "bundesliga", "ger": "bundesliga",
    "bundesliga2": "ger2", "ger2": "ger2",
    "ligue1": "ligue-1", "ligue-1": "ligue-1",
    "ligue2": "fr2", "fr2": "fr2",
    # Netherlands / Portugal
    "eredivisie": "eredivisie", "ned": "eredivisie",
    "primeira": "por", "liga-portugal": "por", "por": "por",
    # Americas
    "mls": "mls",
    "ligamx": "liga-mx", "liga-mx": "liga-mx", "mex": "liga-mx",
    "brasileirao": "bra", "brasil": "bra", "bra": "bra",
    "bra2": "bra2", "serie-b": "bra2",
    "argentina": "argentina", "arg": "argentina",
    "colombia": "colombia", "col": "colombia",
    "chile": "chile", "chi": "chile",
    "bolivia": "bolivia", "bol": "bolivia",
    "ecuador": "ecuador", "ecu": "ecuador",
    "peru": "peru", "per": "peru",
    "uruguay": "uruguay", "uru": "uruguay",
    "paraguay": "paraguay", "par": "paraguay",
    # Rest of Europe
    "allsvenskan": "allsvenskan", "swe": "allsvenskan",
    "eliteserien": "eliteserien", "nor": "eliteserien",
    "superlig": "super-lig", "tur": "super-lig",
    "belgium": "belgium", "bel": "belgium", "jpl": "belgium",
    "spfl": "scotland", "scotland": "scotland", "sco": "scotland",
    "swiss": "swiss", "sui": "swiss",
    "austria": "austria", "aut": "austria",
    "denmark": "denmark", "den": "denmark",
    "greece": "greece", "gre": "greece",
    "rpl": "russia", "russia": "russia",
    "mar1": "mar1", "morocco": "mar1", "botola": "mar1",

    # Asia
    "csl": "csl", "china": "csl",
    "jleague": "j-league", "j1": "j-league", "jpn": "j-league",
    "kleague": "k-league", "k1": "k-league", "kor": "k-league",
    "saudi": "saudi", "spl": "saudi", "ksa": "saudi",
    # Continental clubs
    "ucl": "ucl", "champions-league": "ucl", "bkcl": "ucl",
    "uel": "uel", "europa-league": "uel",
    "uecl": "uecl", "conference": "uecl",
    "libertadores": "libertadores", "copa-libertadores": "libertadores",
    "sudamericana": "sudamericana", "copa-sudamericana": "sudamericana",
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
