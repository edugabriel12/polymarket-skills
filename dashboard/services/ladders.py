"""v9: ladder strategy KPIs + per-group view for the dashboard.

Wraps weather_edge_analyzer.compute_ladder_breakdown into a compact
shape suitable for the overview KPI strip and a dedicated ladder
table. Read-only.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from .. import settings as S  # noqa: F401

import weather_edge_analyzer as wea  # noqa: E402


def _ro_conn(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(str(path))
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def get_ladder_kpis(days: int = 7) -> dict:
    """Compact ladder strategy KPI bundle for the overview strip.

    Returns:
      {available: bool, n_3bin, n_2bin, n_orphans, n_total_groups,
       ladder_pnl_usd, ladder_pnl_per_dollar, single_pnl_per_dollar,
       short_ttr_win_rate, days}
    """
    try:
        conn = _ro_conn(S.WEATHER_EDGE_DB)
    except FileNotFoundError:
        return {"available": False, "reason": "weather_edge.db not found"}
    try:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        b = wea.compute_ladder_breakdown(conn, since)
        if b.get("schema_status") == "pre_v9_no_ladder_columns":
            return {"available": False, "reason": "DB pre-v9 (no ladder cols)"}

        funnel = b["formation_funnel"]
        n_3bin = funnel.get("3bin_full", 0)
        n_2bin = funnel.get("2bin_partial", 0)
        n_orphans = funnel.get("single_orphan", 0)
        n_total_groups = n_3bin + n_2bin

        lperf = b["ladder_groups_performance"]
        sperf = b["single_bin_performance"]

        ttr_6_12 = b["ttr_cohort_performance"].get("6-12h", {})
        short_wr = ttr_6_12.get("win_rate")

        # Decision colors for the UI to highlight outliers
        def _winrate_color(wr):
            if wr is None: return "muted"
            if wr >= 0.55: return "up"
            if wr <= 0.40: return "down"
            return "muted"

        def _pnl_color(pnl):
            if pnl is None or pnl == 0: return "muted"
            return "up" if pnl > 0 else "down"

        return {
            "available": True,
            "days": days,
            "n_3bin": n_3bin,
            "n_2bin": n_2bin,
            "n_orphans": n_orphans,
            "n_total_groups": n_total_groups,
            "ladder_pnl_usd": lperf.get("total_pnl_usd"),
            "ladder_pnl_per_dollar": lperf.get("pnl_per_dollar"),
            "ladder_win_rate": lperf.get("win_rate"),
            "single_pnl_usd": sperf.get("total_pnl_usd"),
            "single_pnl_per_dollar": sperf.get("pnl_per_dollar"),
            "single_win_rate": sperf.get("win_rate"),
            "short_ttr_n": ttr_6_12.get("n", 0),
            "short_ttr_win_rate": short_wr,
            "short_ttr_color": _winrate_color(short_wr),
            "ladder_pnl_color": _pnl_color(lperf.get("total_pnl_usd")),
            "atomic_failures": sum(b["atomic_gate_failures"].values()),
            "interpretation": b.get("interpretation", []),
        }
    finally:
        conn.close()


def get_open_ladder_groups() -> list[dict]:
    """List currently-open ladder groups with per-leg breakdown.

    A group is "open" when at least one of its legs is EXECUTED and
    has no cashout row yet.
    """
    try:
        conn = _ro_conn(S.WEATHER_EDGE_DB)
    except FileNotFoundError:
        return []
    try:
        # Defensive: pre-v9 schemas
        try:
            conn.execute("SELECT ladder_group_id FROM entries LIMIT 1")
        except Exception:
            return []

        rows = conn.execute("""
            SELECT e.entry_id, e.ladder_group_id, e.ladder_position,
                   e.ladder_event_slug, e.market_slug, e.market_question,
                   e.city_resolved, e.side, e.entry_price, e.size_usd,
                   e.threshold_value, e.threshold_unit, e.end_date,
                   e.ladder_stake_usd, e.status,
                   c.cashout_id
            FROM entries e
            LEFT JOIN cashouts c ON c.entry_id = e.entry_id
            WHERE e.ladder_group_id IS NOT NULL
              AND e.status = 'EXECUTED' AND c.cashout_id IS NULL
            ORDER BY e.ladder_group_id, e.ladder_position
        """).fetchall()

        # Group legs by ladder_group_id
        groups: dict[str, dict] = {}
        for r in rows:
            gid = r["ladder_group_id"]
            if gid not in groups:
                groups[gid] = {
                    "ladder_group_id": gid,
                    "event_slug": r["ladder_event_slug"],
                    "city": r["city_resolved"],
                    "end_date": r["end_date"],
                    "threshold_unit": r["threshold_unit"],
                    "legs": [],
                    "total_stake_usd": 0.0,
                }
            groups[gid]["legs"].append({
                "entry_id": r["entry_id"],
                "position": r["ladder_position"],
                "side": r["side"],
                "threshold_value": r["threshold_value"],
                "entry_price": float(r["entry_price"] or 0),
                "size_usd": float(r["size_usd"] or 0),
                "ladder_stake_usd": (float(r["ladder_stake_usd"])
                                      if r["ladder_stake_usd"] is not None
                                      else None),
            })
            groups[gid]["total_stake_usd"] += float(r["size_usd"] or 0)

        out = list(groups.values())
        # Sort: largest stake first
        out.sort(key=lambda g: g["total_stake_usd"], reverse=True)
        for g in out:
            g["total_stake_usd"] = round(g["total_stake_usd"], 2)
            g["n_legs"] = len(g["legs"])
            g["leg_label"] = (f"{g['n_legs']}-bin"
                              if g["n_legs"] >= 2 else "1-bin (partial)")
        return out
    finally:
        conn.close()
