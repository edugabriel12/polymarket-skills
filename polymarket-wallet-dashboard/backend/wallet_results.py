#!/usr/bin/env python3
"""A wallet's separated Resultados = ONLY its live watched bets after it was added
(the CSV is used solely to derive the confidence→unit bands, never shown as results).

Live bets are reduced to the same per-bet record shape and aggregated with the
existing `rollup_csv`, so the report keeps the by_category / by_confidence structure.
Only SETTLED bets (WON/LOST/VOID) count toward the figures — open ones are cards on
the Sports side.
"""

from __future__ import annotations

import wallet_report as wr
from wallet_filters import filter_bets


def bet_to_record(bet: dict) -> dict:
    """A live wallet_bets row → the record shape rollup_csv consumes."""
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


def live_results(live_bets: list[dict], filters: dict | None = None) -> dict:
    """rollup_csv over the wallet's SETTLED live bets only (no CSV). Same analysis shape.

    `filters` is the wallet's forwarding filter ({category:{subcategory:[confidences]}}). When
    set, only bets whose (category, subcategory, confidence) pass it count — so Resultados show
    exactly what the wallet forwards. `None` (no filter) keeps every bet (unchanged behavior).
    """
    kept = filter_bets(filters, live_bets or [])
    settled = [b for b in kept if b.get("status") in ("WON", "LOST", "VOID")]
    rep = wr.rollup_csv([bet_to_record(b) for b in settled])
    rep["live_settled"] = len(settled)
    rep["live_open"] = sum(1 for b in kept if b.get("status") == "OPEN")
    return rep
