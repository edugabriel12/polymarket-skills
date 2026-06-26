#!/usr/bin/env python3
"""Combined results over settled entries — model + wallets together, never split by
source. Everything is normalized to the Unidade Sugerida, so P&L and ROI are in
UNITS (a won 1U bet at odds 1.8 → +0.8U; a lost 1U → −1U; VOID → 0).

Grouped: overall + by category (each nesting its unit split) + by unit (1U/0.5U/0.25U).
"""

from __future__ import annotations

UNIT_LABEL = {1.0: "1U", 0.5: "0.5U", 0.25: "0.25U"}
_UNIT_ORDER = {1.0: 0, 0.5: 1, 0.25: 2}


def unit_label(u) -> str:
    return UNIT_LABEL.get(round(float(u or 0), 2), f"{u}U")


def _unit_pnl(e: dict) -> float:
    u = float(e.get("unit") or 0)
    odds = float(e.get("odds") or 0)
    s = e.get("status")
    if s == "WON":
        return u * (odds - 1)
    if s == "LOST":
        return -u
    return 0.0                          # VOID


def _blank() -> dict:
    return {"n_bets": 0, "wins": 0, "losses": 0, "voids": 0, "staked_u": 0.0, "pnl_u": 0.0}


def _add(b: dict, e: dict) -> None:
    b["n_bets"] += 1
    b["staked_u"] += float(e.get("unit") or 0)
    b["pnl_u"] += _unit_pnl(e)
    s = e.get("status")
    if s == "WON":
        b["wins"] += 1
    elif s == "LOST":
        b["losses"] += 1
    else:
        b["voids"] += 1


def _finalize(b: dict) -> dict:
    decided = b["wins"] + b["losses"]
    b["win_rate"] = round(b["wins"] / decided, 4) if decided else None
    b["roi"] = round(b["pnl_u"] / b["staked_u"], 4) if b["staked_u"] > 0 else None
    b["staked_u"] = round(b["staked_u"], 3)
    b["pnl_u"] = round(b["pnl_u"], 3)
    return b


def _group(entries, keyfn) -> dict:
    out: dict = {}
    for e in entries:
        out.setdefault(keyfn(e), []).append(e)
    return out


def _metrics(entries) -> dict:
    b = _blank()
    for e in entries:
        _add(b, e)
    return _finalize(b)


def _by_unit(entries) -> list[dict]:
    groups = _group(entries, lambda e: round(float(e.get("unit") or 0), 2))
    return [{"unit": u, "unit_label": unit_label(u), **_metrics(es)}
            for u, es in sorted(groups.items(), key=lambda kv: _UNIT_ORDER.get(kv[0], 9))]


def combined(entries: list[dict]) -> dict:
    """overall + by_category (each with by_unit) + by_unit, all unit-based."""
    by_category = []
    for cat, es in _group(entries, lambda e: e.get("category") or "Other").items():
        by_category.append({"category": cat, **_metrics(es), "by_unit": _by_unit(es)})
    by_category.sort(key=lambda c: c["pnl_u"], reverse=True)
    return {
        "n_bets": len(entries),
        "overall": _metrics(entries),
        "by_unit": _by_unit(entries),
        "by_category": by_category,
    }
