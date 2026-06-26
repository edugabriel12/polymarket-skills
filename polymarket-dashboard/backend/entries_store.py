#!/usr/bin/env python3
"""Store of ingested entries (the storefront's only data).

Each entry arrives via POST /api/copy/ingest from the brain (wallet-dashboard) —
both model and watched-wallet entries, indistinguishable here (no source field).
Keyed by `key` (stable per origin). OPEN entries feed the category cards; settled
entries (WON/LOST/VOID) feed the combined results. Pure stdlib sqlite3.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

DEFAULT_DB = os.environ.get(
    "SPORTS_ENTRIES_DB",
    os.path.expanduser("~/.polymarket-dashboard/entries.db"))

_FIELDS = ("event", "category", "subcategory", "side", "odds", "entry_price", "unit",
           "confidence", "live", "market_url", "game_start", "status", "pnl")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    key         TEXT PRIMARY KEY,
    event       TEXT, category TEXT, subcategory TEXT, side TEXT,
    odds        REAL, entry_price REAL, unit REAL, confidence TEXT,
    live        TEXT, market_url TEXT, game_start TEXT,
    status      TEXT NOT NULL DEFAULT 'OPEN',
    pnl         REAL,
    alerted_unit REAL,                 -- the unit last sent to Telegram (fire on upgrade)
    first_seen  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entries_status ON entries(status);
CREATE INDEX IF NOT EXISTS idx_entries_cat ON entries(category);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: str = DEFAULT_DB) -> sqlite3.Connection:
    d = os.path.dirname(db_path)
    if d:
        os.makedirs(d, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    return con


def upsert(entry: dict, db_path: str = DEFAULT_DB) -> str:
    """Insert/update one entry. Returns the change kind:
      'new'      — first time seen as OPEN (→ Telegram)
      'upgrade'  — still OPEN but its unit increased (→ Telegram)
      'settled'  — transitioned to WON/LOST/VOID (→ results, no Telegram)
      'unchanged'— nothing material changed (no Telegram)
    """
    key = entry.get("key")
    if not key:
        return "unchanged"
    status = entry.get("status") or "OPEN"
    con = connect(db_path)
    try:
        with con:
            row = con.execute("SELECT status, unit, alerted_unit FROM entries WHERE key=?",
                              (key,)).fetchone()
            vals = [entry.get(f) for f in _FIELDS]
            if row is None:
                kind = "new" if status == "OPEN" else "settled"
                alerted = entry.get("unit") if kind == "new" else None
                con.execute(
                    f"INSERT INTO entries(key, {','.join(_FIELDS)}, alerted_unit, first_seen, "
                    f"updated_at) VALUES(?{',?' * len(_FIELDS)},?,?,?)",
                    (key, *vals, alerted, _now(), _now()))
                return kind
            # existing
            if status != "OPEN":
                con.execute(
                    f"UPDATE entries SET {','.join(f'{f}=?' for f in _FIELDS)}, updated_at=? "
                    f"WHERE key=?", (*vals, _now(), key))
                return "settled" if row["status"] == "OPEN" else "unchanged"
            # still OPEN — detect a unit upgrade vs what we last alerted
            new_unit = float(entry.get("unit") or 0)
            alerted = row["alerted_unit"]
            upgrade = alerted is None or new_unit > float(alerted)
            con.execute(
                f"UPDATE entries SET {','.join(f'{f}=?' for f in _FIELDS)}, "
                f"alerted_unit=?, updated_at=? WHERE key=?",
                (*vals, new_unit if upgrade else alerted, _now(), key))
            return "upgrade" if upgrade else "unchanged"
    finally:
        con.close()


def _row(r: sqlite3.Row) -> dict:
    return {k: r[k] for k in r.keys()}


def list_open(db_path: str = DEFAULT_DB) -> list[dict]:
    con = connect(db_path)
    try:
        return [_row(r) for r in con.execute(
            "SELECT * FROM entries WHERE status='OPEN' ORDER BY updated_at DESC")]
    finally:
        con.close()


def list_settled(db_path: str = DEFAULT_DB) -> list[dict]:
    con = connect(db_path)
    try:
        return [_row(r) for r in con.execute(
            "SELECT * FROM entries WHERE status IN ('WON','LOST','VOID') "
            "ORDER BY updated_at DESC")]
    finally:
        con.close()
