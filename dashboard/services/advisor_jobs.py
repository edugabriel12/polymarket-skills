"""Advisor job manager: spawns weather_strategy_advisor.py as a detached
subprocess, tracks status via the `advisor_jobs` table, and polls for
completion via the UI.

Job lifecycle:
  pending → running → done|failed

The advisor subprocess is invoked with `--job-id N` so the script can
update the row from inside itself (status='running' at start, then
final status with `resulting_run_id` set when done).

The dashboard polls /api/advisor/jobs/{id} which calls get_job(); when
status is terminal it stops the HTMX poller by rendering a different
template.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ADVISOR_SCRIPT = (
    REPO_ROOT / "polymarket-analyzer" / "scripts"
    / "weather_strategy_advisor.py"
)
DATA_DIR = Path.home() / ".polymarket-paper"

from .. import settings as S  # noqa: E402


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(S.WEATHER_EDGE_DB), timeout=5.0)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout = 5000")
    return c


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def start_job(since_days: int = 30, per_trade_limit: int = 200) -> dict:
    """Insert pending row, spawn the advisor subprocess detached, then
    flip status to 'running' with PID. Returns the job row as a dict."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO advisor_jobs "
            "(ts_started, trigger, since_days, per_trade_limit, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (_now_iso(), "ui", since_days, per_trade_limit, "pending"),
        )
        job_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    log_path = DATA_DIR / f"advisor_job_{job_id}.log"

    argv = [
        sys.executable, str(ADVISOR_SCRIPT),
        "--once",
        "--since-days", str(since_days),
        "--per-trade-limit", str(per_trade_limit),
        "--trigger", "on_demand",
        "--job-id", str(job_id),
        "--force",
    ]

    try:
        log_fd = open(log_path, "ab")
        kwargs = {
            "stdout": log_fd, "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL, "close_fds": True,
            "env": {**os.environ},  # inherit ANTHROPIC_API_KEY etc
            "cwd": str(REPO_ROOT),
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = (
                subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(argv, **kwargs)
    except Exception as e:
        conn = _conn()
        try:
            conn.execute(
                "UPDATE advisor_jobs SET status='failed', error_msg=?, "
                "ts_finished=? WHERE job_id=?",
                (f"spawn failed: {e}", _now_iso(), job_id),
            )
            conn.commit()
        finally:
            conn.close()
        return {"job_id": job_id, "status": "failed",
                "error_msg": f"spawn failed: {e}"}

    conn = _conn()
    try:
        conn.execute(
            "UPDATE advisor_jobs SET status='running', pid=?, log_path=? "
            "WHERE job_id=?",
            (proc.pid, str(log_path), job_id),
        )
        conn.commit()
    finally:
        conn.close()
    return get_job(job_id)


def get_job(job_id: int) -> Optional[dict]:
    if not S.WEATHER_EDGE_DB.exists():
        return None
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM advisor_jobs WHERE job_id = ?", (job_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_jobs(limit: int = 20) -> list[dict]:
    if not S.WEATHER_EDGE_DB.exists():
        return []
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM advisor_jobs ORDER BY ts_started DESC LIMIT ?",
            (limit,),
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
        CREATE TABLE advisor_jobs (
            job_id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_started TEXT NOT NULL, ts_finished TEXT,
            trigger TEXT NOT NULL, since_days INTEGER NOT NULL,
            per_trade_limit INTEGER, status TEXT NOT NULL,
            pid INTEGER, exit_code INTEGER,
            resulting_run_id INTEGER, log_path TEXT,
            error_msg TEXT);
    """)
    c.commit()
    c.close()

    S.WEATHER_EDGE_DB = db_path

    # Test 1: get_job for nonexistent ID
    assert get_job(999) is None
    print("Test 1 PASS: get_job(missing) → None")

    # Test 2: list_jobs on empty table
    assert list_jobs() == []
    print("Test 2 PASS: list_jobs empty")

    # Test 3: insert a row manually (skip start_job since it spawns real proc)
    c = sqlite3.connect(db_path)
    c.execute(
        "INSERT INTO advisor_jobs (ts_started, trigger, since_days, "
        "per_trade_limit, status, pid) VALUES (?, ?, ?, ?, ?, ?)",
        ("2026-05-14T10:00:00Z", "ui", 30, 200, "running", 12345)
    )
    c.commit(); c.close()
    job = get_job(1)
    assert job is not None and job["status"] == "running"
    assert job["pid"] == 12345
    print(f"Test 3 PASS: get_job → status={job['status']} pid={job['pid']}")

    # Test 4: list_jobs returns inserted job
    jobs = list_jobs()
    assert len(jobs) == 1 and jobs[0]["job_id"] == 1
    print(f"Test 4 PASS: list_jobs → {len(jobs)} job")

    print("\nAll advisor_jobs tests PASS")
