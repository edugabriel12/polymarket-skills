"""v9: ladder strategy KPIs + per-group view for the dashboard.

Wraps weather_edge_analyzer.compute_ladder_breakdown into a compact
shape suitable for the overview KPI strip and a dedicated ladder
table. Read-only.
"""

from __future__ import annotations

import json
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

    A group is "open" when at least one of its legs is EXECUTED (or
    FAST_PATH) and has no cashout row AND no resolution row yet.

    v13.7 (2026-07-05): legs settled via `resolutions` (held to expiry,
    payout_per_share) now count as closed — before, only cashouts closed a
    leg, so resolution-settled groups (e.g. Los Angeles 2/jul) stayed
    "open" forever, inflating the "Open groups now" KPI and total stake.
    Mirrors query_open_positions (weather_edge_db.py).
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
            LEFT JOIN resolutions r ON r.entry_id = e.entry_id
            WHERE e.ladder_group_id IS NOT NULL
              AND e.status IN ('EXECUTED', 'FAST_PATH')
              AND c.cashout_id IS NULL
              AND r.resolution_id IS NULL
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


# Event types that constitute the atomic execution + atomic cashout trail.
# The bot emits these from _execute_ladder_group_atomic, _ladder_atomic_gate
# (indirectly via gating decisions logged on skip), _do_ladder_cashout, etc.
_LADDER_EVENT_TYPES = {
    "ladder_built",
    "ladder_dropped",
    "ladder_group_dead",
    "ladder_group_dead_prejudge",   # v15 F2: morte judge-side, pré-gasto
    "ladder_aborted",
    "ladder_partial_execution",
    "ladder_executed",
    "ladder_executed_dry",
    "ladder_cashout_executed",
    "ladder_cashout_dry",
    "ladder_leg_close_rejected",
}


def get_atomic_trail(entry_id: int, max_events: int = 200) -> dict:
    """v9: read the JSONL event log and return all events tied to the
    ladder group this entry belongs to. Used by the replay modal to
    show the atomic gate decisions (READY/DEFER/DEAD), atomic execution,
    atomic cashout for the whole group.

    Returns {"group_id": str|None, "events": [...]}. If the entry has no
    ladder_group_id, events list is empty.
    """
    # First resolve entry → group_id from the DB
    try:
        conn = _ro_conn(S.WEATHER_EDGE_DB)
    except FileNotFoundError:
        return {"group_id": None, "events": []}
    try:
        try:
            row = conn.execute(
                "SELECT ladder_group_id FROM entries WHERE entry_id = ?",
                (entry_id,)).fetchone()
        except Exception:
            return {"group_id": None, "events": []}
        if not row or not row["ladder_group_id"]:
            return {"group_id": None, "events": []}
        group_id = row["ladder_group_id"]
    finally:
        conn.close()

    # Scan the JSONL log for events whose payload mentions this group_id.
    # We import settings lazily because S is already loaded above.
    log_path = getattr(S, "JSONL_PATH", None)
    if log_path is None or not Path(log_path).exists():
        return {"group_id": group_id, "events": []}

    matched = []
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or "ladder" not in line:
                    continue
                # Cheap pre-filter: skip lines that don't mention the group_id
                if group_id not in line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("event_type") not in _LADDER_EVENT_TYPES:
                    continue
                payload = ev.get("payload") or {}
                if payload.get("ladder_group_id") != group_id:
                    continue
                matched.append({
                    "ts": ev.get("ts", "")[:19].replace("T", " "),
                    "level": ev.get("level", "INFO"),
                    "event_type": ev.get("event_type"),
                    "payload": payload,
                })
    except OSError:
        return {"group_id": group_id, "events": []}

    # Return ordered chronologically, cap to max_events
    return {"group_id": group_id, "events": matched[:max_events]}


