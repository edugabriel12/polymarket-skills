"""Open positions service — joins entries + latest monitor_check bid +
computes trigger-distance progress for the cashout policy.

Reuses query_open_positions and evaluate_cashout_triggers from the analyzer
modules. Read-only.
"""

from __future__ import annotations

import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from .. import settings as S  # noqa: F401 — primes sys.path

import weather_edge_db as wdb  # noqa: E402
from weather_edge_helpers import evaluate_cashout_triggers  # noqa: E402


# v9.7: derive parent-event slug for the Polymarket link. Bracket sub-markets
# come with a temperature/threshold suffix appended to the parent slug:
#   parent: "highest-temperature-in-kuala-lumpur-on-may-23-2026"
#   bracket: "highest-temperature-in-kuala-lumpur-on-may-23-2026-33c"
#   bracket: "highest-temperature-in-nyc-on-may-25-2026-90f"
#   bracket: "highest-temperature-in-beijing-on-may-23-2026-26corbelow"
#   bracket: "highest-temperature-in-tokyo-on-may-25-2026-90forhigher"
# Match a trailing "-<digits>[cf]" with an optional Polymarket modifier
# ("orbelow", "orhigher", "orabove", "orlower", "ormore", "orless", or
# any "or<word>" suffix). Tight enough not to eat the year (e.g.
# "...-2026-33c" must keep "-2026", strip "-33c"; "...-2026" alone stays).
_THRESHOLD_SUFFIX_RE = re.compile(
    r"-\d+[cf](?:or\w+)?$", re.IGNORECASE
)


def parent_event_slug(market_slug: str,
                       ladder_event_slug: Optional[str] = None) -> str:
    """Return the parent Polymarket event slug for a position's market.

    Preference order:
      1. `ladder_event_slug` if non-empty — authoritative (from Gamma /events)
      2. Strip trailing threshold suffix from `market_slug`
      3. Fall back to `market_slug` as-is
    """
    if ladder_event_slug:
        return ladder_event_slug
    if not market_slug:
        return ""
    stripped = _THRESHOLD_SUFFIX_RE.sub("", market_slug)
    return stripped or market_slug


# v9.7: lightweight CLOB orderbook fetch for live price refresh. Mirrors
# the bot's fetch_orderbook but kept local so the dashboard doesn't depend
# on the bot module loading at import time.
_CLOB_BASE = "https://clob.polymarket.com"


def _fetch_best_bid(token_id: str, timeout: float = 4.0) -> Optional[float]:
    """Single-shot HTTP fetch of the best bid for a token. Returns None on
    any failure so callers fall back to cached monitor_check bid.
    """
    if not token_id:
        return None
    try:
        r = requests.get(f"{_CLOB_BASE}/book",
                          params={"token_id": str(token_id)},
                          timeout=timeout)
        if r.status_code != 200:
            return None
        bids = r.json().get("bids") or []
        if not bids:
            return None
        # CLOB returns bids highest-first; the first entry is best.
        # Defensive: max() in case ordering changes.
        prices = []
        for b in bids:
            try:
                prices.append(float(b["price"]))
            except (KeyError, TypeError, ValueError):
                continue
        return max(prices) if prices else None
    except (requests.RequestException, ValueError, KeyError):
        return None


def _refresh_bids_parallel(positions: list[dict],
                            max_workers: int = 12) -> dict[int, Optional[float]]:
    """Fetch live best-bid for each position in parallel. Returns a dict
    entry_id → best_bid (or None on failure).
    """
    if not positions:
        return {}

    def _one(p):
        side = p.get("side")
        token = p.get("token_id_yes") if side == "YES" else p.get("token_id_no")
        return p["entry_id"], _fetch_best_bid(token)

    out: dict[int, Optional[float]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_one, p): p["entry_id"] for p in positions}
        for fut in as_completed(futures, timeout=15.0):
            try:
                eid, bid = fut.result(timeout=5.0)
                out[eid] = bid
            except Exception:
                out[futures[fut]] = None
    return out


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


