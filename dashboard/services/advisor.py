"""Advisor service: read advisor_runs + JSON sidecars produced by
weather_strategy_advisor and surface them in the Advisor tab.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

from .. import settings as S


def _ro_conn(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def list_runs(limit: int = 50) -> list[dict]:
    """Return advisor_runs ordered by ts DESC."""
    if not S.WEATHER_EDGE_DB.exists():
        return []
    conn = _ro_conn(S.WEATHER_EDGE_DB)
    try:
        rows = conn.execute(
            "SELECT * FROM advisor_runs ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_run(run_id: int) -> Optional[dict]:
    """Return one advisor_run row + parsed JSON sidecar payload."""
    if not S.WEATHER_EDGE_DB.exists():
        return None
    conn = _ro_conn(S.WEATHER_EDGE_DB)
    try:
        row = conn.execute(
            "SELECT * FROM advisor_runs WHERE run_id = ?", (run_id,),
        ).fetchone()
        if not row:
            return None
        run = dict(row)
    finally:
        conn.close()

    # Load JSON sidecar from disk
    payload: dict = {}
    json_path = run.get("json_path")
    if json_path and Path(json_path).exists():
        try:
            payload = json.loads(Path(json_path).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            payload = {"_load_error": str(e)}
    run["payload"] = payload
    return run


def list_applies_for_run(run_id: int) -> dict:
    """Return {suggestion_id: apply_row} for a given run, so the UI can
    mark which suggestions are already applied."""
    if not S.WEATHER_EDGE_DB.exists():
        return {}
    conn = _ro_conn(S.WEATHER_EDGE_DB)
    try:
        rows = conn.execute(
            "SELECT * FROM advisor_suggestion_applies WHERE run_id = ?",
            (run_id,),
        ).fetchall()
        return {r["suggestion_id"]: dict(r) for r in rows}
    finally:
        conn.close()


def get_acceptance_stats() -> dict:
    """v6: Acceptance rate = applied / total_suggestions across all runs.
    Returns overall rate + per-category breakdown.

    'total_suggestions' comes from summing n_suggestions per advisor_runs row.
    'applied' counts rows in advisor_suggestion_applies with status='applied'.
    """
    if not S.WEATHER_EDGE_DB.exists():
        return {"available": False}
    conn = _ro_conn(S.WEATHER_EDGE_DB)
    try:
        total = conn.execute(
            "SELECT COALESCE(SUM(n_suggestions), 0) FROM advisor_runs"
        ).fetchone()[0] or 0
        applied = conn.execute(
            "SELECT COUNT(*) FROM advisor_suggestion_applies "
            "WHERE status = 'applied'"
        ).fetchone()[0] or 0
        # By category
        by_cat_rows = conn.execute(
            "SELECT category, status, COUNT(*) as n "
            "FROM advisor_suggestion_applies "
            "GROUP BY category, status"
        ).fetchall()
    finally:
        conn.close()

    by_cat: dict[str, dict] = {}
    for r in by_cat_rows:
        cat = r["category"] or "unknown"
        by_cat.setdefault(cat, {"applied": 0, "failed": 0, "unsupported": 0})
        if r["status"] in by_cat[cat]:
            by_cat[cat][r["status"]] = r["n"]

    return {
        "available": True,
        "total_suggestions": total,
        "applied": applied,
        "acceptance_rate": (applied / total) if total > 0 else 0.0,
        "by_category": by_cat,
    }


def get_summary_kpis() -> dict:
    """Top-level numbers for the Advisor tab header."""
    if not S.WEATHER_EDGE_DB.exists():
        return {"n_runs": 0, "total_cost_usd": 0.0, "last_run_ts": None,
                "n_applies": 0}
    conn = _ro_conn(S.WEATHER_EDGE_DB)
    try:
        r = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(cost_usd), 0), MAX(ts) "
            "FROM advisor_runs",
        ).fetchone()
        n_applies = conn.execute(
            "SELECT COUNT(*) FROM advisor_suggestion_applies "
            "WHERE status = 'applied'",
        ).fetchone()[0]
        return {
            "n_runs": int(r[0]),
            "total_cost_usd": round(float(r[1] or 0), 4),
            "last_run_ts": r[2],
            "n_applies": int(n_applies),
        }
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
        CREATE TABLE advisor_runs (
            run_id INTEGER PRIMARY KEY, ts TEXT, trigger TEXT,
            since_iso TEXT, report_path TEXT, json_path TEXT,
            n_suggestions INTEGER, llm_model TEXT,
            cost_usd REAL, tokens_in INTEGER, tokens_out INTEGER,
            cache_read_tokens INTEGER, status TEXT, error_msg TEXT);
        CREATE TABLE advisor_suggestion_applies (
            apply_id INTEGER PRIMARY KEY, run_id INTEGER,
            suggestion_id TEXT, ts TEXT, category TEXT,
            param_path TEXT, previous_value TEXT, applied_value TEXT,
            git_commit_sha TEXT, status TEXT, error_msg TEXT);
    """)
    json_file = tmp / "report.json"
    json_file.write_text(json.dumps({
        "summary": "test summary",
        "suggestions": [
            {"id": "sug_001", "category": "threshold", "title": "T1"},
            {"id": "sug_002", "category": "city", "title": "C1"},
        ],
    }))
    c.execute(
        "INSERT INTO advisor_runs VALUES "
        "(1, '2026-05-14T10:00:00Z', 'cli', '2026-04-14T00:00:00Z', "
        " '/r.md', ?, 2, 'opus-4-7', 1.35, 8000, 3000, 4000, 'ok', NULL)",
        (str(json_file),),
    )
    c.execute(
        "INSERT INTO advisor_suggestion_applies VALUES "
        "(1, 1, 'sug_001', '2026-05-14T11:00:00Z', 'threshold', "
        " 'weather_edge_bot.py:--profit-lock-pp default', '50.0', '40', "
        " 'abc123', 'applied', NULL)"
    )
    c.commit()
    c.close()
    S.WEATHER_EDGE_DB = db_path

    runs = list_runs(limit=10)
    assert len(runs) == 1 and runs[0]["run_id"] == 1
    print(f"Test 1 PASS: list_runs → {len(runs)} run")

    run = get_run(1)
    assert run is not None and "payload" in run
    assert run["payload"]["summary"] == "test summary"
    assert len(run["payload"]["suggestions"]) == 2
    print(f"Test 2 PASS: get_run + payload loaded")

    applies = list_applies_for_run(1)
    assert "sug_001" in applies and applies["sug_001"]["status"] == "applied"
    assert "sug_002" not in applies
    print(f"Test 3 PASS: list_applies_for_run → {len(applies)} applied")

    kpis = get_summary_kpis()
    assert kpis["n_runs"] == 1
    assert kpis["total_cost_usd"] == 1.35
    assert kpis["n_applies"] == 1
    print(f"Test 4 PASS: get_summary_kpis → {kpis}")

    print("\nAll advisor service tests PASS")
