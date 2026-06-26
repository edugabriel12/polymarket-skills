#!/usr/bin/env python3
"""Persistent store for watched wallets (the copy-trade source list).

Each wallet is added with a name, an on-chain address (watched live) and its
bet-history CSV (from which we derive the per-category/confidence analysis AND
the confidence→value-band thresholds used to size the live trigger). Pure stdlib
sqlite3 so it has no extra deps.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

DEFAULT_DB = os.environ.get(
    "DASHBOARD_WALLETS_DB",
    os.path.expanduser("~/.polymarket-wallet-dashboard/wallets.db"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    name         TEXT NOT NULL,
    address      TEXT NOT NULL UNIQUE,
    csv_filename TEXT,
    analysis     TEXT NOT NULL,     -- JSON: rollup_csv report (by category + confidence)
    thresholds   TEXT NOT NULL      -- JSON: confidence_model.derive_thresholds
);
-- The watcher's dedup state: ONE row per (wallet, market) holding the highest
-- confidence tier already alerted (a tier UPGRADE re-pushes the same entry), plus
-- one row per market already pushed as settled.
CREATE TABLE IF NOT EXISTS seen_alerts (
    wallet_id    INTEGER NOT NULL,
    condition_id TEXT NOT NULL,
    confidence   TEXT NOT NULL,
    at           TEXT NOT NULL,
    PRIMARY KEY (wallet_id, condition_id)
);
CREATE TABLE IF NOT EXISTS settled_markets (
    wallet_id    INTEGER NOT NULL,
    condition_id TEXT NOT NULL,
    at           TEXT NOT NULL,
    PRIMARY KEY (wallet_id, condition_id)
);
-- Phase 2: live watched bets per wallet (latest state per market), so the wallet's
-- separated Resultados can merge them with the CSV snapshot. One row per (wallet, market).
CREATE TABLE IF NOT EXISTS wallet_bets (
    wallet_id      INTEGER NOT NULL,
    condition_id   TEXT NOT NULL,
    category       TEXT, subcategory TEXT, confidence TEXT, side TEXT,
    total_position REAL, entry_price REAL, odds REAL,
    status         TEXT NOT NULL DEFAULT 'OPEN',
    pnl            REAL,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (wallet_id, condition_id)
);
"""

_BET_FIELDS = ("category", "subcategory", "confidence", "side", "total_position",
               "entry_price", "odds", "status", "pnl")


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


def add_wallet(name: str, address: str, analysis: dict, thresholds: dict,
               csv_filename: str | None = None, db_path: str = DEFAULT_DB) -> int:
    """Insert or update (by address) a watched wallet. Returns its id."""
    addr = (address or "").strip().lower()
    con = connect(db_path)
    try:
        with con:
            con.execute(
                "INSERT INTO wallets(created_at, updated_at, name, address, csv_filename, "
                "analysis, thresholds) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(address) DO UPDATE SET updated_at=excluded.updated_at, "
                "name=excluded.name, csv_filename=excluded.csv_filename, "
                "analysis=excluded.analysis, thresholds=excluded.thresholds",
                (_now(), _now(), name.strip() or addr[:10], addr, csv_filename,
                 json.dumps(analysis, default=str), json.dumps(thresholds, default=str)))
            row = con.execute("SELECT id FROM wallets WHERE address=?", (addr,)).fetchone()
            return int(row["id"])
    finally:
        con.close()


def _summary(r: sqlite3.Row) -> dict:
    analysis = json.loads(r["analysis"])
    ov = analysis.get("overall", {})
    return {
        "id": r["id"], "name": r["name"], "address": r["address"],
        "csv_filename": r["csv_filename"], "created_at": r["created_at"],
        "updated_at": r["updated_at"], "n_markets": analysis.get("n_markets", 0),
        "win_rate": ov.get("win_rate"), "total_pnl": ov.get("total_pnl"), "roi": ov.get("roi"),
        "thresholds": json.loads(r["thresholds"]),
    }


def list_wallets(db_path: str = DEFAULT_DB) -> list[dict]:
    con = connect(db_path)
    try:
        return [_summary(r) for r in con.execute(
            "SELECT * FROM wallets ORDER BY created_at DESC")]
    finally:
        con.close()