def get_open_positions(sort_by: str = "entry_id",
                        refresh_prices: bool = False) -> list[dict]:
    """Return a list of open positions with bid, peak, P&L, trigger
    distances, and time held.

    sort_by: 'entry_id' (default, most recent first) or 'size' (largest
    stake first — used by Overview's 'top 5 by size' slot).

    refresh_prices: when True (positions page on tab open / auto-refresh),
    hit CLOB live for each position's token and use that bid instead of
    the cached monitor_check value. Lets the operator see real-time P&L
    independent of the monitor cycle interval. Adds ~1-5s page load
    depending on position count (parallelized, 12 concurrent fetches).
    """
    try:
        conn = _ro_conn(S.WEATHER_EDGE_DB)
    except FileNotFoundError:
        return []
    try:
        rows = list(wdb.query_open_positions(conn))
        # v9.7: optional live CLOB refresh — done up-front, in parallel,
        # so each position uses the freshest possible bid instead of the
        # cached monitor_check.
        live_bids: dict[int, Optional[float]] = {}
        if refresh_prices and rows:
            mini = [{"entry_id": r["entry_id"], "side": r["side"],
                      "token_id_yes": r["token_id_yes"],
                      "token_id_no": r["token_id_no"]}
                     for r in rows]
            live_bids = _refresh_bids_parallel(mini)
        out = []
        now = datetime.now(timezone.utc)
        for row in rows:
            entry_id = row["entry_id"]
            entry_price = float(row["entry_price"] or 0)
            shares = float(row["size_shares"] or 0)
            side = row["side"]
            peak = float(row["peak_bid_seen"]) if row["peak_bid_seen"] is not None else None

            current_bid, bid_ts = _latest_bid(conn, entry_id)
            # v9.7: prefer live CLOB bid when refresh requested + fetch
            # succeeded. Fall back through monitor_check → entry_price.
            live_bid = live_bids.get(entry_id)
            if live_bid is not None:
                current_bid = live_bid
                bid_ts = "live"
            elif current_bid is None:
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

            # v9.7: derive the parent-event slug for the Polymarket link.
            # ladder_event_slug is authoritative when set, else strip the
            # threshold suffix from market_slug.
            _les = (row["ladder_event_slug"]
                     if "ladder_event_slug" in row.keys() else None)
            parent_slug = parent_event_slug(row["market_slug"], _les)

            out.append({
                "entry_id": entry_id,
                "market_slug": row["market_slug"],
                "parent_event_slug": parent_slug,
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
                # v9: ladder grouping (NULL on legacy single-bin entries)
                "ladder_group_id": (row["ladder_group_id"]
                                    if "ladder_group_id" in row.keys()
                                    else None),
                "ladder_position": (row["ladder_position"]
                                    if "ladder_position" in row.keys()
                                    else None),
                "ladder_event_slug": (row["ladder_event_slug"]
                                       if "ladder_event_slug" in row.keys()
                                       else None),
            })
        if sort_by == "size":
            out.sort(key=lambda p: p["size_usd"], reverse=True)
        else:
            out.sort(key=lambda p: p["entry_id"], reverse=True)
        return out
    finally:
        conn.close()


def get_recent_skipped(limit: int = 20) -> list[dict]:
    """v6: Recently SKIPPED entries with reason, for the dashboard panel.

    Highlight actionable reasons (edge_stale, judge_*) in the UI via
    `actionable` flag.
    """
    if not S.WEATHER_EDGE_DB.exists():
        return []
    conn = _ro_conn(S.WEATHER_EDGE_DB)
    try:
        rows = conn.execute(
            "SELECT entry_id, ts, market_slug, market_question, side, "
            "       entry_price, edge_pp_at_entry, "
            "       COALESCE(skip_reason, judge_skipped_reason, 'unknown') "
            "       AS reason, "
            "       city_resolved "
            "FROM entries "
            "WHERE status = 'SKIPPED' "
            "ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()

    actionable = {"edge_stale", "judge_unavailable", "judge_budget_exceeded"}
    out = []
    for r in rows:
        reason = r["reason"]
        out.append({
            "entry_id": r["entry_id"],
            "ts": (r["ts"] or "")[:16],
            "market_slug": r["market_slug"],
            "market_question": r["market_question"],
            "side": r["side"],
            "entry_price": r["entry_price"],
            "edge_pp": r["edge_pp_at_entry"],
            "reason": reason,
            "city": r["city_resolved"],
            "actionable": reason in actionable,
        })
    return out


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
