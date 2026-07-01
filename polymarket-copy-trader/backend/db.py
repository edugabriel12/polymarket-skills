"""SQLite persistence for the copy-trader.

Tables (the two user-facing ones are `wallets` and `entries`, linked by wallet_id):
  wallets          — saved public wallets to copy (name + address)
  entries          — every copy attempt (executed OR skipped), FK -> wallets
  paper_positions  — internal: paper holdings per (wallet, market), for sizing
                     proportional sells and live unrealized P&L
  wallet_holdings  — internal: running tally of the TRACKED wallet's post-baseline
                     position per market, so we know the fraction it sold
  paper_state      — single-row mock portfolio (fake cash)

DB path: ~/.polymarket-copy-trader/copytrade.db (override with COPYTRADE_DB).
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_DB = os.environ.get(
    "COPYTRADE_DB", str(Path.home() / ".polymarket-copy-trader" / "copytrade.db")
)

STARTING_BALANCE = 10_000.0  # fake USD in the paper mock wallet


# ---------------------------------------------------------------------------
# Connection / schema
# ---------------------------------------------------------------------------
def connect(db_path: str = DEFAULT_DB) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str = DEFAULT_DB) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS wallets (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                name          TEXT NOT NULL,
                address       TEXT NOT NULL UNIQUE,
                active        INTEGER NOT NULL DEFAULT 1,
                created_at    TEXT NOT NULL DEFAULT (datetime('now')),
                last_poll_at  TEXT,
                baseline_ts   REAL NOT NULL DEFAULT 0,
                cursor_ts     REAL NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS entries (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet_id       INTEGER NOT NULL REFERENCES wallets(id) ON DELETE CASCADE,
                condition_id    TEXT NOT NULL,
                token_id        TEXT,
                market_question TEXT,
                market_slug     TEXT,
                market_url      TEXT,
                copy_action     TEXT NOT NULL,            -- BUY | SELL
                source_price    REAL,
                source_trade_ts REAL,
                requested_usd   REAL,
                executed_usd    REAL,
                shares          REAL,
                avg_fill_price  REAL,
                best_price      REAL,
                slippage_pct    REAL,
                volume_24h      REAL,
                status          TEXT NOT NULL,            -- EXECUTED | SKIPPED
                skip_reason     TEXT,
                result_status   TEXT NOT NULL DEFAULT 'OPEN',  -- OPEN|WIN|LOSS|VOID
                current_price   REAL,
                realized_pnl    REAL,
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                settled_at      TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_entries_wallet ON entries(wallet_id);
            CREATE INDEX IF NOT EXISTS idx_entries_status ON entries(status);

            CREATE TABLE IF NOT EXISTS paper_positions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet_id       INTEGER NOT NULL REFERENCES wallets(id) ON DELETE CASCADE,
                condition_id    TEXT NOT NULL,
                token_id        TEXT,
                market_question TEXT,
                market_url      TEXT,
                side            TEXT,
                shares          REAL NOT NULL DEFAULT 0,
                avg_entry       REAL NOT NULL DEFAULT 0,
                opened_at       TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
                closed          INTEGER NOT NULL DEFAULT 0,
                UNIQUE(wallet_id, condition_id)
            );

            CREATE TABLE IF NOT EXISTS wallet_holdings (
                wallet_id     INTEGER NOT NULL REFERENCES wallets(id) ON DELETE CASCADE,
                condition_id  TEXT NOT NULL,
                shares        REAL NOT NULL DEFAULT 0,
                avg_price     REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (wallet_id, condition_id)
            );

            CREATE TABLE IF NOT EXISTS paper_state (
                id               INTEGER PRIMARY KEY CHECK (id = 1),
                starting_balance REAL NOT NULL,
                cash_balance     REAL NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO paper_state (id, starting_balance, cash_balance) "
            "VALUES (1, ?, ?)",
            (STARTING_BALANCE, STARTING_BALANCE),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Wallets
# ---------------------------------------------------------------------------
def add_wallet(name: str, address: str, baseline_ts: float = 0.0,
               db_path: str = DEFAULT_DB) -> dict:
    conn = connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO wallets (name, address, baseline_ts, cursor_ts) "
            "VALUES (?, ?, ?, ?)",
            (name, address.lower(), baseline_ts, baseline_ts),
        )
        conn.commit()
        return get_wallet(cur.lastrowid, db_path)
    finally:
        conn.close()


def list_wallets(active_only: bool = False, db_path: str = DEFAULT_DB) -> list[dict]:
    conn = connect(db_path)
    try:
        q = "SELECT * FROM wallets"
        if active_only:
            q += " WHERE active = 1"
        q += " ORDER BY id"
        return [dict(r) for r in conn.execute(q).fetchall()]
    finally:
        conn.close()


def get_wallet(wallet_id: int, db_path: str = DEFAULT_DB) -> dict | None:
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT * FROM wallets WHERE id = ?", (wallet_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def get_wallet_by_address(address: str, db_path: str = DEFAULT_DB) -> dict | None:
    conn = connect(db_path)
    try:
        r = conn.execute(
            "SELECT * FROM wallets WHERE address = ?", (address.lower(),)
        ).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def set_wallet_active(wallet_id: int, active: bool, db_path: str = DEFAULT_DB) -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            "UPDATE wallets SET active = ? WHERE id = ?", (1 if active else 0, wallet_id)
        )
        conn.commit()
    finally:
        conn.close()


def update_cursor(wallet_id: int, cursor_ts: float, db_path: str = DEFAULT_DB) -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            "UPDATE wallets SET cursor_ts = ?, last_poll_at = datetime('now') WHERE id = ?",
            (cursor_ts, wallet_id),
        )
        conn.commit()
    finally:
        conn.close()


def delete_wallet(wallet_id: int, db_path: str = DEFAULT_DB) -> None:
    conn = connect(db_path)
    try:
        conn.execute("DELETE FROM wallets WHERE id = ?", (wallet_id,))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------
_ENTRY_COLS = (
    "wallet_id", "condition_id", "token_id", "market_question", "market_slug",
    "market_url", "copy_action", "source_price", "source_trade_ts", "requested_usd",
    "executed_usd", "shares", "avg_fill_price", "best_price", "slippage_pct",
    "volume_24h", "status", "skip_reason", "result_status", "current_price",
    "realized_pnl",
)


def insert_entry(entry: dict, db_path: str = DEFAULT_DB) -> int:
    conn = connect(db_path)
    try:
        cols = [c for c in _ENTRY_COLS if c in entry]
        placeholders = ", ".join("?" for _ in cols)
        cur = conn.execute(
            f"INSERT INTO entries ({', '.join(cols)}) VALUES ({placeholders})",
            tuple(entry[c] for c in cols),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_entries(wallet_id: int | None = None, status: str | None = None,
                 page: int = 1, page_size: int = 20,
                 db_path: str = DEFAULT_DB) -> dict:
    conn = connect(db_path)
    try:
        where, params = [], []
        if wallet_id is not None:
            where.append("e.wallet_id = ?")
            params.append(wallet_id)
        if status:
            where.append("e.status = ?")
            params.append(status)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total = conn.execute(
            f"SELECT COUNT(*) FROM entries e{clause}", params
        ).fetchone()[0]
        offset = (max(page, 1) - 1) * page_size
        rows = conn.execute(
            f"SELECT e.*, w.name AS wallet_name, w.address AS wallet_address "
            f"FROM entries e JOIN wallets w ON w.id = e.wallet_id{clause} "
            f"ORDER BY e.id DESC LIMIT ? OFFSET ?",
            (*params, page_size, offset),
        ).fetchall()
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "entries": [dict(r) for r in rows],
        }
    finally:
        conn.close()


def open_buy_entries(wallet_id: int, db_path: str = DEFAULT_DB) -> list[dict]:
    """EXECUTED BUY entries still OPEN — candidates for settlement refresh."""
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM entries WHERE wallet_id = ? AND copy_action = 'BUY' "
            "AND status = 'EXECUTED' AND result_status = 'OPEN'",
            (wallet_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_entry_result(entry_id: int, result_status: str, current_price: float,
                        realized_pnl: float | None, settled: bool = False,
                        db_path: str = DEFAULT_DB) -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            "UPDATE entries SET result_status = ?, current_price = ?, realized_pnl = ?, "
            "settled_at = CASE WHEN ? THEN datetime('now') ELSE settled_at END "
            "WHERE id = ?",
            (result_status, current_price, realized_pnl, 1 if settled else 0, entry_id),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Paper positions  (internal)
# ---------------------------------------------------------------------------
def get_paper_position(wallet_id: int, condition_id: str,
                       db_path: str = DEFAULT_DB) -> dict | None:
    conn = connect(db_path)
    try:
        r = conn.execute(
            "SELECT * FROM paper_positions WHERE wallet_id = ? AND condition_id = ?",
            (wallet_id, condition_id),
        ).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def list_open_paper_positions(db_path: str = DEFAULT_DB) -> list[dict]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT p.*, w.name AS wallet_name FROM paper_positions p "
            "JOIN wallets w ON w.id = p.wallet_id "
            "WHERE p.closed = 0 AND p.shares > 0"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def upsert_paper_position(wallet_id: int, condition_id: str, fields: dict,
                          db_path: str = DEFAULT_DB) -> None:
    conn = connect(db_path)
    try:
        existing = conn.execute(
            "SELECT id FROM paper_positions WHERE wallet_id = ? AND condition_id = ?",
            (wallet_id, condition_id),
        ).fetchone()
        if existing:
            sets = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(
                f"UPDATE paper_positions SET {sets}, updated_at = datetime('now') "
                f"WHERE wallet_id = ? AND condition_id = ?",
                (*fields.values(), wallet_id, condition_id),
            )
        else:
            cols = ["wallet_id", "condition_id", *fields.keys()]
            placeholders = ", ".join("?" for _ in cols)
            conn.execute(
                f"INSERT INTO paper_positions ({', '.join(cols)}) VALUES ({placeholders})",
                (wallet_id, condition_id, *fields.values()),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tracked wallet holdings  (internal)
# ---------------------------------------------------------------------------
def get_holding(wallet_id: int, condition_id: str,
                db_path: str = DEFAULT_DB) -> dict:
    conn = connect(db_path)
    try:
        r = conn.execute(
            "SELECT * FROM wallet_holdings WHERE wallet_id = ? AND condition_id = ?",
            (wallet_id, condition_id),
        ).fetchone()
        return dict(r) if r else {"wallet_id": wallet_id, "condition_id": condition_id,
                                  "shares": 0.0, "avg_price": 0.0}
    finally:
        conn.close()


def set_holding(wallet_id: int, condition_id: str, shares: float, avg_price: float,
                db_path: str = DEFAULT_DB) -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO wallet_holdings (wallet_id, condition_id, shares, avg_price) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(wallet_id, condition_id) "
            "DO UPDATE SET shares = excluded.shares, avg_price = excluded.avg_price",
            (wallet_id, condition_id, shares, avg_price),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Paper state (mock cash)
# ---------------------------------------------------------------------------
def get_cash(db_path: str = DEFAULT_DB) -> float:
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT cash_balance FROM paper_state WHERE id = 1").fetchone()
        return float(r["cash_balance"]) if r else STARTING_BALANCE
    finally:
        conn.close()


def get_paper_state(db_path: str = DEFAULT_DB) -> dict:
    conn = connect(db_path)
    try:
        r = conn.execute("SELECT * FROM paper_state WHERE id = 1").fetchone()
        return dict(r) if r else {"starting_balance": STARTING_BALANCE,
                                  "cash_balance": STARTING_BALANCE}
    finally:
        conn.close()


def adjust_cash(delta: float, db_path: str = DEFAULT_DB) -> float:
    conn = connect(db_path)
    try:
        conn.execute(
            "UPDATE paper_state SET cash_balance = cash_balance + ? WHERE id = 1",
            (delta,),
        )
        conn.commit()
        r = conn.execute("SELECT cash_balance FROM paper_state WHERE id = 1").fetchone()
        return float(r["cash_balance"])
    finally:
        conn.close()


def reset_paper(db_path: str = DEFAULT_DB) -> None:
    """Reset the mock wallet: restore cash and clear positions/entries/holdings."""
    conn = connect(db_path)
    try:
        conn.execute("DELETE FROM entries")
        conn.execute("DELETE FROM paper_positions")
        conn.execute("DELETE FROM wallet_holdings")
        conn.execute(
            "UPDATE paper_state SET cash_balance = starting_balance WHERE id = 1"
        )
        # Re-baseline wallets so they don't retro-copy history after a reset.
        conn.execute("UPDATE wallets SET cursor_ts = baseline_ts")
        conn.commit()
    finally:
        conn.close()
