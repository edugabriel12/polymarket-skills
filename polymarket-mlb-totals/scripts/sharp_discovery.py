#!/usr/bin/env python3
"""Sharp-source-driven discovery of the day's MLB game-total markets.

Why this exists — two confirmed bugs in tag-based discovery:

  1. COVERAGE: the `mlb` Gamma tag is NOT honored. Gamma returns the global
     volume-ranked mix (crypto/politics/other sports), and pagination is capped
     (HTTP 422 past offset ~2100). Low-volume MLB games fall past the cut, so only
     a couple of the day's ~11 games ever surface.
  2. MATCHING: even when a game IS found, the sharp reference frequently fails to
     match it (team-abbrev / line drift), so the divergence detector silently
     reverts to factor-noise betting (which the 10-season backtest proved is -EV).

This module fixes BOTH at once by inverting the flow: the SHARP slate
(Pinnacle/consensus) carries the FULL daily card, so it becomes the AUTHORITATIVE
game list. For each sharp game we fetch its Polymarket markets directly by event
slug. Every returned game therefore (a) exists regardless of volume rank, and
(b) already carries its sharp reference (it came FROM the slate).

Pure slug construction is isolated for offline tests; the Gamma fetch is
best-effort and returns [] offline.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  (wires sys.path for the reused category-watcher module)

from category_common import GAMMA_API, parse_market


def games_from_lookup(sharp_lookup: dict, target: str) -> list[tuple[str, str]]:
    """Team-abbrev pairs for each sharp game dated `target`.

    The sharp lookup key is `(date, frozenset({abbr, abbr}))` (already normalized to
    Polymarket abbreviations by sharp_odds._key) — team ORDER is lost, so the two
    abbrevs are returned sorted and discovery tries both slug orderings.
    """
    out: list[tuple[str, str]] = []
    for key in sharp_lookup:
        try:
            date, teams = key
        except (TypeError, ValueError):
            continue
        if date != target:
            continue
        ts = sorted(t for t in teams if t)
        if len(ts) == 2:
            out.append((ts[0], ts[1]))
    return out


def candidate_event_slugs(a: str, b: str, date: str) -> list[str]:
    """Both `mlb-<away>-<home>-<date>` orderings for a pair (home/away is unknown)."""
    slugs = [f"mlb-{a}-{b}-{date}", f"mlb-{b}-{a}-{date}"]
    # Drop the duplicate if a == b (shouldn't happen) and preserve order.
    return list(dict.fromkeys(slugs))


def fetch_event_markets(api, event_slug: str, category_key: str = "baseball") -> list[dict]:
    """Parsed markets nested under a Polymarket event slug via Gamma /events.

    Returns [] on any miss/failure (offline-safe). Each market is normalized with
    category_common.parse_market — identical shape to discover_markets' output.
    """
    try:
        events = api.get(f"{GAMMA_API}/events", params={"slug": event_slug})
    except Exception:  # noqa: BLE001 - discovery is best-effort
        return []
    if not isinstance(events, list) or not events:
        return []
    out: list[dict] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        for m in (ev.get("markets") or []):
            if not isinstance(m, dict):
                continue
            # Group each market by its OWN slug — a totals market slug encodes the line
            # (e.g. mlb-chc-nym-2026-06-23-total-8pt5), and the downstream run-total
            # filter keys off that `-total-<line>` suffix. Forcing the base event slug
            # here would strip the suffix and drop every total market. Moneyline/spread
            # markets keep their own (non-total) slugs and are dropped, as intended.
            m["eventSlug"] = m.get("slug") or ev.get("slug") or event_slug
            out.append(parse_market(m, category_key))
    return out


def discover_from_sharp(api, sharp_lookup: dict, target: str, *, vlog=None) -> list[dict]:
    """Use the sharp slate as the authoritative MLB game list; fetch each game's
    Polymarket markets by event slug.

    Returns parsed markets (same shape as discover_markets) — the full slate, each
    game guaranteed to carry a sharp reference. Best-effort: a game whose event can't
    be fetched (offline, or a slug we couldn't construct) is logged and skipped.
    """
    vlog = vlog or (lambda *a, **k: None)
    games = games_from_lookup(sharp_lookup, target)
    if not games:
        return []
    markets: list[dict] = []
    seen_slugs: set[str] = set()
    found = 0
    for a, b in games:
        ev_markets: list[dict] = []
        for slug in candidate_event_slugs(a, b, target):
            ev_markets = fetch_event_markets(api, slug)
            if ev_markets:
                break
        if not ev_markets:
            vlog(f"  [sharp-discovery] {a} vs {b} {target}: no Polymarket event found")
            continue
        found += 1
        for m in ev_markets:
            slug = m.get("slug") or ""
            if slug and slug in seen_slugs:
                continue
            if slug:
                seen_slugs.add(slug)
            markets.append(m)
    vlog(f"  [sharp-discovery] {found}/{len(games)} sharp game(s) matched to a Polymarket "
         f"event ({len(markets)} market(s) fetched)")
    return markets
