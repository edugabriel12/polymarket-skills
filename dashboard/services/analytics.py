"""Analytics service — thin wrapper over weather_edge_analyzer aggregators
+ city performance from strategy_advisor_helpers. Adds windowed counterfactual
delta over time for the line chart.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .. import settings as S  # noqa: F401

from weather_edge_analyzer import (  # noqa: E402
    aggregate_by_bucket,
    aggregate_judge,
    aggregate_cashout_triggers,
    compute_counterfactuals,
    replay_entry,
)
from strategy_advisor_helpers import _city_performance  # noqa: E402


def _ro_conn(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def since_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def get_all_analytics(days: int = 14, strategy: Optional[str] = None) -> dict:
    """Single call that returns all 4 aggregates for the Performance tab.

    v11: `strategy` filters every aggregate to a single strategy
    ('weather_edge' / 'cheap_convexity'); None = all. Threads through the
    analyzer aggregators (all now accept an optional strategy)."""
    since = since_iso(days)
    if not S.WEATHER_EDGE_DB.exists():
        return {"buckets": {}, "judge": {}, "triggers": {},
                "cities": {}, "cities_sorted": [],
                "counterfactual_series": [],
                "since_iso": since, "days": days, "strategy": strategy}
    conn = _ro_conn(S.WEATHER_EDGE_DB)
    try:
        # Note: compute_counterfactuals would WRITE — skip in read-only.
        # The analyzer expects it but for a read-only dashboard we just
        # use whatever's already computed.
        buckets = aggregate_by_bucket(conn, since, strategy=strategy)
        judge = aggregate_judge(conn, since, strategy=strategy)
        triggers = aggregate_cashout_triggers(conn, since, strategy=strategy)
        cities = _city_performance(conn, since, strategy=strategy)
        cf_series = _counterfactual_series(conn, since, strategy=strategy)
        # v13.6: Win Rate by City renders as a table — pre-sort by sample
        # size desc so the template stays dumb.
        cities_sorted = sorted(cities.items(), key=lambda kv: -kv[1]["n"])
        return {
            "buckets": buckets, "judge": judge, "triggers": triggers,
            "cities": cities, "cities_sorted": cities_sorted,
            "counterfactual_series": cf_series,
            "since_iso": since, "days": days, "strategy": strategy,
        }
    finally:
        conn.close()


def _counterfactual_series(conn: sqlite3.Connection, since_iso: str,
                           strategy: Optional[str] = None) -> list[dict]:
    """Cumulative counterfactual delta over time (one point per cashout date).

    v11: optional strategy filter — cashouts has no strategy column so it
    EXISTS-joins entries (NULL counts as 'weather_edge')."""
    q = ("SELECT c.ts, cf.delta FROM cashouts c "
         "JOIN counterfactuals cf ON cf.entry_id = c.entry_id "
         "WHERE c.ts >= ?")
    params: tuple = (since_iso,)
    if strategy is not None:
        q += (" AND EXISTS (SELECT 1 FROM entries e WHERE e.entry_id = "
              "c.entry_id AND COALESCE(e.strategy,'weather_edge') = ?)")
        params = (since_iso, strategy)
    q += " ORDER BY c.ts ASC"
    rows = conn.execute(q, params).fetchall()
    cum = 0.0
    out = []
    for r in rows:
        ts = r[0]
        delta = float(r[1] or 0)
        cum += delta
        out.append({"ts": ts, "delta": round(delta, 2),
                    "cumulative": round(cum, 2)})
    return out


def get_cumulative_pnl_series(days: int = 30) -> list[dict]:
    """Cumulative realized P&L from cashouts, point per day."""
    since = since_iso(days)
    if not S.WEATHER_EDGE_DB.exists():
        return []
    conn = _ro_conn(S.WEATHER_EDGE_DB)
    try:
        rows = conn.execute(
            "SELECT DATE(ts) AS date, SUM(realized_pnl_usd) AS daily_pnl "
            "FROM cashouts WHERE ts >= ? GROUP BY DATE(ts) ORDER BY date ASC",
            (since,),
        ).fetchall()
        cum = 0.0
        out = []
        for r in rows:
            cum += float(r[1] or 0)
            out.append({"date": r[0],
                        "daily_pnl": round(float(r[1] or 0), 2),
                        "cumulative_pnl": round(cum, 2)})
        return out
    finally:
        conn.close()


def replay_entry_md(entry_id: int) -> str:
    """Return markdown replay for a given entry_id."""
    if not S.WEATHER_EDGE_DB.exists():
        return "_(no DB)_"
    conn = _ro_conn(S.WEATHER_EDGE_DB)
    try:
        return replay_entry(conn, entry_id)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Inline tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    s = since_iso(30)
    # Just verify the function shape — full DB depends on env state
    assert isinstance(s, str)
    assert "T" in s
    print(f"Test 1 PASS: since_iso(30) = {s}")

    out = get_all_analytics(days=30)
    assert "buckets" in out and "judge" in out and "triggers" in out
    assert "cities" in out and "counterfactual_series" in out
    print(f"Test 2 PASS: get_all_analytics keys present: {list(out.keys())}")

    series = get_cumulative_pnl_series(days=30)
    assert isinstance(series, list)
    print(f"Test 3 PASS: cumulative_pnl_series len={len(series)}")

    # Test 4 (v11): strategy segregation threads through get_all_analytics.
    import tempfile
    import sys as _sys
    _analyzer = Path(__file__).resolve().parent.parent.parent \
        / "polymarket-analyzer" / "scripts"
    if str(_analyzer) not in _sys.path:
        _sys.path.insert(0, str(_analyzer))
    import weather_edge_db as _wdb
    _tmp = Path(tempfile.mkdtemp()) / "seg_analytics.db"
    _wdb.init_db(_tmp)
    with _wdb.connect(_tmp) as _c:
        for _strat, _city in (("weather_edge", "London"),
                              ("cheap_convexity", "Paris")):
            _c.execute(
                "INSERT INTO entries (ts, market_slug, market_question, side, "
                "status, entry_price, size_shares, edge_pp_at_entry, "
                "forecast_prob_at_entry, city_resolved, strategy) VALUES "
                "('2026-07-06T00:00:00+00:00','s','q','YES','EXECUTED',0.5,10,"
                "15,0.6,?,?)", (_city, _strat))
        _c.commit()
    S.WEATHER_EDGE_DB = _tmp
    _we = get_all_analytics(days=3650, strategy="weather_edge")
    _cc = get_all_analytics(days=3650, strategy="cheap_convexity")
    _all = get_all_analytics(days=3650, strategy=None)
    assert set(_we["cities"].keys()) == {"London"}, _we["cities"]
    assert set(_cc["cities"].keys()) == {"Paris"}, _cc["cities"]
    assert set(_all["cities"].keys()) == {"London", "Paris"}, _all["cities"]
    print("Test 4 PASS: get_all_analytics segregates by strategy "
          "(weather_edge→London, cheap_convexity→Paris, all→both)")

    print("\nAll analytics tests PASS")
