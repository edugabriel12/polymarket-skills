#!/usr/bin/env python3
"""Phase 2: a wallet's separated Resultados = the CSV snapshot MERGED with its live
watched bets as they settle.

Both are reduced to the same per-bet record shape and re-aggregated with the existing
`rollup_csv`, so the merged report has identical by_category / by_confidence structure.
Only SETTLED live bets (WON/LOST/VOID) join the merge — open ones are cards on the
Sports side, not results here. (No dedup: the CSV is the historical upload and live
bets are new markets detected after adding the wallet.)
"""

from __future__ import annotations

import wallet_report as wr


def bet_to_record(bet: dict) -> dict:
    """A live wallet_bets row → the CSV-record shape rollup_csv consumes."""
    status = bet.get("status")
    won = True if status == "WON" else (False if status == "LOST" else None)
    pnl = float(bet.get("pnl") or 0.0)
    return {
        "category": bet.get("category") or "Other",
        "subcategory": bet.get("subcategory") or "Outro",
        "confidence": bet.get("confidence") or "—",
        "side": bet.get("side") or "",
        "total_pnl": pnl, "realized_pnl": pnl, "unrealized_pnl": 0.0,
        "invested": float(bet.get("total_position") or 0.0), "current_value": 0.0,
        "won": won, "n_trades": 1,
    }


def merged_analysis(csv_records: list[dict], live_bets: list[dict]) -> dict:
    """rollup_csv(csv_records + settled live records). Same shape as the CSV analysis."""
    settled = [b for b in (live_bets or []) if b.get("status") in ("WON", "LOST", "VOID")]
    records = list(csv_records or []) + [bet_to_record(b) for b in settled]
    rep = wr.rollup_csv(records)
    rep["live_settled"] = len(settled)        # how many live bets contributed (for the UI)
    return rep
