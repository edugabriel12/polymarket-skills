"""Notifier service — Tier 4A.

Sends alerts to Telegram or email when the bot/portfolio crosses
operational thresholds (drawdown, daily loss, bot crash). Pluggable
backend via env var, no-op default so silent unless explicitly
configured.

Used by:
  - weather_edge_bot.py heartbeat task: drawdown/daily-loss thresholds
  - dashboard background task: bot/judge process crash detection
  - operator manual: POST /api/notify/test

Env vars:
  NOTIFIER_BACKEND        telegram|email|none (default none = no-op)
  NOTIFIER_RATE_LIMIT_MIN minutes between identical alerts (default 30)

  # Telegram backend
  TELEGRAM_BOT_TOKEN      from @BotFather
  TELEGRAM_CHAT_ID        your numeric chat id

  # Email backend (SMTP)
  SMTP_HOST               smtp.gmail.com etc.
  SMTP_PORT               587 (default)
  SMTP_USER               your email
  SMTP_PASS               app-specific password (NOT main password)
  NOTIFIER_TO_EMAIL       recipient (defaults to SMTP_USER)
"""

from __future__ import annotations

import json
import os
import smtplib
import ssl
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import requests


# Rate-limit state lives in a tiny json file so multiple processes
# (bot + dashboard) can coordinate.
_RATE_LIMIT_FILE = Path.home() / ".polymarket-paper" / "notifier_ratelimit.json"


