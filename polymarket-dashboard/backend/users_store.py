#!/usr/bin/env python3
"""User accounts, sessions, single-use auth tokens, and per-user Telegram config.

Lives in the SAME SQLite file as the entries store (env ``SPORTS_ENTRIES_DB``, default
``~/.polymarket-dashboard/entries.db``) — its tables coexist with ``entries``/``settings``.
Pure stdlib sqlite3, mirroring the wallet-dashboard's ``wallets_store`` patterns.

Security note: only HASHES of session ids and verify/reset tokens are stored here. The raw
secret travels in the cookie / e-mail link and is never persisted, so a DB leak can't be
replayed into a session or a password reset.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone

import entries_store as es

DEFAULT_DB = es.DEFAULT_DB

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    full_name      TEXT NOT NULL,
    email          TEXT NOT NULL UNIQUE,          -- always stored lowercased
    password_hash  TEXT NOT NULL,
    email_verified INTEGER NOT NULL DEFAULT 0,    -- 0 until the e-mail link is clicked
    verified_at    TEXT
);
-- Server-side sessions. The cookie carries the RAW id; we store only its sha256.
CREATE TABLE IF NOT EXISTS sessions (
    id_hash      TEXT PRIMARY KEY,
    user_id      INTEGER NOT NULL,
    created_at   TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    expires_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
-- Single-use e-mail verification / password-reset tokens (store the sha256, e-mail the raw).
CREATE TABLE IF NOT EXISTS auth_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    kind       TEXT NOT NULL,                     -- 'verify' | 'reset'
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_tokens_user ON auth_tokens(user_id, kind);
-- Per-user Telegram config (replaces the global `settings` telegram_* for the fan-out).
CREATE TABLE IF NOT EXISTS user_telegram (
    user_id    INTEGER PRIMARY KEY,
    token      TEXT,
    chat_id    TEXT,
    updated_at TEXT NOT NULL
);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _nowiso() -> str:
    return _iso(_now())


def connect(db_path: str = DEFAULT_DB) -> sqlite3.Connection:
    d = os.path.dirname(db_path)
    if d:
        os.makedirs(d, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    return con


# --- users -----------------------------------------------------------------
def _user(r: sqlite3.Row | None) -> dict | None:
    return {k: r[k] for k in r.keys()} if r else None


def create_user(full_name: str, email: str, password_hash: str,
                db_path: str = DEFAULT_DB) -> int:
    """Insert a new (unverified) user. Raises sqlite3.IntegrityError if the email exists."""
    con = connect(db_path)
    try:
        with con:
            cur = con.execute(
                "INSERT INTO users(created_at, updated_at, full_name, email, password_hash) "
                "VALUES(?,?,?,?,?)",
                (_nowiso(), _nowiso(), full_name.strip(), email.strip().lower(), password_hash))
            return int(cur.lastrowid)
    finally:
        con.close()


def get_user_by_email(email: str, db_path: str = DEFAULT_DB) -> dict | None:
    con = connect(db_path)
    try:
        return _user(con.execute(
            "SELECT * FROM users WHERE email=?", ((email or "").strip().lower(),)).fetchone())
    finally:
        con.close()


def get_user_by_id(user_id: int, db_path: str = DEFAULT_DB) -> dict | None:
    con = connect(db_path)
    try:
        return _user(con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())
    finally:
        con.close()


def set_password(user_id: int, password_hash: str, db_path: str = DEFAULT_DB) -> None:
    con = connect(db_path)
    try:
        with con:
            con.execute("UPDATE users SET password_hash=?, updated_at=? WHERE id=?",
                        (password_hash, _nowiso(), user_id))
    finally:
        con.close()


def mark_verified(user_id: int, db_path: str = DEFAULT_DB) -> None:
    con = connect(db_path)
    try:
        with con:
            con.execute(
                "UPDATE users SET email_verified=1, verified_at=?, updated_at=? WHERE id=?",
                (_nowiso(), _nowiso(), user_id))
    finally:
        con.close()


# --- sessions --------------------------------------------------------------
def create_session(user_id: int, id_hash: str, ttl_days: int = 30,
                   db_path: str = DEFAULT_DB) -> None:
    now = _now()
    con = connect(db_path)
    try:
        with con:
            con.execute(
                "INSERT INTO sessions(id_hash, user_id, created_at, last_seen_at, expires_at) "
                "VALUES(?,?,?,?,?)",
                (id_hash, user_id, _iso(now), _iso(now), _iso(now + timedelta(days=ttl_days))))
    finally:
        con.close()


def get_valid_session(id_hash: str, db_path: str = DEFAULT_DB) -> dict | None:
    """Return the session row if present and not expired; lazily deletes an expired one."""
    con = connect(db_path)
    try:
        r = con.execute("SELECT * FROM sessions WHERE id_hash=?", (id_hash,)).fetchone()
        if not r:
            return None
        if r["expires_at"] <= _nowiso():                  # ISO-UTC strings sort chronologically
            with con:
                con.execute("DELETE FROM sessions WHERE id_hash=?", (id_hash,))
            return None
        return {k: r[k] for k in r.keys()}
    finally:
        con.close()


def touch_session(id_hash: str, ttl_days: int = 30, db_path: str = DEFAULT_DB) -> None:
    """Sliding expiry: bump last_seen_at + push expires_at forward on activity."""
    now = _now()
    con = connect(db_path)
    try:
        with con:
            con.execute("UPDATE sessions SET last_seen_at=?, expires_at=? WHERE id_hash=?",
                        (_iso(now), _iso(now + timedelta(days=ttl_days)), id_hash))
    finally:
        con.close()


def delete_session(id_hash: str, db_path: str = DEFAULT_DB) -> None:
    con = connect(db_path)
    try:
        with con:
            con.execute("DELETE FROM sessions WHERE id_hash=?", (id_hash,))
    finally:
        con.close()


def delete_user_sessions(user_id: int, db_path: str = DEFAULT_DB) -> None:
    """Revoke ALL of a user's sessions (used after a password reset)."""
    con = connect(db_path)
    try:
        with con:
            con.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    finally:
        con.close()


