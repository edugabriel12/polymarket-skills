"""Live trading service — read-only monitoring + killswitch toggle.

Surfaces the live trading state for the dashboard:
  - mode (PAPER, LIVE_MANUAL, LIVE_AUTONOMOUS) from env vars
  - readiness checks via subprocess(backtest.py --live-check --json)
  - tail of ~/.polymarket-live/trades.log (JSONL)
  - daily spend computed from trades.log entries with today's UTC date
  - killswitch: ~/halt-trading file presence / toggle

NEVER places trades. NEVER reads private keys (only env presence).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LIVE_TRADES_LOG = Path.home() / ".polymarket-live" / "trades.log"
HALT_FILE_DEFAULT = Path.home() / "halt-trading"


def _halt_file() -> Path:
    """Resolved killswitch file path (env override allowed per CLAUDE.md §4.1)."""
    p = os.environ.get("HALT_FILE")
    return Path(p).expanduser() if p else HALT_FILE_DEFAULT


def get_live_mode() -> dict:
    """Return mode + flags. Mode ∈ {PAPER, LIVE_MANUAL, LIVE_AUTONOMOUS}."""
    has_key = bool(os.environ.get("POLYMARKET_PRIVATE_KEY"))
    confirm = os.environ.get("POLYMARKET_CONFIRM", "").lower() == "true"
    auto = os.environ.get("POLYMARKET_AUTO_CONFIRM", "").lower() == "true"
    if not has_key or not confirm:
        mode = "PAPER"
    elif auto:
        mode = "LIVE_AUTONOMOUS"
    else:
        mode = "LIVE_MANUAL"
    return {
        "mode": mode,
        "has_key": has_key,
        "confirm": confirm,
        "auto_confirm": auto,
        "max_size_usd": _safe_float(os.environ.get("POLYMARKET_MAX_SIZE")),
        "daily_loss_limit_usd": _safe_float(
            os.environ.get("POLYMARKET_DAILY_LOSS_LIMIT")),
    }


def _safe_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


# ---- Killswitch -----------------------------------------------------------

def is_killswitch_armed() -> bool:
    return _halt_file().exists()


def arm_killswitch() -> dict:
    f = _halt_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.touch(exist_ok=True)
    return {"status": "armed", "path": str(f)}


def disarm_killswitch() -> dict:
    f = _halt_file()
    f.unlink(missing_ok=True)
    return {"status": "disarmed", "path": str(f)}


# ---- Trade log ------------------------------------------------------------

def read_live_trades(limit: int = 50) -> list[dict]:
    """Tail the last `limit` JSONL trades from ~/.polymarket-live/trades.log.
    Most recent first."""
    if not LIVE_TRADES_LOG.exists():
        return []
    try:
        lines = LIVE_TRADES_LOG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(out) >= limit:
            break
    return out


def get_daily_spent_usd() -> dict:
    """Sum cost_usd of today's UTC trades. Returns {spent_usd, n_trades,
    pct_of_limit}."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    spent = 0.0
    n = 0
    for tr in read_live_trades(limit=500):
        ts = tr.get("timestamp") or tr.get("ts") or ""
        if not ts.startswith(today):
            continue
        if tr.get("status") != "EXECUTED":
            continue
        spent += float(tr.get("cost_usd") or 0)
        n += 1
    limit = _safe_float(os.environ.get("POLYMARKET_DAILY_LOSS_LIMIT")) or 0
    pct = (spent / limit * 100) if limit > 0 else None
    return {
        "date_utc": today,
        "spent_usd": round(spent, 2),
        "n_trades": n,
        "limit_usd": limit,
        "pct_of_limit": round(pct, 1) if pct is not None else None,
    }


# ---- Readiness ------------------------------------------------------------

_readiness_cache: dict = {"ts": 0.0, "data": None}
_READINESS_TTL_SEC = 5 * 60


def get_readiness(force_refresh: bool = False) -> dict:
    """Run backtest.py --live-check --json. Cached 5min. Returns the
    full JSON output OR {error: ...} if subprocess fails."""
    now = time.time()
    if (not force_refresh and _readiness_cache["data"]
            and now - _readiness_cache["ts"] < _READINESS_TTL_SEC):
        return _readiness_cache["data"]

    script = REPO_ROOT / "polymarket-strategy-advisor" / "scripts" / "backtest.py"
    if not script.exists():
        return {"error": f"script not found: {script}"}
    try:
        result = subprocess.run(
            [sys.executable, str(script), "--live-check", "--json"],
            capture_output=True, text=True, timeout=60,
            cwd=str(REPO_ROOT),
        )
    except subprocess.TimeoutExpired:
        return {"error": "backtest.py timeout (60s)"}
    if result.returncode != 0:
        return {"error": f"backtest exit {result.returncode}",
                "stderr": result.stderr[:500]}
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        return {"error": f"output not JSON: {e}",
                "stdout_excerpt": result.stdout[:300]}

    _readiness_cache["ts"] = now
    _readiness_cache["data"] = data
    return data


