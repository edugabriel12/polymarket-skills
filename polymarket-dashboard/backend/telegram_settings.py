#!/usr/bin/env python3
"""Persisted Telegram config (bot token + chat id) for the storefront, and chat-id
auto-discovery via the Bot API.

The token is saved by the user in the Telegram tab; the chat id is discovered from
getUpdates (the user must have sent the bot a message — e.g. /start — first). Stored
in the entries DB's settings table. Env (TELEGRAM_BOT_TOKEN/CHAT_ID) is the fallback.
"""

from __future__ import annotations

import os
import sqlite3
import sys

import entries_store as es

_ENV_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
_ENV_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")


def _con(db_path: str | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(db_path or es.DEFAULT_DB)
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    return con


def _get(key: str, db_path: str | None = None) -> str:
    con = _con(db_path)
    try:
        r = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else ""
    finally:
        con.close()


def _set(key: str, value: str, db_path: str | None = None) -> None:
    con = _con(db_path)
    try:
        with con:
            con.execute("INSERT INTO settings(key, value) VALUES(?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value or ""))
    finally:
        con.close()


def get_config(db_path: str | None = None) -> dict:
    """{token, chat_id} — saved settings, falling back to env."""
    return {"token": _get("telegram_token", db_path) or _ENV_TOKEN,
            "chat_id": _get("telegram_chat_id", db_path) or _ENV_CHAT}


def save_config(token: str, chat_id: str, db_path: str | None = None) -> None:
    _set("telegram_token", token or "", db_path)
    _set("telegram_chat_id", chat_id or "", db_path)


def discover_chat_id(token: str, *, client=None) -> str | None:
    """Most recent chat id the bot has seen via getUpdates. None if the user hasn't
    messaged the bot yet (or on any error)."""
    if not token:
        return None
    if client is None:
        import requests
        client = requests
    try:
        r = client.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=8)
        r.raise_for_status()
        data = r.json()
    except Exception as e:  # noqa: BLE001
        print(f"[telegram] getUpdates failed: {e}", file=sys.stderr, flush=True)
        return None
    if not data.get("ok"):
        return None
    for upd in reversed(data.get("result") or []):     # newest first
        for k in ("message", "edited_message", "channel_post", "my_chat_member"):
            chat = (upd.get(k) or {}).get("chat") or {}
            if chat.get("id") is not None:
                return str(chat["id"])
    return None
