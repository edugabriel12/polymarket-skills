#!/usr/bin/env python3
"""Per-wallet forwarding filter — the single source of truth for the
`{category: {subcategory: [confidences]}}` rule a wallet uses.

The SAME predicate gates two things, so they always agree:
  • what the watcher forwards to Sports/Telegram (`watcher.detect_entries`), and
  • what counts in the wallet's Resultados — totals, breakdown, and bet lists
    (`wallet_results.live_results`, the `/bets` and `/open-bets` routes).

Pure stdlib, no imports, so both the watcher and the report layer can depend on it
without coupling them to each other.
"""

from __future__ import annotations


def passes_filter(wallet_filters: dict | None, category: str, subcategory: str,
                  confidence: str) -> bool:
    """Whether a (category, subcategory, confidence) triple is forwarded to Sports/Telegram.

    `None` → no restriction, forward everything (legacy wallets + the user selected ALL combos,
    which the API collapses to None so live categories the CSV never had still pass). A non-null
    dict is strict: a triple passes only if its category AND subcategory are selected AND the
    confidence is listed — so an explicit empty dict `{}` forwards NOTHING.
    """
    if wallet_filters is None:
        return True
    subs = wallet_filters.get(category)
    if not subs:
        return False                                          # category not selected
    confs = subs.get(subcategory)
    if not confs:
        return False                                          # subcategory not selected
    return confidence in confs


def filter_bets(wallet_filters: dict | None, bets: list[dict] | None) -> list[dict]:
    """Keep only the wallet_bets rows whose (category, subcategory, confidence) pass the
    wallet's filter. `None` → no restriction (returns every row), so the Resultados path is
    identical to before when a wallet has no filter; `{}` → keeps nothing.
    """
    bets = bets or []
    if wallet_filters is None:
        return list(bets)
    return [b for b in bets
            if passes_filter(wallet_filters, b.get("category"), b.get("subcategory"),
                             b.get("confidence"))]
