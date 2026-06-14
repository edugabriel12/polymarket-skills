#!/usr/bin/env python3
"""Identify and parse soccer TOTAL-GOALS (Over/Under) and BTTS markets.

Works on the per-game `markets[]` shape from the category scanner / category_common
(each market has `question`, `outcomes`, `outcome_prices`, `token_ids`, `slug`).
Market text is untrusted (CLAUDE.md rule #5) and only used for classification.
"""

from __future__ import annotations

import re

# Full-game total-goals event slug: "...-YYYY-MM-DD-total-<line>[pt5]".
GAME_TOTAL_RE = re.compile(r"-\d{4}-\d{2}-\d{2}-total-\d{1,2}(?:pt5)?$")
# BTTS event slug: "...-YYYY-MM-DD-btts" (or both-teams-to-score variants).
GAME_BTTS_RE = re.compile(r"-\d{4}-\d{2}-\d{2}-(?:btts|both-teams-to-score|gg)$")

_LINE_RE = re.compile(r"(?<!\d)(\d{1,2}(?:\.5)?)(?!\d)")
_MIN_LINE, _MAX_LINE = 0.5, 8.5


def _kind(text: str) -> str | None:
    t = (text or "").strip().lower()
    if t.startswith("over") or t == "o":
        return "over"
    if t.startswith("under") or t == "u":
        return "under"
    if t in ("yes", "y") or t.startswith("yes"):
        return "yes"
    if t in ("no", "n") or t.startswith("no"):
        return "no"
    return None


def is_total_market(market: dict) -> bool:
    outs = market.get("outcomes") or []
    return len(outs) == 2 and {_kind(o) for o in outs} == {"over", "under"}


def is_btts_market(market: dict) -> bool:
    outs = market.get("outcomes") or []
    if len(outs) != 2 or {_kind(o) for o in outs} != {"yes", "no"}:
        return False
    blob = (market.get("question", "") + " " + market.get("slug", "")).lower()
    return ("both" in blob and "score" in blob) or "btts" in blob


def parse_total_line(market: dict) -> float | None:
    for outcome in market.get("outcomes") or []:
        v = _first_plausible(outcome)
        if v is not None:
            return v
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


def _two_sided(market: dict, a: str, b: str) -> dict | None:
    """Map a 2-outcome market to {a_token, b_token, a_price, b_price, book_sum, price_sane}."""
    outcomes = market.get("outcomes") or []
    tokens = market.get("token_ids") or []
    prices = market.get("outcome_prices") or []
    if len(outcomes) != 2 or len(tokens) != 2:
        return None
    idx = {}
    for i, o in enumerate(outcomes):
        k = _kind(o)
        if k:
            idx[k] = i
    if a not in idx or b not in idx:
        return None
    ai, bi = idx[a], idx[b]
    ap, bp = _price(prices, ai), _price(prices, bi)
    book = (ap or 0) + (bp or 0)
    return {
        f"{a}_token": str(tokens[ai]), f"{b}_token": str(tokens[bi]),
        f"{a}_price": ap, f"{b}_price": bp,
        "book_sum": round(book, 4),
        "price_sane": (0.90 <= book <= 1.10) if (ap and bp) else False,
    }


def over_under_tokens(market: dict) -> dict | None:
    return _two_sided(market, "over", "under")


def btts_tokens(market: dict) -> dict | None:
    return _two_sided(market, "yes", "no")


def _price(prices, i):
    try:
        return float(prices[i])
    except (IndexError, TypeError, ValueError):
        return None
