"""Cost-tracking service: aggregates AI agent spend (judge + advisor)
across various time windows for the Costs dashboard tab.

Reads from weather_edge.db:
  - judge_reviews.cost_usd, tokens_in, tokens_out, cache_read_tokens, llm_model
  - advisor_runs.cost_usd, tokens_in, tokens_out, cache_read_tokens, llm_model
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import settings as S


def _ro_conn(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _since_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _today_iso_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def get_cost_summary(days: int = 30) -> dict:
    """Aggregate cost stats for both agents over the window + lifetime."""
    if not S.WEATHER_EDGE_DB.exists():
        return _empty_summary(days)
    conn = _ro_conn(S.WEATHER_EDGE_DB)
    try:
        since = _since_iso(days)
        today = _today_iso_date()

        out: dict = {"days": days, "since_iso": since}

        # JUDGE
        j_window = conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0), COUNT(*), "
            "       COALESCE(SUM(tokens_in),0), COALESCE(SUM(tokens_out),0), "
            "       COALESCE(SUM(cache_read_tokens),0) "
            "FROM judge_reviews WHERE ts >= ?", (since,),
        ).fetchone()
        j_today = conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0), COUNT(*) FROM judge_reviews "
            "WHERE DATE(ts) = ?", (today,),
        ).fetchone()
        j_all = conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0), COUNT(*) FROM judge_reviews",
        ).fetchone()
        j_per_model = {}
        for r in conn.execute(
            "SELECT llm_model, COALESCE(SUM(cost_usd),0), COUNT(*) "
            "FROM judge_reviews WHERE ts >= ? GROUP BY llm_model", (since,),
        ):
            if r[0]:
                j_per_model[r[0]] = {"cost_usd": float(r[1]), "n": int(r[2])}

        out["judge"] = {
            "today_usd": round(float(j_today[0]), 4),
            "today_n": int(j_today[1]),
            "window_usd": round(float(j_window[0]), 4),
            "window_n": int(j_window[1]),
            "lifetime_usd": round(float(j_all[0]), 4),
            "lifetime_n": int(j_all[1]),
            "tokens_in": int(j_window[2]),
            "tokens_out": int(j_window[3]),
            "cache_read_tokens": int(j_window[4]),
            "avg_cost_per_review": (
                round(float(j_window[0]) / int(j_window[1]), 4)
                if int(j_window[1]) > 0 else 0
            ),
            "per_model": j_per_model,
        }

        # ADVISOR
        a_window = conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0), COUNT(*), "
            "       COALESCE(SUM(tokens_in),0), COALESCE(SUM(tokens_out),0), "
            "       COALESCE(SUM(cache_read_tokens),0) "
            "FROM advisor_runs WHERE ts >= ? AND status='ok'", (since,),
        ).fetchone()
        a_today = conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0), COUNT(*) FROM advisor_runs "
            "WHERE DATE(ts) = ? AND status='ok'", (today,),
        ).fetchone()
        a_all = conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0), COUNT(*) FROM advisor_runs "
            "WHERE status='ok'",
        ).fetchone()
        a_per_model = {}
        for r in conn.execute(
            "SELECT llm_model, COALESCE(SUM(cost_usd),0), COUNT(*) "
            "FROM advisor_runs WHERE ts >= ? AND status='ok' "
            "GROUP BY llm_model", (since,),
        ):
            if r[0]:
                a_per_model[r[0]] = {"cost_usd": float(r[1]), "n": int(r[2])}

        out["advisor"] = {
            "today_usd": round(float(a_today[0]), 4),
            "today_n": int(a_today[1]),
            "window_usd": round(float(a_window[0]), 4),
            "window_n": int(a_window[1]),
            "lifetime_usd": round(float(a_all[0]), 4),
            "lifetime_n": int(a_all[1]),
            "tokens_in": int(a_window[2]),
            "tokens_out": int(a_window[3]),
            "cache_read_tokens": int(a_window[4]),
            "avg_cost_per_run": (
                round(float(a_window[0]) / int(a_window[1]), 4)
                if int(a_window[1]) > 0 else 0
            ),
            "per_model": a_per_model,
        }

        # COMBINED
        out["total"] = {
            "today_usd": round(out["judge"]["today_usd"]
                               + out["advisor"]["today_usd"], 4),
            "window_usd": round(out["judge"]["window_usd"]
                                + out["advisor"]["window_usd"], 4),
            "lifetime_usd": round(out["judge"]["lifetime_usd"]
                                  + out["advisor"]["lifetime_usd"], 4),
        }
        return out
    finally:
        conn.close()


def _empty_summary(days: int) -> dict:
    z = {"today_usd": 0.0, "today_n": 0, "window_usd": 0.0, "window_n": 0,
         "lifetime_usd": 0.0, "lifetime_n": 0, "tokens_in": 0,
         "tokens_out": 0, "cache_read_tokens": 0,
         "avg_cost_per_review": 0, "avg_cost_per_run": 0, "per_model": {}}
    return {"days": days, "since_iso": _since_iso(days),
            "judge": z, "advisor": z,
            "total": {"today_usd": 0.0, "window_usd": 0.0, "lifetime_usd": 0.0}}


def get_daily_cost_series(days: int = 30) -> list[dict]:
    """Per-day combined cost series for chart. Returns list of dicts:
    [{date, judge_usd, advisor_usd, total_usd}, ...] oldest first.
    """
    if not S.WEATHER_EDGE_DB.exists():
        return []
    since = _since_iso(days)
    conn = _ro_conn(S.WEATHER_EDGE_DB)
    try:
        # Judge daily
        judge_by_day: dict[str, float] = {}
        for r in conn.execute(
            "SELECT DATE(ts) AS d, SUM(cost_usd) AS c "
            "FROM judge_reviews WHERE ts >= ? GROUP BY DATE(ts)",
            (since,),
        ):
            judge_by_day[r["d"]] = float(r["c"] or 0)
        advisor_by_day: dict[str, float] = {}
        for r in conn.execute(
            "SELECT DATE(ts) AS d, SUM(cost_usd) AS c "
            "FROM advisor_runs WHERE ts >= ? AND status='ok' GROUP BY DATE(ts)",
            (since,),
        ):
            advisor_by_day[r["d"]] = float(r["c"] or 0)
        all_dates = sorted(set(judge_by_day) | set(advisor_by_day))
        return [
            {"date": d,
             "judge_usd": round(judge_by_day.get(d, 0.0), 4),
             "advisor_usd": round(advisor_by_day.get(d, 0.0), 4),
             "total_usd": round(judge_by_day.get(d, 0.0)
                                + advisor_by_day.get(d, 0.0), 4)}
            for d in all_dates
        ]
    finally:
        conn.close()


def get_top_expensive_reviews(limit: int = 10, days: int = 30) -> list[dict]:
    """Top-N most expensive judge reviews in the window."""
    if not S.WEATHER_EDGE_DB.exists():
        return []
    since = _since_iso(days)
    conn = _ro_conn(S.WEATHER_EDGE_DB)
    try:
        rows = conn.execute(
            "SELECT j.review_id, j.entry_id, j.ts, j.verdict, j.cost_usd, "
            "       j.tokens_in, j.tokens_out, j.duration_ms, "
            "       e.city_resolved, e.side, e.market_slug "
            "FROM judge_reviews j "
            "LEFT JOIN entries e ON e.entry_id = j.entry_id "
            "WHERE j.ts >= ? AND j.cost_usd IS NOT NULL "
            "ORDER BY j.cost_usd DESC LIMIT ?",
            (since, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_advisor_run_history(days: int = 90) -> list[dict]:
    """All advisor runs in the window, newest first."""
    if not S.WEATHER_EDGE_DB.exists():
        return []
    since = _since_iso(days)
    conn = _ro_conn(S.WEATHER_EDGE_DB)
    try:
        rows = conn.execute(
            "SELECT * FROM advisor_runs WHERE ts >= ? ORDER BY ts DESC",
            (since,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Inline tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    db_path = tmp / "test.db"
    c = sqlite3.connect(db_path)
    c.executescript("""
        CREATE TABLE entries (entry_id INTEGER PRIMARY KEY, market_slug TEXT,
            side TEXT, city_resolved TEXT);
        CREATE TABLE judge_reviews (
            review_id INTEGER PRIMARY KEY, entry_id INTEGER, ts TEXT,
            verdict TEXT, cost_usd REAL, tokens_in INTEGER,
            tokens_out INTEGER, cache_read_tokens INTEGER,
            duration_ms INTEGER, llm_model TEXT);
        CREATE TABLE advisor_runs (
            run_id INTEGER PRIMARY KEY, ts TEXT, status TEXT,
            cost_usd REAL, tokens_in INTEGER, tokens_out INTEGER,
            cache_read_tokens INTEGER, llm_model TEXT,
            trigger TEXT, since_iso TEXT, report_path TEXT,
            json_path TEXT, n_suggestions INTEGER);
    """)
    now = datetime.now(timezone.utc)
    yesterday = (now - timedelta(days=1)).isoformat()
    today_iso = now.isoformat()
    c.execute("INSERT INTO entries VALUES (1, 'tokyo-25c', 'NO', 'Tokyo')")
    c.execute("INSERT INTO entries VALUES (2, 'paris-17c', 'YES', 'Paris')")
    c.execute("INSERT INTO judge_reviews VALUES "
              "(1, 1, ?, 'APPROVE', 0.0322, 5000, 1200, 4000, 28500, 'claude-sonnet-4-6')",
              (today_iso,))
    c.execute("INSERT INTO judge_reviews VALUES "
              "(2, 1, ?, 'REJECT', 0.0481, 6000, 1500, 4000, 35000, 'claude-sonnet-4-6')",
              (today_iso,))
    c.execute("INSERT INTO judge_reviews VALUES "
              "(3, 2, ?, 'APPROVE', 0.0153, 4500, 900, 4000, 20000, 'claude-sonnet-4-6')",
              (yesterday,))
    c.execute("INSERT INTO advisor_runs VALUES "
              "(1, ?, 'ok', 1.2500, 18000, 3200, 3000, 'claude-opus-4-7', "
              "'cli', '2026-04-15', '/r.md', '/r.json', 3)",
              (today_iso,))
    c.commit()
    c.close()
    S.WEATHER_EDGE_DB = db_path

    s = get_cost_summary(days=30)
    assert s["judge"]["today_n"] == 2, s["judge"]
    assert abs(s["judge"]["today_usd"] - 0.0803) < 0.001, s["judge"]
    assert s["judge"]["lifetime_n"] == 3
    assert abs(s["judge"]["lifetime_usd"] - 0.0956) < 0.001
    assert s["advisor"]["today_n"] == 1
    assert s["advisor"]["today_usd"] == 1.25
    assert abs(s["total"]["today_usd"] - 1.3303) < 0.001
    print(f"Test 1 PASS: get_cost_summary today=${s['total']['today_usd']}")

    daily = get_daily_cost_series(days=30)
    assert len(daily) >= 1
    print(f"Test 2 PASS: daily series len={len(daily)}, first={daily[0]}")

    top = get_top_expensive_reviews(limit=10, days=30)
    assert len(top) == 3
    assert top[0]["cost_usd"] == 0.0481  # most expensive
    assert top[0]["city_resolved"] == "Tokyo"
    print(f"Test 3 PASS: top expensive review = ${top[0]['cost_usd']} ({top[0]['city_resolved']})")

    runs = get_advisor_run_history(days=90)
    assert len(runs) == 1 and runs[0]["cost_usd"] == 1.25
    print(f"Test 4 PASS: advisor history len={len(runs)}")

    print("\nAll cost service tests PASS")