SEVERITY_PREFIX = {
    "info":     "ℹ️ ",
    "success":  "✓ ",
    "warning":  "⚠ ",
    "critical": "🚨 ",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_rate_limit() -> dict:
    if not _RATE_LIMIT_FILE.exists():
        return {}
    try:
        return json.loads(_RATE_LIMIT_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_rate_limit(state: dict) -> None:
    try:
        _RATE_LIMIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        _RATE_LIMIT_FILE.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


def _is_rate_limited(key: str, window_min: int) -> bool:
    """Return True if we've sent the same alert within `window_min` minutes."""
    state = _load_rate_limit()
    last = state.get(key)
    if last is None:
        return False
    elapsed_sec = time.time() - float(last)
    return elapsed_sec < window_min * 60


def _mark_sent(key: str) -> None:
    state = _load_rate_limit()
    state[key] = time.time()
    # Garbage-collect entries older than 24h
    cutoff = time.time() - 24 * 3600
    state = {k: v for k, v in state.items() if float(v) > cutoff}
    _save_rate_limit(state)


def _send_telegram(title: str, body: str, severity: str) -> dict:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return {"status": "skipped",
                "reason": "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing"}
    prefix = SEVERITY_PREFIX.get(severity, "")
    text = f"{prefix}*{title}*\n\n{body}"
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
        if r.status_code != 200:
            return {"status": "failed",
                    "reason": f"telegram api {r.status_code}: {r.text[:200]}"}
        return {"status": "sent", "backend": "telegram"}
    except Exception as e:
        return {"status": "failed", "reason": f"telegram error: {e}"}


def _send_email(title: str, body: str, severity: str) -> dict:
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    to_email = os.environ.get("NOTIFIER_TO_EMAIL") or user
    if not host or not user or not password or not to_email:
        return {"status": "skipped",
                "reason": "SMTP_HOST/USER/PASS or NOTIFIER_TO_EMAIL missing"}
    prefix = SEVERITY_PREFIX.get(severity, "")
    subject = f"[Polymarket {severity.upper()}] {prefix}{title}"
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_email
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(host, port, timeout=15) as s:
            s.starttls(context=ctx)
            s.login(user, password)
            s.send_message(msg)
        return {"status": "sent", "backend": "email"}
    except Exception as e:
        return {"status": "failed", "reason": f"smtp error: {e}"}


def send(severity: str, title: str, body: str = "",
         rate_limit_key: Optional[str] = None) -> dict:
    """Send a notification. Returns {status, backend, reason}.

    severity: info | success | warning | critical
    title: short headline (1 line)
    body: longer description (multi-line OK)
    rate_limit_key: dedupe key (default = title). Same key within
                    NOTIFIER_RATE_LIMIT_MIN minutes is silently dropped.
    """
    backend = os.environ.get("NOTIFIER_BACKEND", "none").lower()
    if backend == "none":
        return {"status": "skipped", "reason": "NOTIFIER_BACKEND=none"}

    key = f"{rate_limit_key or title}|{severity}"
    window = int(os.environ.get("NOTIFIER_RATE_LIMIT_MIN", "30"))
    if _is_rate_limited(key, window):
        return {"status": "rate_limited",
                "reason": f"same alert sent within {window}min"}

    if backend == "telegram":
        result = _send_telegram(title, body, severity)
    elif backend == "email":
        result = _send_email(title, body, severity)
    else:
        return {"status": "failed",
                "reason": f"unknown NOTIFIER_BACKEND={backend!r}"}

    if result.get("status") == "sent":
        _mark_sent(key)
    return result


def is_configured() -> bool:
    """Quick check: is a notifier backend wired up?"""
    backend = os.environ.get("NOTIFIER_BACKEND", "none").lower()
    if backend == "none":
        return False
    if backend == "telegram":
        return bool(os.environ.get("TELEGRAM_BOT_TOKEN")
                    and os.environ.get("TELEGRAM_CHAT_ID"))
    if backend == "email":
        return bool(os.environ.get("SMTP_HOST")
                    and os.environ.get("SMTP_USER")
                    and os.environ.get("SMTP_PASS"))
    return False


def get_status() -> dict:
    """For the dashboard: show backend + readiness + last-sent timestamps."""
    backend = os.environ.get("NOTIFIER_BACKEND", "none").lower()
    state = _load_rate_limit()
    return {
        "backend": backend,
        "configured": is_configured(),
        "rate_limit_window_min": int(
            os.environ.get("NOTIFIER_RATE_LIMIT_MIN", "30")),
        "recent_sent_keys": list(state.keys())[:10],
    }


# ---------------------------------------------------------------------------
# Inline tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile

    # Use temp rate-limit file
    tmp = Path(tempfile.mkdtemp())
    import sys as _sys
    _self = _sys.modules[__name__]
    _self._RATE_LIMIT_FILE = tmp / "ratelimit.json"

    # Test 1: NOTIFIER_BACKEND=none → skipped
    os.environ["NOTIFIER_BACKEND"] = "none"
    r = send("info", "test", "body")
    assert r["status"] == "skipped" and "none" in r["reason"]
    print(f"Test 1 PASS: backend=none → skipped")

    # Test 2: telegram without creds → skipped
    os.environ["NOTIFIER_BACKEND"] = "telegram"
    os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    os.environ.pop("TELEGRAM_CHAT_ID", None)
    r = send("warning", "test no creds", "body", rate_limit_key="t2")
    assert r["status"] == "skipped" and "TELEGRAM" in r["reason"]
    print(f"Test 2 PASS: telegram no creds → skipped")

    # Test 3: rate limit dedupes
    # Simulate a successful send by directly marking the key
    _mark_sent("rate_test|warning")
    os.environ["NOTIFIER_BACKEND"] = "telegram"
    r = send("warning", "rate_test", "body", rate_limit_key="rate_test")
    assert r["status"] == "rate_limited", r
    print(f"Test 3 PASS: rate-limited within window → {r['reason']}")

    # Test 4: is_configured
    os.environ["NOTIFIER_BACKEND"] = "none"
    assert is_configured() is False
    os.environ["NOTIFIER_BACKEND"] = "telegram"
    os.environ["TELEGRAM_BOT_TOKEN"] = "fake"
    os.environ["TELEGRAM_CHAT_ID"] = "fake"
    assert is_configured() is True
    print(f"Test 4 PASS: is_configured() respects env state")

    # Test 5: get_status
    s = get_status()
    assert s["backend"] == "telegram"
    assert s["configured"] is True
    print(f"Test 5 PASS: get_status() → {s}")

    print("\nAll notifier tests PASS")
