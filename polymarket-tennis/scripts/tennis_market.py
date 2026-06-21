#!/usr/bin/env python3
"""Identify and parse Polymarket tennis MATCH-WINNER (moneyline) markets.

Works on the per-market shape from the category scanner / category_common (each
market has `question`, `outcomes`, `outcome_prices`, `token_ids`, `slug`,
`event_slug`). Market text is UNTRUSTED (CLAUDE.md rule #5) — only used to classify
and to read player labels, never executed.

A tennis match market is a 2-outcome moneyline where the outcomes are the two PLAYER
names (not Over/Under or Yes/No). We map outcome[i] -> {token, price, player label}.
Surface is inferred from the tournament tag/slug.
"""

from __future__ import annotations

import re

import elo as _elo

# Real Polymarket tennis tag slugs (discovery), best-effort; the distinct values feed
# discovery in suggest_tennis.py. Confirm against per-tag logs on a live run.
TENNIS_TAGS = ("atp", "wta", "tennis", "australian-open", "french-open", "wimbledon",
               "us-open", "atp-masters", "challenger")

# Grand Slam / tournament keyword -> surface. Default is hard court.
SURFACE_KEYWORDS = (
    ("french-open", "clay"), ("roland-garros", "clay"), ("rg", "clay"),
    ("monte-carlo", "clay"), ("madrid", "clay"), ("rome", "clay"), ("clay", "clay"),
    ("wimbledon", "grass"), ("queens", "grass"), ("halle", "grass"), ("grass", "grass"),
    ("australian-open", "hard"), ("us-open", "hard"), ("hard", "hard"),
)

# Outcome labels that mark a market as NOT a moneyline (over/under, yes/no props).
_NON_MONEYLINE = ("over", "under", "yes", "no")

# Slug markers of NON-match-winner markets (handicaps, spreads, set/game props, doubles).
# The base match-winner slug ends with the date and carries none of these.
_PROP_MARKERS = ("handicap", "spread", "-total-", "set-betting", "correct-score",
                 "-games-", "-sets-", "to-win-a-set", "tie-break", "tiebreak",
                 "first-set", "exact-score")
_DOUBLES_MARKERS = ("doubles", "/")

_SUFFIX_RE = re.compile(r"-(?:winner|moneyline|ml|h2h|match-winner)$")
_DATE_END_RE = re.compile(r"-\d{4}-\d{2}-\d{2}$")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def base_match_slug(slug: str) -> str:
    """Strip a trailing market-type suffix to the base match-event slug."""
    return _SUFFIX_RE.sub("", slug or "")


def is_doubles(market: dict) -> bool:
    """Doubles match (singles Elo doesn't apply): slug says 'doubles' or names have '/'."""
    blob = (market.get("slug", "") + " " + " ".join(market.get("outcomes") or [])).lower()
    return any(mk in blob for mk in _DOUBLES_MARKERS)


def is_moneyline_slug(slug: str) -> bool:
    """True only for a base SINGLES match-winner slug: '<tag>-<a>-<b>-YYYY-MM-DD' with no
    prop suffix (handicap/spread/set-betting/...) and not doubles."""
    s = (slug or "").lower()
    if any(mk in s for mk in _DOUBLES_MARKERS) or any(p in s for p in _PROP_MARKERS):
        return False
    return bool(_DATE_END_RE.search(base_match_slug(s)))


def surface_for(slug: str, tag: str | None = None) -> str:
    """Infer the court surface from the slug/tag; default hard."""
    blob = f"{slug or ''} {tag or ''}".lower()
    for kw, surf in SURFACE_KEYWORDS:
        if kw in blob:
            return surf
    return "hard"


def _norm_label(text: str) -> str:
    return (text or "").strip().lower()


def is_match_market(market: dict) -> bool:
    """True only for a base SINGLES match-winner moneyline: 2 player-label outcomes AND a
    base match slug (no handicap/spread/set prop, no doubles)."""
    outs = market.get("outcomes") or []
    toks = market.get("token_ids") or []
    if len(outs) != 2 or len(toks) != 2:
        return False
    labels = [_norm_label(o) for o in outs]
    if any(not lbl for lbl in labels) or labels[0] == labels[1]:
        return False
    # Reject prop markets: over/under or yes/no (label is or starts with those words).
    if any(lbl == w or lbl.startswith(w + " ") for lbl in labels for w in _NON_MONEYLINE):
        return False
    # Reject handicap/spread/set props and doubles via the slug.
    return is_moneyline_slug(market.get("slug", ""))


def _price(prices, i):
    try:
        return float(prices[i])
    except (IndexError, TypeError, ValueError):
        return None


def match_sides(market: dict) -> dict | None:
    """Map a moneyline market to its two sides.

    Returns {sides: [{label, token, price}, {label, token, price}], book_sum,
    price_sane} with sides ordered as the market lists them, or None if unusable.
    """
    outs = market.get("outcomes") or []
    toks = market.get("token_ids") or []
    prices = market.get("outcome_prices") or []
    if len(outs) != 2 or len(toks) != 2:
        return None
    sides = []
    for i in range(2):
        sides.append({"label": str(outs[i]).strip(),
                      "token": str(toks[i]),
                      "price": _price(prices, i)})
    pa, pb = sides[0]["price"], sides[1]["price"]
    book = (pa or 0) + (pb or 0)
    return {"sides": sides, "book_sum": round(book, 4),
            "price_sane": (0.90 <= book <= 1.10) if (pa and pb) else False}


def parse_players(slug: str) -> tuple[str | None, str | None]:
    """Best-effort: read the two player tokens from a match slug.

    Expects '<tag>-<a>-<b>-YYYY-MM-DD[...]' (e.g. 'atp-alcaraz-sinner-2026-06-20').
    Returns (player_a, player_b) tokens, or (None, None) if it can't be parsed.
    """
    if not slug:
        return None, None
    s = base_match_slug(slug).lower()
    s = _DATE_RE.split(s)[0].strip("-")          # drop the date and anything after
    toks = [t for t in s.split("-") if t]
    if len(toks) < 3:
        return None, None
    # toks[0] is the tag/tournament prefix; the next two are the players (best-effort).
    return toks[1], toks[2]
