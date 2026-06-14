#!/usr/bin/env python3
"""Cross-source settlement of PENDENTE predictions.

A prediction is settled to ACERTO/ERRO only when BOTH sources confirm:
  1. MLB Stats API reports the game Final with a total runs count (the truth for
     Over/Under), reused from track_predictions.fetch_final_totals.
  2. The Polymarket market is closed/resolved (Gamma API), so the bet actually
     resolved (guards against postponed/suspended games).

Best-effort and network-isolated: with egress blocked, sources return empty and
nothing settles (rows stay PENDENTE). Also backfills the stored market_url.
"""

from __future__ import annotations

import predictions_db as pdb
import park_factors as pf

GAMMA_API = "https://gamma-api.polymarket.com"


def fetch_market_status(api, condition_ids: list[str]) -> tuple[dict, dict]:
    """Return ({condition_id: closed_bool}, {condition_id: slug}) from Gamma.

    Best-effort; {} on failure. Tries a batched query, falls back to per-id.
    """
    closed: dict[str, bool] = {}
    slugs: dict[str, str] = {}
    ids = [c for c in dict.fromkeys(condition_ids) if c]
    if not ids:
        return closed, slugs

    def absorb(markets):
        for m in markets if isinstance(markets, list) else []:
            cid = m.get("conditionId") or m.get("condition_id")
            if not cid:
                continue
            is_closed = bool(m.get("closed")) or (
                str(m.get("umaResolutionStatus", "")).lower() == "resolved")
            closed[cid] = is_closed
            if m.get("slug"):
                slugs[cid] = m["slug"]

    try:
        absorb(api.get(f"{GAMMA_API}/markets", params={"condition_ids": ids}))
    except Exception:  # noqa: BLE001 - batch failed hard (offline/forbidden): give up
        return closed, slugs
    # Fall back to per-id only for ids the (successful) batch call didn't return.
    for cid in ids:
        if cid in closed:
            continue
        try:
            absorb(api.get(f"{GAMMA_API}/markets", params={"condition_ids": cid}))
        except Exception:  # noqa: BLE001
            continue
    return closed, slugs


def decide_settlements(pending: list[dict], finals: dict, closed_map: dict,
                       require_closed: bool = True) -> list[tuple[int, float]]:
    """Pure: choose (prediction_id, actual_total) pairs ready to settle.

    Requires an MLB final total for the matchup and (when require_closed) the
    Polymarket market to be closed/resolved.
    """
    out = []
    for row in pending:
        away, home = pf.parse_slug_teams(row.get("game_slug", ""))
        total = finals.get((away, home))
        if total is None:
            continue
        if require_closed and not closed_map.get(row.get("condition_id")):
            continue
        out.append((row["id"], total))
    return out


def settle_pending(api, db_path: str = pdb.DEFAULT_DB,
                   require_closed: bool = True) -> dict:
    """Settle eligible PENDENTE predictions across MLB + Polymarket sources.

    Returns {checked, settled:[{id,status,actual_total}], backfilled_urls}.
    """
    from track_predictions import fetch_final_totals  # lazy (pulls requests)

    pending = pdb.get_predictions(db_path, status="PENDENTE")
    if not pending:
        return {"checked": 0, "settled": [], "backfilled_urls": 0}

    dates = sorted({r["game_date"] for r in pending if r.get("game_date")})
    finals: dict = {}
    for d in dates:
        finals.update(fetch_final_totals(api, d))

    cond_ids = [r.get("condition_id") for r in pending]
    closed_map, slug_map = fetch_market_status(api, cond_ids)

    backfilled = _backfill_market_urls(db_path, pending, slug_map)

    settled = []
    for pid, total in decide_settlements(pending, finals, closed_map, require_closed):
        status = pdb.settle_prediction(pid, total, db_path)
        settled.append({"id": pid, "status": status, "actual_total": total})
    return {"checked": len(pending), "settled": settled, "backfilled_urls": backfilled}


def _backfill_market_urls(db_path, pending, slug_map) -> int:
    """Fill market_url for rows missing it, using a Gamma slug when available."""
    con = pdb.connect(db_path)
    n = 0
    try:
        with con:
            for r in pending:
                if r.get("market_url"):
                    continue
                slug = slug_map.get(r.get("condition_id"))
                url = (f"https://polymarket.com/event/{slug}" if slug
                       else f"https://polymarket.com/sports/mlb/{r.get('game_slug','')}")
                con.execute("UPDATE predictions SET market_url=? WHERE id=?", (url, r["id"]))
                n += 1
    finally:
        con.close()
    return n
