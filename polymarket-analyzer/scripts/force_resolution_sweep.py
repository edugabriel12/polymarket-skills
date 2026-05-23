"""One-shot manual resolution sweep — use to clean up positions that
the daemon's sweep missed (e.g. Busan/Beijing held open after end_date
because the legacy 0.99 price threshold was too strict).

Behaviour:
  1. Loads weather_edge.db
  2. Finds EXECUTED entries past end_date with no cashout/resolution
  3. For each, fetches Gamma /markets?slug=... and inspects:
       - m["closed"] boolean (authoritative)
       - m["outcomePrices"] (fallback if closed but no 0.99 marker)
  4. Inserts resolutions row + closes paper_engine position
  5. Prints a summary

Safe to re-run: each successful resolution writes a resolutions row
that excludes the entry from future passes.

Usage:
  python force_resolution_sweep.py            # process all eligible
  python force_resolution_sweep.py --dry-run  # report only, no writes
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO.parent.parent / "polymarket-paper-trader" / "scripts"))

import weather_edge_db as db  # noqa: E402

GAMMA = "https://gamma-api.polymarket.com"
PRICE_THRESHOLD = 0.95


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_one(row: dict, dry_run: bool) -> dict:
    slug = row["market_slug"]
    try:
        r = requests.get(f"{GAMMA}/markets", params={"slug": slug}, timeout=15)
        r.raise_for_status()
        results = r.json()
    except Exception as e:
        return {"entry_id": row["entry_id"], "status": "fetch_failed",
                "err": str(e)}
    if not isinstance(results, list) or not results:
        return {"entry_id": row["entry_id"], "status": "not_found"}
    m = results[0]
    try:
        outcomes = json.loads(m.get("outcomes", "[]"))
        prices = [float(p) for p in json.loads(m.get("outcomePrices", "[]"))]
    except Exception:
        return {"entry_id": row["entry_id"], "status": "bad_response"}
    if not outcomes or not prices or len(outcomes) != len(prices):
        return {"entry_id": row["entry_id"], "status": "no_prices"}

    gamma_closed = bool(m.get("closed"))
    if prices[0] >= PRICE_THRESHOLD:
        final = "YES"
    elif prices[1] >= PRICE_THRESHOLD:
        final = "NO"
    elif gamma_closed:
        final = "VOID"
    else:
        return {"entry_id": row["entry_id"], "status": "not_settled",
                "prices": prices, "closed": gamma_closed}

    payout = 1.0 if final == row["side"] else 0.0
    if final == "VOID":
        payout = float(row["entry_price"] or 0)

    pnl_est = (payout - float(row["entry_price"] or 0)) * float(row["size_shares"] or 0)
    out = {
        "entry_id": row["entry_id"], "city": row["city_resolved"],
        "side": row["side"], "entry_price": row["entry_price"],
        "final_outcome": final, "payout": payout,
        "pnl_estimate_usd": round(pnl_est, 2),
        "closed_flag": gamma_closed, "prices": prices,
        "status": "would_resolve" if dry_run else "resolved",
    }
    if dry_run:
        return out

    # Write resolution + close paper position
    with db.connect() as conn:
        db.insert_resolution(
            conn, entry_id=row["entry_id"],
            ts_resolved=_now_iso(),
            final_outcome=final,
            payout_per_share=payout,
        )
        conn.commit()

    try:
        import paper_engine
        token_id = (row["token_id_yes"] if row["side"] == "YES"
                     else row["token_id_no"])
        if token_id:
            try:
                paper_engine.close_position(
                    token_id=token_id, side=row["side"],
                    reasoning=f"force_resolution:{final}",
                    force_exit_price=payout,
                )
                out["paper_closed"] = True
            except RuntimeError as ce:
                out["paper_closed"] = False
                out["paper_err"] = str(ce)
    except ImportError as e:
        out["paper_err"] = str(e)
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true",
                   help="Report what would happen, don't write")
    args = p.parse_args()

    with db.connect() as conn:
        rows = [dict(r) for r in db.query_unresolved_past_end(conn, _now_iso())]

    print(f"Found {len(rows)} EXECUTED entries past end_date.")
    if not rows:
        return 0

    results = []
    for row in rows:
        r = resolve_one(row, dry_run=args.dry_run)
        results.append(r)
        marker = "DRY" if args.dry_run else "OK"
        print(f"  #{r['entry_id']:>3} {r.get('city','?'):<14} "
              f"{r.get('side','?'):<3} → {r.get('status'):<20} "
              f"{r.get('final_outcome','-'):<5} "
              f"payout=${r.get('payout',0):.2f} "
              f"pnl≈${r.get('pnl_estimate_usd',0):+.2f}")

    total_pnl = sum(r.get("pnl_estimate_usd", 0)
                     for r in results
                     if r.get("status") in ("resolved", "would_resolve"))
    n_resolved = sum(1 for r in results
                      if r.get("status") in ("resolved", "would_resolve"))
    n_pending = sum(1 for r in results if r.get("status") == "not_settled")
    n_failed = len(results) - n_resolved - n_pending
    print()
    print(f"Summary: {n_resolved} resolved, {n_pending} still not settled, "
          f"{n_failed} failed")
    print(f"Estimated realized P&L: ${total_pnl:+.2f}")
    if args.dry_run:
        print("(DRY RUN — no changes written. Re-run without --dry-run to apply.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
