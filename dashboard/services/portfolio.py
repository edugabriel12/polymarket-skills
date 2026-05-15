"""Portfolio service — KPIs derived from paper_engine portfolio DB
and weather_edge DB. Read-only. No live Polymarket API calls (uses
the most recent bid recorded by the bot's monitor instead, to avoid
hammering the upstream API on each dashboard refresh).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .. import settings as S


def _ro_conn(path: Path) -> sqlite3.Connection:
    """Open SQLite in read-only mode. WAL-friendly with concurrent writers."""
    if not path.exists():
        raise FileNotFoundError(str(path))
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _latest_bid(weather_conn: sqlite3.Connection, entry_id: int) -> Optional[float]:
    row = weather_conn.execute(
        "SELECT market_best_bid FROM monitor_checks "
        "WHERE entry_id = ? ORDER BY ts DESC LIMIT 1",
        (entry_id,),
    ).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def get_kpis(portfolio_name: str = "default") -> dict:
    """Return 4 KPI numbers for the overview header.

    Keys: portfolio_total_usd, portfolio_delta_today_usd, open_positions,
          max_positions, realized_pnl_today_usd, drawdown_pct_from_peak,
          drawdown_peak_usd.
    """
    # Cash balance + open positions value (using last known bid from monitor_checks)
    pconn = _ro_conn(S.PORTFOLIO_DB)
    try:
        pf = pconn.execute(
            "SELECT * FROM portfolios WHERE name = ?", (portfolio_name,),
        ).fetchone()
        if not pf:
            return _empty_kpis()
        cash = float(pf["cash_balance"])
        pid = pf["id"]
        starting = float(pf["starting_balance"])

        # positions columns (paper_engine schema): shares, avg_entry, current_price, closed
        positions = pconn.execute(
            "SELECT * FROM positions WHERE portfolio_id = ? AND closed = 0",
            (pid,),
        ).fetchall()
        positions_value = sum(
            float(p["shares"]) * float(p["current_price"] or p["avg_entry"])
            for p in positions
        )
        open_count = len(positions)

        # Realized P&L today (UTC) — paper_engine's trades table doesn't store
        # per-trade realized_pnl, so we pull from weather_edge.db cashouts
        # (which is what the bot uses for its own monitor cashouts and where
        # realized_pnl_usd is computed at exit time).
        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        realized_today = 0.0
        if S.WEATHER_EDGE_DB.exists():
            wconn = _ro_conn(S.WEATHER_EDGE_DB)
            try:
                row = wconn.execute(
                    "SELECT COALESCE(SUM(realized_pnl_usd), 0) "
                    "FROM cashouts WHERE DATE(ts) = ?",
                    (today_utc,),
                ).fetchone()
                realized_today = float(row[0] or 0)
            finally:
                wconn.close()

        # Yesterday equity (closest daily snapshot) for delta_today
        delta_today = None
        snap = pconn.execute(
            "SELECT total_value FROM daily_snapshots "
            "WHERE portfolio_id = ? ORDER BY date DESC LIMIT 1",
            (pid,),
        ).fetchone()
        total = cash + positions_value
        if snap and snap[0]:
            delta_today = total - float(snap[0])

        # Drawdown from peak (use max of all daily snapshots + current)
        peak_row = pconn.execute(
            "SELECT MAX(total_value) FROM daily_snapshots WHERE portfolio_id = ?",
            (pid,),
        ).fetchone()
        peak = max(float(peak_row[0] or starting), starting, total)
        dd_pct = ((total - peak) / peak * 100) if peak > 0 else 0.0

        # Read from paper_engine's DEFAULT_RISK so dashboard always reflects
        # the current configured cap, even if the operator changes it.
        try:
            import paper_engine  # noqa
            max_pos = int(paper_engine.DEFAULT_RISK.get("max_concurrent_positions", 30))
        except Exception:
            max_pos = 30  # safe fallback matching current default

        return {
            "portfolio_total_usd": round(total, 2),
            "portfolio_delta_today_usd": (round(delta_today, 2)
                                          if delta_today is not None else None),
            "open_positions": open_count,
            "max_positions": max_pos,
            "realized_pnl_today_usd": round(realized_today, 2),
            "drawdown_pct_from_peak": round(dd_pct, 2),
            "drawdown_peak_usd": round(peak, 2),
            "cash_usd": round(cash, 2),
            "positions_value_usd": round(positions_value, 2),
            "starting_balance_usd": round(starting, 2),
        }
    finally:
        pconn.close()


def _empty_kpis() -> dict:
    return {
        "portfolio_total_usd": 0.0,
        "portfolio_delta_today_usd": None,
        "open_positions": 0,
        "max_positions": 30,
        "realized_pnl_today_usd": 0.0,
        "drawdown_pct_from_peak": 0.0,
        "drawdown_peak_usd": 0.0,
        "cash_usd": 0.0,
        "positions_value_usd": 0.0,
        "starting_balance_usd": 0.0,
    }


def get_judge_kpis(days: int = 30) -> dict:
    """v6: Surface judge accuracy metrics on the overview page.

    Returns 4 numbers + status flags so the kpi_cards template can color
    them. Empty/error gracefully when there are no judge reviews yet.
    """
    if not S.WEATHER_EDGE_DB.exists():
        return {"available": False, "reason": "weather_edge.db not found"}

    # Lazy import — analyzer module isn't always on the path during tests
    import sys
    analyzer_path = Path(__file__).resolve().parent.parent.parent \
        / "polymarket-analyzer" / "scripts"
    if str(analyzer_path) not in sys.path:
        sys.path.insert(0, str(analyzer_path))
    try:
        from weather_edge_analyzer import compute_judge_accuracy
    except ImportError as e:
        return {"available": False, "reason": f"import failed: {e}"}

    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = _ro_conn(S.WEATHER_EDGE_DB)
    try:
        acc = compute_judge_accuracy(conn, since)
    finally:
        conn.close()

    if acc.get("n_reviews", 0) == 0:
        return {"available": False,
                "reason": acc.get("note", "no judge reviews in window")}

    brier = acc.get("brier_score")
    appr = acc.get("approval_rate") or 0
    fpr = acc.get("false_positive_rate")
    fnr = acc.get("false_negative_rate")

    # Color coding
    def _brier_color(b):
        if b is None: return "muted"
        if b < 0.20: return "up"
        if b < 0.30: return "warn"
        return "down"

    def _approval_color(a):
        if a > 0.70 or a < 0.20: return "warn"
        return ""

    def _fpr_color(f):
        if f is None: return "muted"
        if f > 0.50: return "down"
        if f > 0.30: return "warn"
        return ""

    return {
        "available": True,
        "days": days,
        "n_reviews": acc["n_reviews"],
        "n_resolved": acc.get("n_resolved", 0),
        "brier": brier,
        "brier_color": _brier_color(brier),
        "approval_rate": appr,
        "approval_color": _approval_color(appr),
        "false_positive_rate": fpr,
        "fpr_color": _fpr_color(fpr),
        "false_negative_rate": fnr,
        "missed_pnl_usd": acc.get("missed_pnl_from_rejects_usd", 0),
        "n_flags": len(acc.get("interpretation", [])),
    }


def get_queue_health() -> dict:
    """v6: Queue depth + aging + skipped reasons today (UTC).

    Critical observability after edge-recheck v6: operator needs to see
    if entries are aging in the queue or being skipped en masse.
    """
    if not S.WEATHER_EDGE_DB.exists():
        return {"available": False}

    conn = _ro_conn(S.WEATHER_EDGE_DB)
    try:
        try:
            # Queue depths
            proposed = conn.execute(
                "SELECT COUNT(*) FROM entries WHERE status = 'PROPOSED'"
            ).fetchone()[0]
            approved = conn.execute(
                "SELECT COUNT(*) FROM entries "
                "WHERE status IN ('APPROVED', 'ADJUSTED')"
            ).fetchone()[0]

            # Age of oldest pending (PROPOSED or APPROVED/ADJUSTED)
            oldest = conn.execute(
                "SELECT MIN(ts) FROM entries "
                "WHERE status IN ('PROPOSED', 'APPROVED', 'ADJUSTED')"
            ).fetchone()[0]
            oldest_age_min = None
            if oldest:
                try:
                    ts = datetime.fromisoformat(oldest.replace("Z", "+00:00"))
                    oldest_age_min = int(
                        (datetime.now(timezone.utc) - ts).total_seconds() / 60)
                except (ValueError, TypeError):
                    pass

            # Skipped breakdown today (UTC)
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            skipped_rows = conn.execute(
                "SELECT COALESCE(skip_reason, 'unknown') as reason, "
                "COUNT(*) as n "
                "FROM entries WHERE status = 'SKIPPED' AND DATE(ts) = ? "
                "GROUP BY reason ORDER BY n DESC",
                (today,),
            ).fetchall()
        except sqlite3.OperationalError:
            # entries table missing or schema older than v1 — degrade
            return {"available": False, "reason": "entries table missing"}
        by_reason = [{"reason": r["reason"], "n": r["n"]} for r in skipped_rows]
        n_skipped_today = sum(r["n"] for r in by_reason)

    finally:
        conn.close()

    # Aging severity
    age_color = ""
    if oldest_age_min is not None:
        if oldest_age_min > 120:
            age_color = "down"   # > 2h old
        elif oldest_age_min > 30:
            age_color = "warn"

    return {
        "available": True,
        "n_proposed": proposed,
        "n_approved_waiting": approved,
        "oldest_age_min": oldest_age_min,
        "age_color": age_color,
        "n_skipped_today": n_skipped_today,
        "skipped_by_reason": by_reason,
    }


def get_equity_curve(days: int = 30) -> list[dict]:
    """Return daily snapshots for line-chart of equity over time."""
    try:
        conn = _ro_conn(S.PORTFOLIO_DB)
    except FileNotFoundError:
        return []
    try:
        rows = conn.execute(
            "SELECT date, total_value, cash_balance, positions_value "
            "FROM daily_snapshots ORDER BY date DESC LIMIT ?",
            (days,),
        ).fetchall()
        return [
            {"date": r["date"], "total_value": float(r["total_value"]),
             "cash_balance": float(r["cash_balance"]),
             "positions_value": float(r["positions_value"])}
            for r in reversed(rows)
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Inline tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile

    # Build a synthetic portfolio.db
    tmp = Path(tempfile.mkdtemp())
    db_path = tmp / "portfolio.db"
    c = sqlite3.connect(db_path)
    # Use real paper_engine schema: avg_entry (not entry_price), no realized_pnl
    c.executescript("""
        CREATE TABLE portfolios (id INTEGER PRIMARY KEY, name TEXT,
            cash_balance REAL, starting_balance REAL);
        CREATE TABLE positions (id INTEGER PRIMARY KEY, portfolio_id INTEGER,
            shares REAL, avg_entry REAL, current_price REAL, closed INTEGER);
        CREATE TABLE trades (id INTEGER PRIMARY KEY, portfolio_id INTEGER,
            executed_at TEXT);
        CREATE TABLE daily_snapshots (id INTEGER PRIMARY KEY,
            portfolio_id INTEGER, date TEXT, total_value REAL,
            cash_balance REAL, positions_value REAL);
        INSERT INTO portfolios VALUES (1, 'default', 800.0, 1000.0);
        INSERT INTO positions VALUES (1, 1, 100, 0.13, 0.20, 0);
        INSERT INTO positions VALUES (2, 1, 50, 0.50, 0.55, 0);
        INSERT INTO daily_snapshots VALUES (1, 1, '2026-05-10', 850.0, 800, 50);
        INSERT INTO daily_snapshots VALUES (2, 1, '2026-05-11', 900.0, 800, 100);
    """)
    c.commit()
    c.close()

    # Seed a minimal weather_edge.db so realized_pnl_today is non-zero
    w_path = tmp / "weather_edge.db"
    w = sqlite3.connect(w_path)
    w.executescript("""
        CREATE TABLE cashouts (cashout_id INTEGER PRIMARY KEY,
            ts TEXT, realized_pnl_usd REAL);
    """)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%dT12:00:00+00:00")
    w.execute("INSERT INTO cashouts VALUES (1, ?, 5.50)", (today,))
    w.execute("INSERT INTO cashouts VALUES (2, ?, -2.10)", (today,))
    w.execute("INSERT INTO cashouts VALUES (3, '2026-05-10T10:00:00+00:00', 99.0)")
    w.commit()
    w.close()

    # Monkeypatch settings
    S.PORTFOLIO_DB = db_path
    S.WEATHER_EDGE_DB = w_path
    k = get_kpis()
    # cash 800 + positions (100*0.20 + 50*0.55 = 20 + 27.5 = 47.5) → total 847.5
    assert abs(k["portfolio_total_usd"] - 847.5) < 0.01, k
    assert k["open_positions"] == 2
    assert k["max_positions"] == 30
    # today's cashouts: 5.50 + (-2.10) = 3.40
    assert abs(k["realized_pnl_today_usd"] - 3.40) < 0.01, k
    assert abs(k["portfolio_delta_today_usd"] - (-52.5)) < 0.01, k
    assert abs(k["drawdown_pct_from_peak"] - -15.25) < 0.1, k
    print(f"Test 1 PASS: get_kpis → {k}")

    eq = get_equity_curve(days=10)
    assert len(eq) == 2
    assert eq[0]["date"] == "2026-05-10"
    assert eq[1]["date"] == "2026-05-11"
    print(f"Test 2 PASS: equity_curve → {len(eq)} points")
    print("All portfolio tests PASS")