def get_wallet(wallet_id: int, db_path: str = DEFAULT_DB) -> dict | None:
    con = connect(db_path)
    try:
        r = con.execute("SELECT * FROM wallets WHERE id=?", (wallet_id,)).fetchone()
        if not r:
            return None
        return {**_summary(r), "analysis": json.loads(r["analysis"])}
    finally:
        con.close()


def get_by_address(address: str, db_path: str = DEFAULT_DB) -> dict | None:
    con = connect(db_path)
    try:
        r = con.execute("SELECT * FROM wallets WHERE address=?",
                        ((address or "").strip().lower(),)).fetchone()
        return {**_summary(r), "analysis": json.loads(r["analysis"])} if r else None
    finally:
        con.close()


def delete_wallet(wallet_id: int, db_path: str = DEFAULT_DB) -> bool:
    con = connect(db_path)
    try:
        with con:
            con.execute("DELETE FROM seen_alerts WHERE wallet_id=?", (wallet_id,))
            con.execute("DELETE FROM settled_markets WHERE wallet_id=?", (wallet_id,))
            con.execute("DELETE FROM wallet_bets WHERE wallet_id=?", (wallet_id,))
            cur = con.execute("DELETE FROM wallets WHERE id=?", (wallet_id,))
            return cur.rowcount > 0
    finally:
        con.close()


# --- watcher dedup state ---------------------------------------------------
def seen_confidences(wallet_id: int, db_path: str = DEFAULT_DB) -> dict:
    """{condition_id: highest confidence tier already alerted} for a wallet."""
    con = connect(db_path)
    try:
        return {r["condition_id"]: r["confidence"] for r in con.execute(
            "SELECT condition_id, confidence FROM seen_alerts WHERE wallet_id=?", (wallet_id,))}
    finally:
        con.close()


def set_seen_confidence(wallet_id: int, condition_id: str, confidence: str,
                        db_path: str = DEFAULT_DB) -> None:
    """Upsert the highest tier alerted for (wallet, market)."""
    con = connect(db_path)
    try:
        with con:
            con.execute(
                "INSERT INTO seen_alerts(wallet_id, condition_id, confidence, at) VALUES(?,?,?,?) "
                "ON CONFLICT(wallet_id, condition_id) DO UPDATE SET confidence=excluded.confidence, "
                "at=excluded.at", (wallet_id, condition_id, confidence, _now()))
    finally:
        con.close()


def settled_keys(wallet_id: int, db_path: str = DEFAULT_DB) -> set:
    con = connect(db_path)
    try:
        return {r["condition_id"] for r in con.execute(
            "SELECT condition_id FROM settled_markets WHERE wallet_id=?", (wallet_id,))}
    finally:
        con.close()


def mark_settled(wallet_id: int, condition_id: str, db_path: str = DEFAULT_DB) -> None:
    con = connect(db_path)
    try:
        with con:
            con.execute("INSERT OR IGNORE INTO settled_markets(wallet_id, condition_id, at) "
                        "VALUES(?,?,?)", (wallet_id, condition_id, _now()))
    finally:
        con.close()


# --- live wallet bets (Phase 2) --------------------------------------------
def upsert_bet(wallet_id: int, condition_id: str, bet: dict,
               db_path: str = DEFAULT_DB) -> None:
    """Upsert the latest state of one watched market for a wallet."""
    con = connect(db_path)
    try:
        with con:
            con.execute(
                f"INSERT INTO wallet_bets(wallet_id, condition_id, {','.join(_BET_FIELDS)}, "
                f"updated_at) VALUES(?,?{',?' * len(_BET_FIELDS)},?) "
                f"ON CONFLICT(wallet_id, condition_id) DO UPDATE SET "
                f"{','.join(f'{f}=excluded.{f}' for f in _BET_FIELDS)}, updated_at=excluded.updated_at",
                (wallet_id, condition_id, *[bet.get(f) for f in _BET_FIELDS], _now()))
    finally:
        con.close()


def list_bets(wallet_id: int, db_path: str = DEFAULT_DB) -> list[dict]:
    con = connect(db_path)
    try:
        return [{k: r[k] for k in r.keys()} for r in con.execute(
            "SELECT * FROM wallet_bets WHERE wallet_id=?", (wallet_id,))]
    finally:
        con.close()
