"""Open positions service — joins entries + latest monitor_check bid +
computes trigger-distance progress for the cashout policy.

Reuses query_open_positions and evaluate_cashout_triggers from the analyzer
modules. Read-only.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .. import settings as S  # noqa: F401 — primes sys.path

import weather_edge_db as wdb  # noqa: E402
from weather_edge_helpers import evaluate_cashout_triggers  # noqa: E402


def _ro_conn(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(str(path))
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _latest_bid(conn: sqlite3.Connection, entry_id: int) -> tuple[Optional[float], Optional[str]]:
    row = conn.execute(
        "SELECT market_best_bid, ts FROM monitor_checks "
        "WHERE entry_id = ? ORDER BY ts DESC LIMIT 1",
        (entry_id,),
    ).fetchone()
    if not row:
        return None, None
    return (float(row[0]) if row[0] is not None else None), row[1]


def _latest_forecast_prob_yes(conn: sqlite3.Connection,
                              entry_id: int) -> Optional[float]:
    """Get the latest forecast probability (P(YES)) from monitor_checks
    or from the entry row if no check yet."""
    row = conn.execute(
        "SELECT forecast_prob_now FROM monitor_checks "
        "WHERE entry_id = ? ORDER BY ts DESC LIMIT 1",
        (entry_id,),
    ).fetchone()
    if row and row[0] is not None:
        # forecast_prob_now is from the entry's side perspective
        # We need P(YES) for the trigger evaluator
        side_row = conn.execute(
            "SELECT side FROM entries WHERE entry_id = ?", (entry_id,),
        ).fetchone()
        if side_row and side_row[0] == "YES":
            return float(row[0])
        return 1.0 - float(row[0])
    # Fallback: use entry's forecast at proposal time
    row2 = conn.execute(
        "SELECT forecast_prob_at_entry, side FROM entries WHERE entry_id = ?",
        (entry_id,),
    ).fetchone()
    if not row2 or row2[0] is None:
        return None
    return float(row2[0])


def trigger_distances(
    side: str, entry_price: float, current_bid: float,
    peak_bid: Optional[float], forecast_prob_yes: Optional[float],
    profit_lock_pp: float = 50.0,
    trailing_drawdown_pct: float = 30.0,
    trailing_min_gain_pp: float = 20.0,
    convergence_pp: float = 5.0,
) -> dict:
    """Compute % progress toward each trigger firing. 0.0 = not started,
    1.0 = would fire now. Returns dict with one entry per trigger plus
    a 'would_fire_now' bool indicating if evaluate_cashout_triggers
    would cash out at this moment."""
    in_profit = current_bid >= entry_price
    peak = float(peak_bid) if peak_bid is not None else current_bid

    # 1. profit_lock progress
    if not in_profit:
        pl_pct = 0.0
    else:
        pl_pct = min(1.0, (current_bid - entry_price) / (profit_lock_pp / 100.0))

    # 2. trailing_stop progress
    min_gain = trailing_min_gain_pp / 100.0
    if not in_profit or peak < entry_price + min_gain:
        ts_pct = 0.0
        ts_armed = False
    else:
        ts_armed = True
        drawdown_threshold = peak * (1.0 - trailing_drawdown_pct / 100.0)
        # Progress: how close current_bid is to drawdown_threshold (from above)
        if current_bid <= drawdown_threshold:
            ts_pct = 1.0
        else:
            # Range from peak (0%) down to threshold (100%)
            span = peak - drawdown_threshold
            ts_pct = min(1.0, max(0.0, (peak - current_bid) / span)) if span > 0 else 0.0

    # 3. convergence progress
    if not in_profit or forecast_prob_yes is None:
        cv_pct = 0.0
        fair = None
    else:
        fair = forecast_prob_yes if side == "YES" else 1.0 - forecast_prob_yes
        target = fair - convergence_pp / 100.0
        if current_bid >= target:
            cv_pct = 1.0
        else:
            # Progress: bid distance from entry vs distance from entry to target
            span = target - entry_price
            cv_pct = min(1.0, max(0.0, (current_bid - entry_price) / span)) if span > 0 else 0.0

    # Forecast reversal: backstop, only fires when forecast turned against side
    fr_armed = False
    if forecast_prob_yes is not None:
        forecast_prob_now = (forecast_prob_yes if side == "YES"
                             else 1.0 - forecast_prob_yes)
        if forecast_prob_now < entry_price and current_bid >= entry_price:
            fr_armed = True

    # Final decision via the canonical evaluator
    verdict = evaluate_cashout_triggers(
        side=side, entry_price=entry_price, current_bid=current_bid,
        peak_bid_seen=peak, forecast_prob_yes=forecast_prob_yes,
        profit_lock_pp=profit_lock_pp,
        trailing_drawdown_pct=trailing_drawdown_pct,
        trailing_min_gain_pp=trailing_min_gain_pp,
        convergence_pp=convergence_pp,
    )

    return {
        "profit_lock": {"progress": round(pl_pct, 3),
                        "target_bid": round(entry_price + profit_lock_pp / 100.0, 3)},
        "trailing_stop": {"progress": round(ts_pct, 3), "armed": ts_armed,
                          "peak_bid": round(peak, 3),
                          "threshold_bid": (round(peak * (1 - trailing_drawdown_pct / 100.0), 3)
                                            if ts_armed else None)},
        "convergence": {"progress": round(cv_pct, 3),
                        "fair_value": round(fair, 3) if fair is not None else None,
                        "target_bid": round(fair - convergence_pp / 100.0, 3)
                                        if fair is not None else None},
        "forecast_reversal": {"armed": fr_armed},
        "would_fire_now": verdict["decision"] == "CASHOUT",
        "winning_trigger": verdict["trigger"],
    }


def get_open_positions() -> list[dict]:
    """Return a list of open positions with bid, peak, P&L, trigger
    distances, and time held. Sorted by entry_id DESC (most recent first)."""
    try:
        conn = _ro_conn(S.WEATHER_EDGE_DB)
    except FileNotFoundError:
        return []
    try:
        rows = wdb.query_open_positions(conn)
        out = []
        now = datetime.now(timezone.utc)
        for row in rows:
            entry_id = row["entry_id"]
            entry_price = float(row["entry_price"] or 0)
            shares = float(row["size_shares"] or 0)
            side = row["side"]
            peak = float(row["peak_bid_seen"]) if row["peak_bid_seen"] is not None else None

            current_bid, bid_ts = _latest_bid(conn, entry_id)
            if current_bid is None:
                current_bid = entry_price  # fallback to entry

            fcst = _latest_forecast_prob_yes(conn, entry_id)
            distances = trigger_distances(
                side=side, entry_price=entry_price,
                current_bid=current_bid,
                peak_bid=peak if peak is not None else current_bid,
                forecast_prob_yes=fcst,
            )

            # Paper P&L: (current_bid - entry_price) * shares
            paper_pnl = (current_bid - entry_price) * shares

            # Time held
            try:
                entry_ts = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
                held_seconds = int((now - entry_ts).total_seconds())
            except Exception:
                held_seconds = 0

            out.append({
                "entry_id": entry_id,
                "market_slug": row["market_slug"],
                "market_question": row["market_question"],
                "city": row["city_resolved"],
                "side": side,
                "entry_price": entry_price,
                "size_shares": shares,
                "size_usd": float(row["size_usd"] or 0),
                "current_bid": round(current_bid, 4),
                "peak_bid": round(peak, 4) if peak is not None else None,
                "paper_pnl_usd": round(paper_pnl, 2),
                "paper_pnl_pct": round((current_bid - entry_price) / entry_price * 100, 2) if entry_price > 0 else 0,
                "held_seconds": held_seconds,
                "held_human": _humanize_duration(held_seconds),
                "forecast_prob_yes": round(fcst, 3) if fcst is not None else None,
                "bid_ts": bid_ts,
                "end_date": row["end_date"],
                "edge_pp_at_entry": float(row["edge_pp_at_entry"] or 0),
                "triggers": distances,
            })
        # Most recently entered first
        out.sort(key=lambda p: p["entry_id"], reverse=True)
        return out
    finally:
        conn.close()


def _humanize_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes, s = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{s}s"
    hours, m = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{m}m"
    days, h = divmod(hours, 24)
    return f"{days}d{h}h"


# ---------------------------------------------------------------------------
# Inline tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Test trigger_distances scenarios
    # 1) NO @ 0.13, bid 0.15, no peak → all near 0
    d = trigger_distances(side="NO", entry_price=0.13, current_bid=0.15,
                          peak_bid=0.15, forecast_prob_yes=0.05)
    # profit progress: (0.15 - 0.13) / 0.50 = 0.04
    assert abs(d["profit_lock"]["progress"] - 0.04) < 0.005, d
    # trailing not armed (peak 0.15 < entry+20pp = 0.33)
    assert d["trailing_stop"]["armed"] is False
    # convergence: fair NO = 0.95, target 0.90, bid 0.15
    # span = 0.90 - 0.13 = 0.77, progress = (0.15-0.13)/0.77 = 0.026
    assert abs(d["convergence"]["progress"] - 0.026) < 0.005, d
    assert d["would_fire_now"] is False
    print(f"Test 1 PASS: scenario A — early NO bet, all triggers low")

    # 2) NO @ 0.13, bid 0.65, peak 0.65 → profit_lock fires
    d = trigger_distances(side="NO", entry_price=0.13, current_bid=0.65,
                          peak_bid=0.65, forecast_prob_yes=0.05)
    assert d["profit_lock"]["progress"] == 1.0, d
    assert d["would_fire_now"] is True
    assert d["winning_trigger"] == "profit_lock"
    print(f"Test 2 PASS: profit_lock fires at bid 0.65")

    # 3) NO @ 0.13, bid 0.40, peak 0.50 → trailing not yet (drawdown 20% < 30%)
    d = trigger_distances(side="NO", entry_price=0.13, current_bid=0.40,
                          peak_bid=0.50, forecast_prob_yes=0.05)
    # ts armed (peak 0.50 >= 0.33), drawdown 20%, threshold 0.35
    # progress = (peak - current) / (peak - threshold) = 0.10 / 0.15 = 0.667
    assert d["trailing_stop"]["armed"] is True
    assert abs(d["trailing_stop"]["progress"] - 0.667) < 0.01, d
    assert d["would_fire_now"] is False
    print(f"Test 3 PASS: trailing armed but not fired (drawdown 20%)")

    # 4) NO @ 0.13, bid 0.34, peak 0.50 → trailing fires (drawdown 32%)
    d = trigger_distances(side="NO", entry_price=0.13, current_bid=0.34,
                          peak_bid=0.50, forecast_prob_yes=0.05)
    assert d["trailing_stop"]["progress"] == 1.0, d
    assert d["would_fire_now"] is True
    print(f"Test 4 PASS: trailing fires at bid 0.34 from peak 0.50")

    # 5) bid < entry → all progress 0 except potentially forecast_reversal
    d = trigger_distances(side="NO", entry_price=0.13, current_bid=0.10,
                          peak_bid=0.15, forecast_prob_yes=0.05)
    assert d["profit_lock"]["progress"] == 0.0
    assert d["trailing_stop"]["progress"] == 0.0
    assert d["convergence"]["progress"] == 0.0
    assert d["would_fire_now"] is False
    print(f"Test 5 PASS: bid below entry → all zero")

    # Test _humanize_duration
    assert _humanize_duration(30) == "30s"
    assert _humanize_duration(90) == "1m30s"
    assert _humanize_duration(3700) == "1h1m"
    assert _humanize_duration(90000) == "1d1h"
    print("Test 6 PASS: _humanize_duration")

    print("\nAll positions tests PASS")
