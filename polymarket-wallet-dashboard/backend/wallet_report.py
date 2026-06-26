#!/usr/bin/env python3
"""Wallet report: reuse the polymarket-wallet-analyzer engine, add the
sub-category axis, and roll up to overall -> category -> subcategory.

The engine (analyze_wallet.py) does the hard part — fetch /positions + /trades,
average-cost realized P&L, category, resolved/won. Here we attach a sub-category
to each per-market record and aggregate three levels deep. A "bet" is one market
(conditionId), matching the engine's win-rate model.
"""

from __future__ import annotations

import os
import sys

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_BACKEND_DIR, "..", ".."))
_WALLET_ANALYZER = os.path.join(_REPO_ROOT, "polymarket-wallet-analyzer", "scripts")
if _WALLET_ANALYZER not in sys.path:
    sys.path.append(_WALLET_ANALYZER)

import analyze_wallet as wa  # noqa: E402  (the engine; reused, never modified)
import subcategory as sc     # noqa: E402


def _blank() -> dict:
    return {"markets": 0, "resolved": 0, "wins": 0, "losses": 0, "n_trades": 0,
            "total_pnl": 0.0, "realized_pnl": 0.0, "unrealized_pnl": 0.0,
            "invested": 0.0, "current_value": 0.0}


def _add(b: dict, r: dict) -> None:
    b["markets"] += 1
    b["n_trades"] += int(r.get("n_trades", 0) or 0)
    b["total_pnl"] += r.get("total_pnl", 0.0)
    b["realized_pnl"] += r.get("realized_pnl", 0.0)
    b["unrealized_pnl"] += r.get("unrealized_pnl", 0.0)
    b["invested"] += r.get("invested", 0.0)
    b["current_value"] += r.get("current_value", 0.0)
    if r.get("won") is not None:
        b["resolved"] += 1
        if r["won"]:
            b["wins"] += 1
        else:
            b["losses"] += 1


def _finalize(b: dict) -> dict:
    resolved = b["resolved"]
    b["win_rate"] = round(b["wins"] / resolved, 4) if resolved else None
    b["roi"] = round(b["total_pnl"] / b["invested"], 4) if b["invested"] > 0 else None
    for k in ("total_pnl", "realized_pnl", "unrealized_pnl", "invested", "current_value"):
        b[k] = round(b[k], 2)
    return b


def rollup(address: str, records: list[dict], n_trades_total: int) -> dict:
    """overall + by_category (each nesting its subcategories), sorted by P&L desc."""
    overall = _blank()
    cats: dict[str, dict] = {}
    for r in records:
        _add(overall, r)
        cat = r.get("category") or "Other"
        sub = r.get("subcategory") or "Outro"
        c = cats.setdefault(cat, {"bucket": _blank(), "subs": {}})
        _add(c["bucket"], r)
        _add(c["subs"].setdefault(sub, _blank()), r)

    by_category = []
    for name, c in sorted(cats.items(), key=lambda kv: kv[1]["bucket"]["total_pnl"], reverse=True):
        subs = [{"subcategory": sn, **_finalize(sv)}
                for sn, sv in sorted(c["subs"].items(),
                                     key=lambda kv: kv[1]["total_pnl"], reverse=True)]
        by_category.append({"category": name, **_finalize(c["bucket"]), "subcategories": subs})

    return {
        "address": address,
        "n_markets": overall["markets"],
        "n_trades": n_trades_total,
        "overall": _finalize(overall),
        "by_category": by_category,
    }


def attach_subcategories(records: list[dict]) -> list[dict]:
    for r in records:
        r["subcategory"] = sc.classify(
            r.get("category", ""), r.get("title", ""), r.get("slug", ""), r.get("eventSlug", ""))
    return records


def analyze(address: str, trade_limit: int = 2000, enrich_tags: bool = False,
            debug: bool = False) -> dict:
    """Full live analysis for a wallet address. Raises on a hard API failure."""
    api = wa.APIClient(debug=debug)
    positions = wa.fetch_positions(api, address)
    trades = wa.fetch_trades(api, address, trade_limit)
    trade_pnl = wa.reconstruct_trade_pnl(trades)
    records = wa.build_market_records(positions, trade_pnl, api if enrich_tags else None)
    attach_subcategories(records)
    report = rollup(address, records, len(trades))
    report["markets"] = records          # per-market detail (for an optional drill-in)
    return report
