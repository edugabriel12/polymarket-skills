#!/usr/bin/env python3
"""Auto-settlement of tennis predictions + closing-price capture (best-effort).

Settlement matches each PENDENTE prediction's player pair to a finished match in Jeff
Sackmann's season data and settles to the actual winner. Sackmann's `tourney_date` is
the tournament START date (not the match date), so matching is by the unordered player
pair within the season — best-effort, and lagged by however often Sackmann updates.
Offline/blocked it settles nothing (rows stay PENDENTE).

CLV: `capture_close_prices` snapshots each shadow row's reference-side CLOB midpoint.
"""

from __future__ import annotations

from datetime import datetime, timezone

import ratings_source
import tennis_predictions as tdb
from ratings import normalize


def _pair(a: str, b: str) -> frozenset:
    return frozenset({normalize(a), normalize(b)})


def _surname(name: str) -> str:
    """Last surname token, dropping a trailing initial — converges name formats across feeds.

    'Carlos Alcaraz' (Sackmann) -> 'alcaraz'; 'Alcaraz C.' (tennis-data) -> 'alcaraz';
    'Roberto Bautista Agut' -> 'agut'. So the same match settles regardless of which feed
    supplied it and how the prediction stored the player name.
    """
    return ratings_source._surname_key(normalize(name))


def _surname_pair(a: str, b: str) -> frozenset:
    return frozenset({_surname(a), _surname(b)})


def build_winner_lookup(matches: list[dict]) -> dict:
    """{frozenset(players): winner} keyed by BOTH the full-name pair and (when unambiguous) the
    surname pair — so predictions match whether the feed used full names or 'Surname I.'."""
    out: dict[frozenset, str] = {}
    sur_count: dict[frozenset, int] = {}
    sur_win: dict[frozenset, str] = {}
    for m in matches:
        w, l = m["winner"], m["loser"]
        out[_pair(w, l)] = w
        sk = _surname_pair(w, l)
        if len(sk) == 2:                       # two distinct surnames
            sur_count[sk] = sur_count.get(sk, 0) + 1
            sur_win[sk] = w
    for sk, n in sur_count.items():
        if n == 1 and sk not in out:           # add surname alias only when unambiguous
            out[sk] = sur_win[sk]
    return out


def lookup_winner(lookup: dict, side: str, opponent: str) -> str | None:
    """Winner for a prediction's (side, opponent), trying the full-name pair then the surname pair."""
    return lookup.get(_pair(side, opponent)) or lookup.get(_surname_pair(side, opponent))


def settle_pending(db_path: str = tdb.DEFAULT_DB, tour: str = "atp",
                   years: list[int] | None = None) -> dict:
    """Settle eligible PENDENTE predictions from Sackmann season results. Best-effort."""
    pending = tdb.get_predictions(db_path, status="PENDENTE")
    if not pending:
        return {"checked": 0, "settled": []}
    if years is None:
        years = [datetime.now(timezone.utc).year]
    matches = ratings_source.fetch_matches(tour, years)
    if not matches:
        return {"checked": len(pending), "settled": [],
                "note": "no results feed (offline, or no source has the matches yet)"}
    lookup = build_winner_lookup(matches)

    settled, seen = [], set()
    for r in pending:
        slug = r["match_slug"]
        if slug in seen:
            continue
        winner = lookup_winner(lookup, r.get("side", ""), r.get("opponent", ""))
        if winner:
            seen.add(slug)
            settled.extend(tdb.settle_match(slug, winner, db_path))
    return {"checked": len(pending), "settled": settled, "games_matched": len(seen)}


def capture_close_prices(db_path: str = tdb.DEFAULT_DB) -> int:
    """Snapshot the reference-side closing CLOB midpoint for shadow rows missing it (CLV)."""
    from category_common import APIClient, fetch_midpoint  # lazy
    con = tdb.connect(db_path)
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT id, ref_token FROM model_log WHERE close_price IS NULL "
            "AND ref_token IS NOT NULL")]
    finally:
        con.close()
    api = APIClient()
    n = 0
    for r in rows:
        mid = fetch_midpoint(api, r["ref_token"])
        if mid is not None:
            tdb.set_close_price(db_path, r["id"], mid)
            n += 1
    return n


def settle_model_log_from_feed(db_path: str = tdb.DEFAULT_DB, tour: str = "atp",
                               years: list[int] | None = None) -> int:
    """Settle ALL shadow rows (bet or not) from Sackmann results, for unbiased calibration."""
    rows = [r for r in tdb.get_model_log(db_path) if r.get("ref_outcome") is None]
    if not rows:
        return 0
    if years is None:
        years = [datetime.now(timezone.utc).year]
    matches = ratings_source.fetch_matches(tour, years)
    if not matches:
        return 0
    # Need each shadow row's two players; ref_side is player A, but the opponent isn't
    # stored on model_log — recover it from the recorded prediction when present.
    preds = {p["match_slug"]: p for p in tdb.get_predictions(db_path)}
    lookup = build_winner_lookup(matches)
    winners: dict[str, str] = {}
    for r in rows:
        slug = r["match_slug"]
        p = preds.get(slug)
        if not p:
            continue
        winner = lookup_winner(lookup, p.get("side", ""), p.get("opponent", ""))
        if winner:
            winners[tdb.model_log_base(slug)] = winner
    return tdb.settle_model_log(db_path, winners)