# --- single-use auth tokens ------------------------------------------------
def create_token(user_id: int, kind: str, token_hash: str, ttl_hours: int,
                 db_path: str = DEFAULT_DB) -> None:
    now = _now()
    con = connect(db_path)
    try:
        with con:
            con.execute(
                "INSERT INTO auth_tokens(token_hash, user_id, kind, created_at, expires_at) "
                "VALUES(?,?,?,?,?)",
                (token_hash, user_id, kind, _iso(now), _iso(now + timedelta(hours=ttl_hours))))
    finally:
        con.close()


def consume_token(kind: str, token_hash: str, db_path: str = DEFAULT_DB) -> int | None:
    """Atomically validate + mark used. Returns the user_id for a token that is the right kind,
    unused and unexpired; None otherwise. Single-use is enforced inside the transaction."""
    con = connect(db_path)
    try:
        with con:
            r = con.execute("SELECT * FROM auth_tokens WHERE token_hash=? AND kind=?",
                            (token_hash, kind)).fetchone()
            if not r or r["used_at"] or r["expires_at"] <= _nowiso():
                return None
            con.execute("UPDATE auth_tokens SET used_at=? WHERE token_hash=?",
                        (_nowiso(), token_hash))
            return int(r["user_id"])
    finally:
        con.close()


def invalidate_tokens(user_id: int, kind: str, db_path: str = DEFAULT_DB) -> None:
    """Drop a user's outstanding tokens of a kind (e.g. before issuing a fresh one)."""
    con = connect(db_path)
    try:
        with con:
            con.execute("DELETE FROM auth_tokens WHERE user_id=? AND kind=?", (user_id, kind))
    finally:
        con.close()


# --- per-user Telegram config ----------------------------------------------
def get_user_telegram(user_id: int, db_path: str = DEFAULT_DB) -> dict:
    con = connect(db_path)
    try:
        r = con.execute("SELECT token, chat_id FROM user_telegram WHERE user_id=?",
                        (user_id,)).fetchone()
        return {"token": (r["token"] if r else "") or "",
                "chat_id": (r["chat_id"] if r else "") or ""}
    finally:
        con.close()


def set_user_telegram(user_id: int, token: str, chat_id: str,
                      db_path: str = DEFAULT_DB) -> None:
    con = connect(db_path)
    try:
        with con:
            con.execute(
                "INSERT INTO user_telegram(user_id, token, chat_id, updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET token=excluded.token, "
                "chat_id=excluded.chat_id, updated_at=excluded.updated_at",
                (user_id, token or "", chat_id or "", _nowiso()))
    finally:
        con.close()


def delete_user_telegram(user_id: int, db_path: str = DEFAULT_DB) -> None:
    con = connect(db_path)
    try:
        with con:
            con.execute("DELETE FROM user_telegram WHERE user_id=?", (user_id,))
    finally:
        con.close()


def list_telegram_recipients(db_path: str = DEFAULT_DB) -> list[dict]:
    """[{user_id, token, chat_id}] for every VERIFIED user with a fully configured Telegram —
    the fan-out targets for an ingested entry."""
    con = connect(db_path)
    try:
        rows = con.execute(
            "SELECT ut.user_id, ut.token, ut.chat_id FROM user_telegram ut "
            "JOIN users u ON u.id = ut.user_id "
            "WHERE u.email_verified=1 AND ut.token<>'' AND ut.chat_id<>''")
        return [{"user_id": r["user_id"], "token": r["token"], "chat_id": r["chat_id"]}
                for r in rows]
    finally:
        con.close()
