"""Cross-platform process manager for the bot/judge daemons.

Bot/judge write a PID file (JSON) on startup containing `pid`, `argv`,
`cwd`, `started_at`. This module reads those files to:
  - check if the process is alive
  - kill it gracefully (SIGTERM/CTRL_BREAK), falling back to forceful
    kill (SIGKILL on Unix, taskkill /F on Windows) if it doesn't exit
    within a timeout
  - respawn it detached with the same argv/cwd, redirecting
    stdout/stderr to ~/.polymarket-paper/{target}.out.log

Used by SuggestionApplier when `auto_restart=True` is passed.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional


PID_DIR = Path.home() / ".polymarket-paper"

TARGET_PIDFILES = {
    "bot": PID_DIR / "bot.pid.json",
    "judge": PID_DIR / "judge.pid.json",
}

# Mapping of suggestion category → list of processes to restart.
CATEGORY_TARGETS = {
    "threshold":    ["bot"],
    "mae_constant": ["bot", "judge"],
    "city":         ["bot"],
    "risk_limit":   ["bot"],
    "judge_prompt": ["judge"],
}


def read_pidfile(target: str) -> Optional[dict]:
    """Read and parse the JSON PID file for `target`. Returns None if
    file missing or malformed."""
    path = TARGET_PIDFILES.get(target)
    if not path or not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def is_alive(pid: int) -> bool:
    """Check if a PID is running. Cross-platform, no psutil dep."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            PROCESS_QUERY_INFO = 0x0400
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_INFO, False, pid)
            if not handle:
                return False
            # Check if still running via exit code
            exit_code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(
                handle, ctypes.byref(exit_code))
            ctypes.windll.kernel32.CloseHandle(handle)
            # STILL_ACTIVE == 259
            return exit_code.value == 259
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def kill_gracefully(pid: int, timeout: float = 5.0) -> dict:
    """Send graceful shutdown signal, wait, then force-kill if needed."""
    if not is_alive(pid):
        return {"status": "not_running"}

    # Send graceful signal
    try:
        if sys.platform == "win32":
            # CTRL_BREAK_EVENT requires the process to have been spawned
            # with CREATE_NEW_PROCESS_GROUP. If our spawn used that flag,
            # this works; otherwise it'll fail and we fall back to taskkill.
            try:
                os.kill(pid, signal.CTRL_BREAK_EVENT)
            except (AttributeError, OSError):
                # Falls through to force kill below
                pass
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError as e:
        return {"status": "signal_failed", "error": str(e)}

    # Wait up to timeout for the process to exit gracefully
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_alive(pid):
            return {"status": "killed_graceful"}
        time.sleep(0.2)

    # Force kill
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0 and is_alive(pid):
                return {"status": "kill_failed",
                        "error": result.stderr or result.stdout}
        else:
            os.kill(pid, signal.SIGKILL)
        # Give it a moment to disappear
        time.sleep(0.5)
        if is_alive(pid):
            return {"status": "kill_failed",
                    "error": "still alive after force kill"}
        return {"status": "killed_forced"}
    except Exception as e:
        return {"status": "kill_failed", "error": str(e)}


def respawn(target: str, original: dict) -> dict:
    """Spawn a detached process with the original argv/cwd. Redirects
    stdout/stderr to a log file under ~/.polymarket-paper/."""
    argv = original.get("argv") or []
    cwd = original.get("cwd")
    if not argv:
        return {"status": "respawn_failed", "error": "no argv in pidfile"}

    PID_DIR.mkdir(parents=True, exist_ok=True)
    log_path = PID_DIR / f"{target}.out.log"

    try:
        log_fd = open(log_path, "ab")
    except OSError as e:
        return {"status": "respawn_failed",
                "error": f"cannot open log: {e}"}

    kwargs = {
        "cwd": cwd,
        "stdout": log_fd,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(argv, **kwargs)
    except Exception as e:
        return {"status": "respawn_failed", "error": str(e)}

    # Don't wait — process is detached. Quick sanity check.
    time.sleep(0.5)
    return {
        "status": "respawned",
        "new_pid": proc.pid,
        "log_path": str(log_path),
    }


def restart(target: str, timeout: float = 5.0) -> dict:
    """Read pidfile, kill the process, respawn detached.
    Returns combined result dict with kill + respawn info."""
    original = read_pidfile(target)
    if not original:
        return {
            "status": "no_pidfile",
            "target": target,
            "hint": (f"{target} hasn't started since v5 deploy — "
                     "start it manually, then auto-restart will work next time"),
        }
    pid = original.get("pid")
    if not isinstance(pid, int):
        return {"status": "bad_pidfile", "target": target}

    kill_result = kill_gracefully(pid, timeout=timeout)
    if kill_result["status"] not in (
            "not_running", "killed_graceful", "killed_forced"):
        return {"target": target, "status": "kill_failed",
                "details": kill_result}

    spawn_result = respawn(target, original)
    return {"target": target, "kill": kill_result, "spawn": spawn_result,
            "status": spawn_result["status"]}


def restart_for_category(category: str, timeout: float = 5.0) -> list[dict]:
    """Restart all processes affected by a given suggestion category."""
    targets = CATEGORY_TARGETS.get(category, [])
    return [restart(t, timeout=timeout) for t in targets]


# ---------------------------------------------------------------------------
# Inline tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile

    tmp = Path(tempfile.mkdtemp())

    # Test 1: read_pidfile with missing file
    import sys as _sys
    _self = _sys.modules[__name__]
    _self.TARGET_PIDFILES = {
        "bot": tmp / "bot.pid.json",
        "judge": tmp / "judge.pid.json",
    }
    assert read_pidfile("bot") is None
    print("Test 1 PASS: read_pidfile returns None when missing")

    # Test 2: write + read roundtrip
    bot_pf = _self.TARGET_PIDFILES["bot"]
    bot_pf.write_text(json.dumps({
        "pid": 12345, "argv": ["python", "bot.py"],
        "cwd": "/tmp", "started_at": "2026-05-14T00:00:00Z",
    }))
    data = read_pidfile("bot")
    assert data["pid"] == 12345 and data["argv"] == ["python", "bot.py"]
    print(f"Test 2 PASS: pidfile roundtrip — pid {data['pid']}")

    # Test 3: is_alive on current process
    assert is_alive(os.getpid()) is True
    print(f"Test 3 PASS: is_alive(self pid {os.getpid()}) → True")

    # Test 4: is_alive on dead pid
    # Pick a PID very unlikely to exist
    assert is_alive(99999999) is False
    print("Test 4 PASS: is_alive(99999999) → False")

    # Test 5: restart with no pidfile
    bot_pf.unlink()
    result = restart("bot")
    assert result["status"] == "no_pidfile", result
    print(f"Test 5 PASS: restart no_pidfile → {result['status']}")

    # Test 6: category mapping
    assert CATEGORY_TARGETS["threshold"] == ["bot"]
    assert CATEGORY_TARGETS["mae_constant"] == ["bot", "judge"]
    assert CATEGORY_TARGETS["judge_prompt"] == ["judge"]
    print("Test 6 PASS: CATEGORY_TARGETS mapping correct")

    print("\nAll process_manager tests PASS")
