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


_CONF_ORDER = {"Alta": 0, "Média": 1, "Baixa": 2}


def _metrics(records: list[dict]) -> dict:
    b = _blank()
    for r in records:
        _add(b, r)
    return _finalize(b)


def _group(records: list[dict], keyfn) -> dict:
    out: dict = {}
    for r in records:
        out.setdefault(keyfn(r), []).append(r)
    return out


def _by_confidence(records: list[dict]) -> list[dict]:
    groups = _group(records, lambda r: r.get("confidence") or "—")
    return [{"confidence": c, **_metrics(rs)}
            for c, rs in sorted(groups.items(), key=lambda kv: _CONF_ORDER.get(kv[0], 9))]


def filter_tree(records: list[dict]) -> dict:
    """{category: {subcategory: [confidences present, in Alta/Média/Baixa order]}} — the
    options the user picks from on the Add screen. Defaults MUST match rollup_csv
    ("Other"/"Outro"/"—") so the tree keys line up 1:1 with by_category/subcategories."""
    tree: dict[str, dict] = {}
    for r in records:
        cat = r.get("category") or "Other"
        sub = r.get("subcategory") or "Outro"
        conf = r.get("confidence") or "—"
        tree.setdefault(cat, {}).setdefault(sub, set()).add(conf)
    return {cat: {sub: sorted(cs, key=lambda c: _CONF_ORDER.get(c, 9))
                  for sub, cs in subs.items()}
            for cat, subs in tree.items()}


def rollup_csv(records: list[dict]) -> dict:
    """Roll up CSV bet records: overall + by_confidence + by_category (each nesting
    its subcategories AND its own by_confidence split). A "bet" is one CSV row."""
    overall = _metrics(records)
    by_category = []
    for cat, crs in _group(records, lambda r: r.get("category") or "Other").items():
        subs = [{"subcategory": s, **_metrics(rs), "by_confidence": _by_confidence(rs)}
                for s, rs in _group(crs, lambda r: r.get("subcategory") or "Outro").items()]
        subs.sort(key=lambda x: x["total_pnl"], reverse=True)
        by_category.append({"category": cat, **_metrics(crs),
                            "subcategories": subs, "by_confidence": _by_confidence(crs)})
    by_category.sort(key=lambda x: x["total_pnl"], reverse=True)
    return {
        "source": "csv",
        "n_markets": overall["markets"],
        "n_trades": overall["markets"],          # one bet per row
        "overall": overall,
        "by_confidence": _by_confidence(records),
        "by_category": by_category,
        "filter_tree": filter_tree(records),
    }


# ---------------------------------------------------------------------------
# Merge two reports (e.g. the stored CSV rollup + a live rollup) into a TOTAL.
# Metrics are additive, so we sum the raw components at every level and re-derive
# win_rate / roi via the same _finalize used by the rollups — no raw records needed.
# ---------------------------------------------------------------------------

_ADDITIVE = ("markets", "resolved", "wins", "losses", "n_trades", "total_pnl",
             "realized_pnl", "unrealized_pnl", "invested", "current_value")


def _accumulate(bucket: dict, row: dict) -> None:
    """Add a finalized metrics row's additive components into a _blank() bucket."""
    for k in _ADDITIVE:
        bucket[k] += (row.get(k) or 0)


def _merge_conf(rows_a: list | None, rows_b: list | None) -> list[dict]:
    groups: dict[str, dict] = {}
    order: list[str] = []
    for r in (rows_a or []) + (rows_b or []):
        c = r.get("confidence")
        if c not in groups:
            groups[c] = _blank(); order.append(c)
        _accumulate(groups[c], r)
    out = [{"confidence": c, **_finalize(groups[c])} for c in order]
    out.sort(key=lambda x: _CONF_ORDER.get(x["confidence"], 9))
    return out


def _merge_subs(rows_a: list | None, rows_b: list | None) -> list[dict]:
    groups: dict[str, dict] = {}
    order: list[str] = []
    for r in (rows_a or []) + (rows_b or []):
        s = r.get("subcategory")
        if s not in groups:
            groups[s] = {"bucket": _blank(), "conf": []}; order.append(s)
        _accumulate(groups[s]["bucket"], r)
        groups[s]["conf"].extend(r.get("by_confidence") or [])
    out = [{"subcategory": s, **_finalize(groups[s]["bucket"]),
            "by_confidence": _merge_conf(groups[s]["conf"], None)} for s in order]
    out.sort(key=lambda x: x["total_pnl"], reverse=True)
    return out


def _merge_cats(rows_a: list | None, rows_b: list | None) -> list[dict]:
    groups: dict[str, dict] = {}
    order: list[str] = []
    for r in (rows_a or []) + (rows_b or []):
        c = r.get("category")
        if c not in groups:
            groups[c] = {"bucket": _blank(), "subs": [], "conf": []}; order.append(c)
        _accumulate(groups[c]["bucket"], r)
        groups[c]["subs"].extend(r.get("subcategories") or [])
        groups[c]["conf"].extend(r.get("by_confidence") or [])
    out = [{"category": c, **_finalize(groups[c]["bucket"]),
            "subcategories": _merge_subs(groups[c]["subs"], None),
            "by_confidence": _merge_conf(groups[c]["conf"], None)} for c in order]
    out.sort(key=lambda x: x["total_pnl"], reverse=True)
    return out


def merge_reports(a: dict | None, b: dict | None) -> dict:
    """Combine two rollup_csv-shaped reports into one TOTAL, summing additive metrics at every
    level (overall, by_confidence, by_category → subcategories → by_confidence) and recomputing
    win_rate/roi. Used for a wallet's TOTAL view (stored CSV rollup + all live bets). Either side
    may be {}/None (merge with empty returns the other side)."""
    a = a or {}
    b = b or {}
    overall = _blank()
    _accumulate(overall, a.get("overall") or {})
    _accumulate(overall, b.get("overall") or {})
    overall = _finalize(overall)
    return {
        "source": "combined",
        "n_markets": overall["markets"],
        "n_trades": (a.get("n_trades") or 0) + (b.get("n_trades") or 0),
        "overall": overall,
        "by_confidence": _merge_conf(a.get("by_confidence"), b.get("by_confidence")),
        "by_category": _merge_cats(a.get("by_category"), b.get("by_category")),
        "filter_tree": a.get("filter_tree") or b.get("filter_tree") or {},
        "live_settled": (a.get("live_settled") or 0) + (b.get("live_settled") or 0),
        "live_open": (a.get("live_open") or 0) + (b.get("live_open") or 0),
    }


def analyze_csv(data) -> dict:
    """Parse a bet-history CSV and roll it up. Raises on a malformed file."""
    import csv_parser
    records = csv_parser.parse_csv(data)
    report = rollup_csv(records)
    report["markets"] = records
    return report


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