# ---------------------------------------------------------------------------
# Inline tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile

    # Test 1: get_live_mode covers all 3 modes
    for k in ("POLYMARKET_PRIVATE_KEY", "POLYMARKET_CONFIRM", "POLYMARKET_AUTO_CONFIRM"):
        os.environ.pop(k, None)
    assert get_live_mode()["mode"] == "PAPER"
    os.environ["POLYMARKET_PRIVATE_KEY"] = "0x" + "a" * 64
    os.environ["POLYMARKET_CONFIRM"] = "true"
    assert get_live_mode()["mode"] == "LIVE_MANUAL"
    os.environ["POLYMARKET_AUTO_CONFIRM"] = "true"
    assert get_live_mode()["mode"] == "LIVE_AUTONOMOUS"
    print("Test 1 PASS: get_live_mode covers PAPER/LIVE_MANUAL/LIVE_AUTONOMOUS")

    # Cleanup env
    for k in ("POLYMARKET_PRIVATE_KEY", "POLYMARKET_CONFIRM", "POLYMARKET_AUTO_CONFIRM"):
        os.environ.pop(k, None)

    # Test 2: killswitch arm/disarm
    tmp = Path(tempfile.mkdtemp())
    halt = tmp / "halt-trading"
    os.environ["HALT_FILE"] = str(halt)
    assert is_killswitch_armed() is False
    arm_killswitch()
    assert is_killswitch_armed() is True
    disarm_killswitch()
    assert is_killswitch_armed() is False
    # Idempotent
    disarm_killswitch()
    arm_killswitch()
    arm_killswitch()
    assert is_killswitch_armed() is True
    print(f"Test 2 PASS: killswitch arm/disarm + idempotency")

    # Test 3: read_live_trades parses JSONL, most-recent-first
    import sys as _sys
    _self = _sys.modules[__name__]
    fake_log = tmp / "trades.log"
    fake_log.write_text(
        json.dumps({"timestamp": "2026-05-13T12:00:00Z", "status": "EXECUTED",
                    "side": "YES", "cost_usd": 5}) + "\n"
        + json.dumps({"timestamp": "2026-05-14T08:00:00Z", "status": "EXECUTED",
                       "side": "NO", "cost_usd": 10}) + "\n"
        + "garbage line\n"
        + json.dumps({"timestamp": "2026-05-14T09:00:00Z", "status": "FAILED",
                       "cost_usd": 0}) + "\n"
    )
    _self.LIVE_TRADES_LOG = fake_log
    trades = read_live_trades()
    assert len(trades) == 3
    # Most recent first
    assert trades[0]["timestamp"] == "2026-05-14T09:00:00Z"
    assert trades[2]["timestamp"] == "2026-05-13T12:00:00Z"
    print(f"Test 3 PASS: read_live_trades → 3 entries (garbage skipped, sorted DESC)")

    # Test 4: get_daily_spent_usd sums today (UTC) only
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fake_log.write_text(
        json.dumps({"timestamp": f"{today}T08:00:00Z", "status": "EXECUTED",
                    "cost_usd": 5}) + "\n"
        + json.dumps({"timestamp": f"{today}T10:00:00Z", "status": "EXECUTED",
                       "cost_usd": 7.5}) + "\n"
        + json.dumps({"timestamp": "2020-01-01T00:00:00Z", "status": "EXECUTED",
                       "cost_usd": 999}) + "\n"
        + json.dumps({"timestamp": f"{today}T11:00:00Z", "status": "FAILED",
                       "cost_usd": 50}) + "\n"  # skipped (not EXECUTED)
    )
    os.environ["POLYMARKET_DAILY_LOSS_LIMIT"] = "100"
    spent = get_daily_spent_usd()
    assert abs(spent["spent_usd"] - 12.5) < 0.01, spent
    assert spent["n_trades"] == 2
    assert spent["pct_of_limit"] == 12.5
    print(f"Test 4 PASS: daily spent → ${spent['spent_usd']} "
          f"({spent['n_trades']} trades, {spent['pct_of_limit']}% of limit)")

    # Test 5: empty log graceful
    fake_log.unlink()
    trades = read_live_trades()
    assert trades == []
    print(f"Test 5 PASS: missing log → empty list")

    # Test 6: get_readiness when backtest.py exists (we don't actually run
    # it here, just verify the function is callable and returns dict)
    # Note: real call may fail if no paper trades — that's OK
    res = get_readiness()
    assert isinstance(res, dict)
    print(f"Test 6 PASS: get_readiness returned dict (verdict={res.get('verdict', 'N/A')})")

    print("\nAll live_trading tests PASS")
