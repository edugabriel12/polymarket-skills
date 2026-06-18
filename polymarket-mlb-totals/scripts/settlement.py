#!/usr/bin/env python3
"""Cross-source settlement of PENDENTE predictions.

The authoritative Over/Under resolution is the MLB Stats API final total: it only
reports a total once a game reaches the **Final** state (postponed/suspended games
never do), so a final total already means the game resolved. Settlement keys off
that.

The Polymarket market's closed/resolved status (Gamma API) is fetched too — used to
backfill the stored market_url and available as an *optional* extra guard via
`require_closed=True`. It is **not** required by default, because Polymarket often
closes a market well after the game ends (and the lookup itself is flaky), which
would otherwise leave clearly-finished games stuck on PENDENTE.

Best-effort and network-isolated: with egress blocked, sources return empty and
nothing settles (rows stay PENDENTE).
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
                       require_closed: bool = False) -> list[tuple[int, float]]:
    """Pure: choose (prediction_id, actual_total) pairs ready to settle.

    Requires an MLB final total for the matchup (authoritative). When
    require_closed is set, also requires the Polymarket market to be closed.
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
                   require_closed: bool = False) -> dict:
    """Settle eligible PENDENTE predictions from the authoritative MLB final total.

    Returns {checked, finals_found, markets_closed, settled:[...], backfilled_urls}.
    """
    from track_predictions import fetch_final_totals  # lazy (pulls requests)

    pending = pdb.get_predictions(db_path, status="PENDENTE")
    if not pending:
        return {"checked": 0, "finals_found": 0, "markets_closed": 0,
                "settled": [], "backfilled_urls": 0}

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
    return {"checked": len(pending), "finals_found": len(finals),
            "markets_closed": sum(1 for v in closed_map.values() if v),
            "settled": settled, "backfilled_urls": backfilled}


def settle_model_log_from_feed(api, db_path: str = pdb.DEFAULT_DB) -> int:
    """Settle ALL shadow rows (bet or not) from MLB finals, for unbiased calibration.

    Unlike settle_pending, this covers games the model did NOT bet — needed so the
    calibration report isn't biased toward games where edge was found. Best-effort.
    """
    rows = pdb.get_model_log(db_path)
    dates = sorted({r["game_date"] for r in rows
                    if r.get("game_date") and r.get("ref_outcome") is None})
    if not dates:
        return 0
    from track_predictions import fetch_final_totals  # lazy
    finals_pair: dict = {}
    for d in dates:
        finals_pair.update(fetch_final_totals(api, d))
    # Map base game slug -> total via its (away, home) teams.
    finals_total: dict = {}
    for r in rows:
        base = pdb.model_log_base(r.get("game_slug", ""))
        if base in finals_total:
            continue
        away, home = pf.parse_slug_teams(r.get("game_slug", ""))
        t = finals_pair.get((away, home))
        if t is not None:
            finals_total[base] = t
    return pdb.settle_model_log(db_path, finals_total)


def capture_close_prices(api, db_path: str = pdb.DEFAULT_DB) -> int:
    """Snapshot the reference-side closing price for shadow rows missing it (CLV).

    Run near game time. Fetches the current CLOB midpoint for each row's ref_token.
    Best-effort: rows without a token or price are left for a later run.
    """
    from category_common import fetch_midpoint  # lazy
    con = pdb.connect(db_path)
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT id, ref_token FROM model_log WHERE close_price IS NULL "
            "AND ref_token IS NOT NULL")]
    finally:
        con.close()
    n = 0
    for r in rows:
        mid = fetch_midpoint(api, r["ref_token"])
        if mid is not None:
            pdb.set_close_price(db_path, r["id"], mid)
            n += 1
    return n


def _backfill_market_urls(db_path, pending, slug_map) -> int:
    """Fill market_url for rows missing it, using a Gamma slug when available."""
    con = pdb.connect(db_path)
    n = 0
    try:
        with con:
            for r in pending:
                if r.get("market_url"):
                    continue
                # Link to the game event, not the specific total line (strip "-total-9pt5").
                event = pdb.model_log_base(slug_map.get(r.get("condition_id")) or "")
                url = (f"https://polymarket.com/event/{event}" if event
                       else f"https://polymarket.com/sports/mlb/{r.get('game_slug','')}")
                con.execute("UPDATE predictions SET market_url=? WHERE id=?", (url, r["id"]))
                n += 1
    finally:
        con.close()
    return n