def get_resolved_ladder_history(limit: int = 50) -> list[dict]:
    """Settled ladder groups with realized P&L summed across settled legs.
    Most recent groups first.

    v13.7 (2026-07-05): legs settled via `resolutions` now count — before,
    the inner JOIN on cashouts meant resolution-settled ladders never
    reached the history and their payout P&L was ignored. Per-leg
    precedence mirrors the v13.6 Win Rate by City semantics: cashout
    (realized P&L governs; covers legs with cashout AND resolution without
    double count) → resolution ((payout - entry) * shares). A group shows
    here once it has >= 1 settled leg (partially-settled groups appear in
    both Open and history — pre-existing behavior for cashouts).
    """
    try:
        conn = _ro_conn(S.WEATHER_EDGE_DB)
    except FileNotFoundError:
        return []
    try:
        try:
            conn.execute("SELECT ladder_group_id FROM entries LIMIT 1")
        except Exception:
            return []

        # Aggregate by group over SETTLED legs only: sum P&L (cashout
        # precedence, else resolution payout), count legs, latest event ts.
        rows = conn.execute("""
            SELECT e.ladder_group_id,
                   e.ladder_event_slug,
                   e.city_resolved,
                   e.end_date,
                   COUNT(*) n_legs,
                   SUM(e.size_usd) total_stake,
                   SUM(COALESCE(c.realized_pnl_usd,
                                (r.payout_per_share - e.entry_price)
                                  * e.size_shares)) total_pnl,
                   MAX(COALESCE(c.ts, r.ts_resolved)) last_cashout_ts,
                   MAX(COALESCE(c.reason,
                                'resolution:' || r.final_outcome)) reason
            FROM entries e
            LEFT JOIN cashouts c ON c.entry_id = e.entry_id
            LEFT JOIN resolutions r ON r.entry_id = e.entry_id
            WHERE e.ladder_group_id IS NOT NULL
              AND e.status IN ('EXECUTED', 'FAST_PATH')
              AND (c.cashout_id IS NOT NULL OR r.resolution_id IS NOT NULL)
            GROUP BY e.ladder_group_id
            HAVING COUNT(*) > 0
            ORDER BY last_cashout_ts DESC
            LIMIT ?
        """, (limit,)).fetchall()

        out = []
        for r in rows:
            stake = float(r["total_stake"] or 0)
            pnl = float(r["total_pnl"] or 0)
            out.append({
                "ladder_group_id": r["ladder_group_id"],
                "event_slug": r["ladder_event_slug"],
                "city": r["city_resolved"],
                "end_date": r["end_date"],
                "n_legs": r["n_legs"],
                "total_stake_usd": round(stake, 2),
                "total_pnl_usd": round(pnl, 2),
                "pnl_pct": round(pnl / stake * 100, 1) if stake else None,
                "last_cashout_ts": r["last_cashout_ts"],
                "reason": (r["reason"] or "").split(":")[0],  # strip prefix
                "won": pnl > 0,
            })
        return out
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Inline tests (v13.7) — open/closed classification counts resolutions
# Run: python -m dashboard.services.ladders
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile

    tmp = Path(tempfile.mkdtemp()) / "ladders_test.db"
    conn = sqlite3.connect(tmp)
    conn.executescript("""
        CREATE TABLE entries (entry_id INTEGER PRIMARY KEY, ts TEXT,
            ladder_group_id TEXT, ladder_position TEXT,
            ladder_event_slug TEXT, market_slug TEXT, market_question TEXT,
            city_resolved TEXT, side TEXT, entry_price REAL, size_usd REAL,
            size_shares REAL, threshold_value REAL, threshold_unit TEXT,
            end_date TEXT, ladder_stake_usd REAL, status TEXT);
        CREATE TABLE cashouts (cashout_id INTEGER PRIMARY KEY,
            entry_id INTEGER, ts TEXT, realized_pnl_usd REAL, reason TEXT);
        CREATE TABLE resolutions (resolution_id INTEGER PRIMARY KEY,
            entry_id INTEGER, ts_resolved TEXT, final_outcome TEXT,
            payout_per_share REAL);
    """)

    def leg(eid, gid, pos, price, shares, status='EXECUTED', city='X'):
        conn.execute(
            "INSERT INTO entries VALUES (?, '2026-07-01T00:00:00', ?, ?, "
            "'ev-'||?, 's'||?, 'q', ?, 'NO', ?, ?, ?, 20.0, 'C', "
            "'2026-07-02', ?, ?)",
            (eid, gid, pos, gid, eid, city, price, price * shares, shares,
             price * shares, status))

    # Grupo A: 2 legs abertos (sem cashout/resolução) → OPEN, fora do histórico
    leg(1, 'gA', 'central', 0.50, 100, city='Aberta')
    leg(2, 'gA', 'below', 0.60, 50, city='Aberta')

    # Grupo B (caso LA): todos os legs resolvidos, sem cashout → FECHADO,
    # no histórico com P&L de payout. pnl = (1-0.5)*100 + (0-0.6)*50 = 50-30 = +20
    leg(3, 'gB', 'central', 0.50, 100, city='Los Angeles')
    leg(4, 'gB', 'below', 0.60, 50, status='FAST_PATH', city='Los Angeles')
    conn.execute("INSERT INTO resolutions VALUES (1, 3, '2026-07-05T02:12:00', 'NO', 1.0)")
    conn.execute("INSERT INTO resolutions VALUES (2, 4, '2026-07-05T02:12:00', 'YES', 0.0)")

    # Grupo C: 1 leg com cashout (+7) + 1 leg aberto → em OPEN (leg restante)
    # e no histórico (P&L parcial +7, precedência do cashout mesmo com resolução)
    leg(5, 'gC', 'central', 0.40, 50, city='Parcial')
    leg(6, 'gC', 'below', 0.55, 50, city='Parcial')
    conn.execute("INSERT INTO cashouts VALUES (1, 5, '2026-07-03T10:00:00', 7.0, 'convergence: x')")
    conn.execute("INSERT INTO resolutions VALUES (3, 5, '2026-07-05T02:12:00', 'NO', 1.0)")
    conn.commit()
    conn.close()

    S.WEATHER_EDGE_DB = tmp  # aponta os services para o DB de teste

    open_groups = get_open_ladder_groups()
    gids = {g["ladder_group_id"] for g in open_groups}
    assert gids == {"gA", "gC"}, gids
    print("Test 1 PASS: open = {gA, gC} — gB (resolvido) saiu de Open")

    ga = next(g for g in open_groups if g["ladder_group_id"] == "gA")
    gc = next(g for g in open_groups if g["ladder_group_id"] == "gC")
    assert ga["n_legs"] == 2 and gc["n_legs"] == 1, (ga["n_legs"], gc["n_legs"])
    print("Test 2 PASS: gA com 2 legs abertos; gC só com o leg não liquidado")

    hist = get_resolved_ladder_history()
    h = {r["ladder_group_id"]: r for r in hist}
    assert set(h) == {"gB", "gC"}, set(h)
    assert abs(h["gB"]["total_pnl_usd"] - 20.0) < 1e-6, h["gB"]
    assert h["gB"]["reason"] == "resolution", h["gB"]
    assert h["gB"]["won"] is True
    assert h["gB"]["n_legs"] == 2  # inclui o leg FAST_PATH
    print(f"Test 3 PASS: gB no histórico via resolução — pnl ${h['gB']['total_pnl_usd']} (FAST_PATH contado)")

    assert abs(h["gC"]["total_pnl_usd"] - 7.0) < 1e-6, h["gC"]  # cashout precede resolução
    assert h["gC"]["reason"] == "convergence", h["gC"]
    print(f"Test 4 PASS: gC histórico parcial — pnl ${h['gC']['total_pnl_usd']} (precedência do cashout, sem dupla contagem)")

    print("\nAll ladders tests PASS (4/4)")
