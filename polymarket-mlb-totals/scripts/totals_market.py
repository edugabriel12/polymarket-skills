#!/usr/bin/env python3
"""Identify and parse the 'total runs' Over/Under market within an MLB game.

Works on either the grouped game `markets[]` entries from list_games_today.py or
the richer `parse_market` dicts from category_common — both expose `question`,
`outcomes`, `outcome_prices`, `token_ids`. Market text is already sanitized
upstream and is treated as untrusted data (CLAUDE.md rule #5).
"""

from __future__ import annotations

import re

# Match a 1-2 digit number (optionally .5) that is NOT embedded in a longer
# digit run, so a year like "2026" or a slug date does not parse as a line.
_LINE_RE = re.compile(r"(?<!\d)(\d{1,2}(?:\.5)?)(?!\d)")
_MIN_LINE, _MAX_LINE = 3.0, 18.0


def _outcome_kind(text: str) -> str | None:
    """Classify an outcome string as 'over', 'under', or None."""
    t = (text or "").strip().lower()
    if t.startswith("over") or t == "o":
        return "over"
    if t.startswith("under") or t == "u":
        return "under"
    return None


def is_totals_market(market: dict) -> bool:
    """True iff the market is an Over/Under pair (one 'over' + one 'under')."""
    outcomes = market.get("outcomes") or []
    if len(outcomes) != 2:
        return False
    kinds = {_outcome_kind(o) for o in outcomes}
    return kinds == {"over", "under"}


def find_totals_market(game: dict) -> dict | None:
    """Return the Over/Under total-runs sub-market of a game, or None.

    If multiple totals markets exist (alternate lines), returns the highest-volume
    one; callers may inspect `all_totals_markets` for the rest.
    """
    candidates = [m for m in (game.get("markets") or []) if is_totals_market(m)]
    if not candidates:
        return None
    candidates.sort(key=lambda m: float(m.get("volume_24h") or 0), reverse=True)
    return candidates[0]


def all_totals_markets(game: dict) -> list[dict]:
    """All Over/Under total-runs sub-markets of a game (possibly alternate lines)."""
    return [m for m in (game.get("markets") or []) if is_totals_market(m)]


def parse_total_line(market: dict) -> float | None:
    """Extract the numeric total line (e.g. 8.5) from outcome/question text.

    Prefers the number embedded in an outcome ("Over 8.5"), falling back to the
    question. Returns None if no plausible line in [3, 20] is found.
    """
    for outcome in market.get("outcomes") or []:
        line = _first_plausible(outcome)
        if line is not None:
            return line
    return _first_plausible(market.get("question", ""))


def _first_plausible(text: str) -> float | None:
    for raw in _LINE_RE.findall(text or ""):
        try:
            val = float(raw)
        except ValueError:
            continue
        if _MIN_LINE <= val <= _MAX_LINE:
            return val
    return None


def over_under_tokens(market: dict) -> dict | None:
    """Map the Over/Under outcomes to their token ids and prices.

    Returns {over_token, under_token, over_price, under_price, book_sum,
    price_sane}. `price_sane` is False when over+under is far from 1.0 (stale or
    mis-mapped book). Returns None if the market is not a valid Over/Under pair
    or token ids are missing.
    """
    outcomes = market.get("outcomes") or []
    token_ids = market.get("token_ids") or []
    prices = market.get("outcome_prices") or []
    if len(outcomes) != 2 or len(token_ids) != 2:
        return None

    idx = {}
    for i, o in enumerate(outcomes):
        kind = _outcome_kind(o)
        if kind:
            idx[kind] = i
    if "over" not in idx or "under" not in idx:
        return None

    oi, ui = idx["over"], idx["under"]
    over_price = _price_at(prices, oi)
    under_price = _price_at(prices, ui)
    book_sum = (over_price or 0.0) + (under_price or 0.0)
    price_sane = 0.90 <= book_sum <= 1.10 if (over_price and under_price) else False

    return {
        "over_token": str(token_ids[oi]),
        "under_token": str(token_ids[ui]),
        "over_price": over_price,
        "under_price": under_price,
        "book_sum": round(book_sum, 4),
        "price_sane": price_sane,
    }


def _price_at(prices, i):
    try:
        return float(prices[i])
    except (IndexError, TypeError, ValueError):
        return None
