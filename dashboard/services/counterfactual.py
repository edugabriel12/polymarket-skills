"""Counterfactual replay service — Tier 4B.

Operator picks an entry_id + sliders for cashout-policy params; we
replay that entry's monitor_checks timeline through
weather_edge_backtest.replay_entry() and return the simulated exit
+ delta vs realized.

Backend is stateless — every slider change triggers a fresh replay.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Optional

from .. import settings as S


def _ensure_backtest_on_path() -> None:
    p = Path(__file__).resolve().parent.parent.parent \
        / "polymarket-analyzer" / "scripts"
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


def _ro_conn(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(str(path))
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def get_entry_baseline(entry_id: int) -> Optional[dict]:
    """Return the realized data + monitor_checks timeline for an entry,
    plus realized cashout/resolution if any. Used by the modal to show
    what actually happened."""
    if not S.WEATHER_EDGE_DB.exists():
        return None
    conn = _ro_conn(S.WEATHER_EDGE_DB)
    try:
        entry = conn.execute(
            "SELECT * FROM entries WHERE entry_id = ?", (entry_id,)
        ).fetchone()
        if not entry:
            return None
        checks = conn.execute(
            "SELECT ts, market_best_bid, forecast_prob_now, decision "
            "FROM monitor_checks WHERE entry_id = ? ORDER BY ts ASC",
            (entry_id,),
        ).fetchall()
        cashout = conn.execute(
            "SELECT * FROM cashouts WHERE entry_id = ? LIMIT 1", (entry_id,)
        ).fetchone()
        resolution = conn.execute(
            "SELECT * FROM resolutions WHERE entry_id = ? LIMIT 1", (entry_id,)
        ).fetchone()
    finally:
        conn.close()

    return {
        "entry": dict(entry),
        "checks": [dict(c) for c in checks],
        "cashout": dict(cashout) if cashout else None,
        "resolution": dict(resolution) if resolution else None,
    }


def replay_with_params(entry_id: int, *,
                       profit_lock_pp: float = 50.0,
                       trailing_drawdown_pct: float = 30.0,
                       trailing_min_gain_pp: float = 20.0,
                       convergence_pp: float = 5.0,
                       bid_slippage_pct: float = 0.0,
                       fee_rate: float = 0.0) -> Optional[dict]:
    """Replay one entry's lifecycle with custom params. Returns dict:
      {realized: {...}, simulated: {...}, delta_usd, chart_data}
    """
    baseline = get_entry_baseline(entry_id)
    if baseline is None:
        return None

    _ensure_backtest_on_path()
    try:
        from weather_edge_backtest import BacktestParams, replay_entry
    except ImportError as e:
        return {"error": f"backtest module import failed: {e}"}

    params = BacktestParams(
        profit_lock_pp=profit_lock_pp,
        trailing_drawdown_pct=trailing_drawdown_pct,
        trailing_min_gain_pp=trailing_min_gain_pp,
        convergence_pp=convergence_pp,
        bid_slippage_pct=bid_slippage_pct,
        fee_rate=fee_rate,
    )
    sim = replay_entry(baseline["entry"], baseline["checks"],
                        baseline["resolution"], params)

    # Compute realized P&L from cashout or resolution
    realized_pnl = None
    realized_trigger = None
    realized_exit_ts = None
    realized_exit_price = None
    if baseline["cashout"]:
        realized_pnl = baseline["cashout"].get("realized_pnl_usd")
        realized_trigger = baseline["cashout"].get("trigger")
        realized_exit_ts = baseline["cashout"].get("ts")
        realized_exit_price = baseline["cashout"].get("exit_price")
    elif baseline["resolution"]:
        e = baseline["entry"]
        payout = float(baseline["resolution"].get("payout_per_share") or 0)
        shares = float(e.get("size_shares") or 0)
        size_usd = float(e.get("size_usd") or 0)
        realized_pnl = round(payout * shares - size_usd, 2)
        realized_trigger = "hold_to_resolution"

    sim_pnl = sim.get("sim_pnl_usd")
    delta_usd = (sim_pnl - realized_pnl) if (sim_pnl is not None
                                              and realized_pnl is not None) else None

    # Chart data: bid timeline + 2 vertical markers
    chart_points = [
        {"ts": c["ts"], "bid": c["market_best_bid"],
         "forecast": c["forecast_prob_now"]}
        for c in baseline["checks"] if c.get("market_best_bid")
    ]

    return {
        "entry_id": entry_id,
        "realized": {
            "exit_ts": realized_exit_ts,
            "exit_price": realized_exit_price,
            "trigger": realized_trigger,
            "pnl_usd": realized_pnl,
        },
        "simulated": {
            "exit_ts": sim.get("sim_exit_ts"),
            "exit_price": sim.get("sim_exit_bid"),
            "trigger": sim.get("sim_trigger"),
            "pnl_usd": sim_pnl,
        },
        "delta_usd": round(delta_usd, 2) if delta_usd is not None else None,
        "chart_points": chart_points,
        "params": {
            "profit_lock_pp": profit_lock_pp,
            "trailing_drawdown_pct": trailing_drawdown_pct,
            "trailing_min_gain_pp": trailing_min_gain_pp,
            "convergence_pp": convergence_pp,
            "bid_slippage_pct": bid_slippage_pct,
            "fee_rate": fee_rate,
        },
        "entry_meta": {
            "market_question": baseline["entry"]["market_question"],
            "city": baseline["entry"].get("city_resolved"),
            "side": baseline["entry"].get("side"),
            "entry_price": baseline["entry"].get("entry_price"),
            "size_usd": baseline["entry"].get("size_usd"),
        },
    }


# Inline test
if __name__ == "__main__":
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    db_path = tmp / "weather_edge.db"

    # Build minimal schema + 1 entry + 3 monitor_checks + 1 resolution
    c = sqlite3.connect(db_path)
    c.executescript("""
        CREATE TABLE entries (
            entry_id INTEGER PRIMARY KEY,
            ts TEXT, market_slug TEXT, market_question TEXT,
            side TEXT, entry_price REAL,
            size_shares REAL, size_usd REAL,
            forecast_prob_at_entry REAL, status TEXT,
            city_resolved TEXT
        );
        CREATE TABLE monitor_checks (
            check_id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id INTEGER, ts TEXT,
            market_best_bid REAL, forecast_prob_now REAL, decision TEXT
        );
        CREATE TABLE cashouts (
            cashout_id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id INTEGER, ts TEXT,
            exit_price REAL, realized_pnl_usd REAL, trigger TEXT
        );
        CREATE TABLE resolutions (
            resolution_id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id INTEGER, ts_resolved TEXT,
            final_outcome TEXT, payout_per_share REAL
        );
    """)
    c.execute("INSERT INTO entries VALUES (1, '2026-05-01T00:00:00Z', "
              "'slug', 'Q?', 'NO', 0.30, 100, 30.0, 0.95, 'EXECUTED', 'Tokyo')")
    for ts, bid in [
        ("2026-05-01T01:00:00Z", 0.40),
        ("2026-05-01T02:00:00Z", 0.55),
        ("2026-05-01T03:00:00Z", 0.50),
    ]:
        c.execute("INSERT INTO monitor_checks (entry_id, ts, market_best_bid, "
                  "forecast_prob_now, decision) VALUES (1, ?, ?, 0.95, 'HOLD')",
                  (ts, bid))
    c.execute("INSERT INTO resolutions (entry_id, ts_resolved, final_outcome, "
              "payout_per_share) VALUES (1, '2026-05-02T00:00:00Z', 'NO', 1.0)")
    c.commit(); c.close()

    S.WEATHER_EDGE_DB = db_path

    # Replay with default params (no cashout, hold to resolution)
    r = replay_with_params(1)
    assert r["simulated"]["trigger"] == "hold_to_resolution", r
    # 100 shares * payout 1.0 = 100; cost 30 → pnl 70
    assert abs(r["simulated"]["pnl_usd"] - 70) < 0.01
    print(f"Test 1 PASS: default replay (hold) → pnl {r['simulated']['pnl_usd']}")

    # Replay with profit_lock_pp=20 (cap = 0.30 + 0.20 = 0.50, fires at bid 0.55 first check)
    r = replay_with_params(1, profit_lock_pp=20)
    assert r["simulated"]["trigger"] == "profit_lock", r
    # Cashout at 0.55, pnl = (0.55 - 0.30) * 100 = 25
    assert abs(r["simulated"]["pnl_usd"] - 25) < 0.01, r
    print(f"Test 2 PASS: profit_lock=20 → pnl {r['simulated']['pnl_usd']}")

    # Slippage 10% on the cashout
    r = replay_with_params(1, profit_lock_pp=20, bid_slippage_pct=10)
    # effective_bid = 0.55 * 0.9 = 0.495 → pnl = (0.495 - 0.30) * 100 = 19.5
    assert abs(r["simulated"]["pnl_usd"] - 19.5) < 0.01, r
    print(f"Test 3 PASS: with 10% slippage → pnl {r['simulated']['pnl_usd']}")

    # Chart points present
    assert len(r["chart_points"]) == 3
    print(f"Test 4 PASS: chart has {len(r['chart_points'])} points")

    print("\nAll counterfactual tests PASS")
