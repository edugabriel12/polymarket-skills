#!/usr/bin/env python3
"""Repair resolutions corrupted by the v9.9 settlement-outcome bug.

Post-mortem (2026-07-05): run_resolution_sweep decided the outcome with
    if gamma_closed or prices[0] >= 0.95: final = "YES"
    elif gamma_closed or prices[1] >= 0.95: final = "NO"
— once Polymarket flips `closed=True`, the first branch is always true, so
EVERY officially-closed market resolved as "YES". All 17 positions settled by
the 2026-07-05 on-demand sweep were recorded as losses, including NO bets that
verifiably WON on Polymarket (e.g. lowest-temperature-in-paris-on-july-3-2026-14c
resolved "No" → the bot's NO bet won, but was recorded outcome=YES, payout 0).

This script re-audits EVERY row in `resolutions` against Gamma using the fixed
decision (weather_edge_bot._decide_final_outcome) and, with --apply:

  1. Updates resolutions.final_outcome / payout_per_share in weather_edge.db.
  2. Compensates the paper portfolio: finds the SELL trade the resolution
     close wrote (reasoning 'resolution:%', matching token), re-prices it at
     the correct payout, and credits/debits cash_balance by the delta.
     If no such trade exists (position was cashed out before resolution, so
     the close was skipped), only the resolutions row is fixed.

Default is DRY-RUN (report only). Run on the machine that hosts
~/.polymarket-paper/ (the operator's), with the venv active:

    python repair_resolutions.py            # audit, no writes
    python repair_resolutions.py --apply    # fix DB + refund portfolio
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                       / "polymarket-paper-trader" / "scripts"))

import weather_edge_db as db  # noqa: E402
from weather_edge_bot import (_decide_final_outcome,  # noqa: E402
                              _fetch_resolved_market)

PORTFOLIO_DB = Path.home() / ".polymarket-paper" / "portfolio.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def audit_row(row) -> dict:
    """Re-fetch Gamma for one resolved entry and recompute the outcome.
    Returns a report dict with status:
      'ok'          — stored outcome matches the recomputed one
      'wrong'       — mismatch; carries correct_outcome/correct_payout
      'unfetchable' — Gamma has no data for the slug (leave as-is)
      'inconclusive'— prices don't determine a winner yet (leave as-is)
    """
    base = {"entry_id": row["entry_id"], "slug": row["market_slug"],
            "side": row["side"], "stored_outcome": row["final_outcome"],
            "stored_payout": row["payout_per_share"]}
    m = _fetch_resolved_market(row["market_slug"])
    if m is None:
        return {**base, "status": "unfetchable"}
    try:
        prices = [float(p) for p in json.loads(m.get("outcomePrices", "[]"))]
    except (ValueError, TypeError, json.JSONDecodeError):
        return {**base, "status": "unfetchable"}
    if len(prices) < 2:
        return {**base, "status": "unfetchable"}
    correct = _decide_final_outcome(prices, bool(m.get("closed")))
    if correct is None:
        return {**base, "status": "inconclusive", "prices": prices}
    correct_payout = (1.0 if correct == row["side"] else 0.0)
    if correct == "VOID":
        correct_payout = float(row["entry_price"] or 0)
    if (correct == row["final_outcome"]
            and abs(correct_payout - float(row["payout_per_share"] or 0)) < 1e-9):
        return {**base, "status": "ok"}
    return {**base, "status": "wrong", "prices": prices,
            "correct_outcome": correct, "correct_payout": correct_payout}


def repair_portfolio(rep: dict, row, dry_run: bool) -> dict:
    """Re-price the resolution SELL trade at the correct payout and adjust
    cash. Returns {"portfolio": <action>, "cash_delta": float}."""
    token_id = (row["token_id_yes"] if row["side"] == "YES"
                else row["token_id_no"])
    shares = float(row["size_shares"] or 0)
    if not token_id or shares <= 0:
        return {"portfolio": "no_position_data", "cash_delta": 0.0}
    if not PORTFOLIO_DB.exists():
        return {"portfolio": "portfolio_db_missing", "cash_delta": 0.0}

    conn = sqlite3.connect(PORTFOLIO_DB)
    conn.row_factory = sqlite3.Row
    try:
        trade = conn.execute(
            "SELECT * FROM trades WHERE token_id = ? AND action = 'SELL' "
            "AND reasoning LIKE 'resolution:%' ORDER BY id DESC LIMIT 1",
            (str(token_id),)).fetchone()
        if trade is None:
            # Position was cashed out before resolution (close was skipped) —
            # nothing to refund; the cashout P&L already stands.
            return {"portfolio": "no_resolution_trade", "cash_delta": 0.0}

        old_proceeds = float(trade["total_cost"] or 0)
        traded_shares = float(trade["shares"] or shares)
        new_price = rep["correct_payout"]
        # DEFAULT_FEE_RATE is 0.0; mirror the original close's fee treatment
        # by scaling: original fee was proceeds*rate → recompute proportionally.
        old_price = float(trade["price"] or 0)
        old_fee = float(trade["fee"] or 0)
        fee_rate = (old_fee / (old_price * traded_shares)
                    if old_price and traded_shares else 0.0)
        new_gross = traded_shares * new_price
        new_fee = new_gross * fee_rate
        new_net = new_gross - new_fee
        delta = new_net - old_proceeds
        if dry_run:
            return {"portfolio": "would_adjust", "cash_delta": round(delta, 4)}

        now = _now_iso()
        conn.execute(
            "UPDATE trades SET price = ?, fee = ?, total_cost = ?, "
            "reasoning = reasoning || ' |repaired v13.3 (was price=' || ? || ')' "
            "WHERE id = ?",
            (round(new_price, 6), round(new_fee, 4), round(new_net, 4),
             round(old_price, 6), trade["id"]))
        conn.execute(
            "UPDATE portfolios SET cash_balance = cash_balance + ?, "
            "updated_at = ? WHERE id = ?",
            (round(delta, 4), now, trade["portfolio_id"]))
        conn.commit()
        return {"portfolio": "adjusted", "cash_delta": round(delta, 4)}
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Re-audit resolutions against Gamma with the fixed "
                    "outcome logic; --apply fixes weather_edge.db and "
                    "refunds the paper portfolio.")
    ap.add_argument("--apply", action="store_true",
                    help="write fixes (default: dry-run report only)")
    args = ap.parse_args()
    dry = not args.apply

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT e.*, r.final_outcome, r.payout_per_share, r.resolution_id "
            "FROM resolutions r JOIN entries e ON e.entry_id = r.entry_id "
            "ORDER BY r.resolution_id").fetchall()

    print(f"{'DRY-RUN' if dry else 'APPLY'}: auditando {len(rows)} resoluções…\n")
    n_wrong = n_ok = n_skip = 0
    total_delta = 0.0
    for row in rows:
        rep = audit_row(row)
        if rep["status"] == "ok":
            n_ok += 1
            continue
        if rep["status"] in ("unfetchable", "inconclusive"):
            n_skip += 1
            print(f"  SKIP #{rep['entry_id']:3d} {rep['slug'][:52]:52s} "
                  f"{rep['status']}")
            continue
        n_wrong += 1
        port = repair_portfolio(rep, row, dry)
        total_delta += port["cash_delta"]
        print(f"  {'WRONG' if dry else 'FIXED'} #{rep['entry_id']:3d} "
              f"{rep['slug'][:52]:52s} "
              f"{rep['stored_outcome']}→{rep['correct_outcome']} "
              f"payout {rep['stored_payout']}→{rep['correct_payout']} "
              f"| portfolio: {port['portfolio']} "
              f"cash{'+' if port['cash_delta'] >= 0 else ''}{port['cash_delta']}")
        if not dry:
            with db.connect() as conn:
                conn.execute(
                    "UPDATE resolutions SET final_outcome = ?, "
                    "payout_per_share = ? WHERE resolution_id = ?",
                    (rep["correct_outcome"], rep["correct_payout"],
                     row["resolution_id"]))

    print(f"\nresumo: {n_ok} corretas, {n_wrong} "
          f"{'erradas (use --apply para corrigir)' if dry else 'corrigidas'}, "
          f"{n_skip} puladas | ajuste de caixa total: "
          f"${total_delta:+.2f}{' (simulado)' if dry else ''}")


if __name__ == "__main__":
    main()
