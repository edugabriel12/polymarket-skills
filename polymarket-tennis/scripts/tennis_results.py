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


def build_winner_lookup(matches: list[dict]) -> dict:
    """{frozenset(player_a, player_b): winner_name} from Sackmann match dicts."""
    out: dict[frozenset, str] = {}
    for m in matches:
        out[_pair(m["winner"], m["loser"])] = m["winner"]
    return out


def settle_pending(db_path: str = tdb.DEFAULT_DB, tour: str = "atp",
                   years: list[int] | None = None) -> dict:
    """Settle eligible PENDENTE predictions from Sackmann season results. Best-effort."""
    pending = tdb.get_predictions(db_path, status="PENDENTE")
    if not pending:
        return {"checked": 0, "settled": []}
    if years is None:
        years = [datetime.now(timezone.utc).year]
    matches = ratings_source.fetch_sackmann_matches(tour, years)
    if not matches:
        return {"checked": len(pending), "settled": [],
                "note": "no results feed (offline or Sackmann not yet updated)"}
    lookup = build_winner_lookup(matches)

    settled, seen = [], set()
    for r in pending:
        slug = r["match_slug"]
        if slug in seen:
            continue
        winner = lookup.get(_pair(r.get("side", ""), r.get("opponent", "")))
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
    matches = ratings_source.fetch_sackmann_matches(tour, years)
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
        winner = lookup.get(_pair(p.get("side", ""), p.get("opponent", "")))
        if winner:
            winners[tdb.model_log_base(slug)] = winner
    return tdb.settle_model_log(db_path, winners)
