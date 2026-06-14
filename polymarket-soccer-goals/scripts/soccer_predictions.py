#!/usr/bin/env python3
"""Predictions store for the soccer skill (total-goals + BTTS).

Mirrors the MLB predictions store but supports two market types — `TOTAL`
(side OVER/UNDER vs a goals line) and `BTTS` (side YES/NO) — so it cannot reuse
the MLB table's OVER/UNDER-only CHECK. Dedicated SQLite DB
(default ~/.polymarket-soccer/predictions.db). Pure stdlib (sqlite3 + json).

Status: PENDENTE (default) -> ACERTO / ERRO after settlement (ANULADO on a
total-goals push). Stores the full math audit in stats_log (JSON) and the direct
Polymarket market link.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = str(Path.home() / ".polymarket-soccer" / "predictions.db")
VALID_STATUS = ("PENDENTE", "ACERTO", "ERRO", "ANULADO")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    game_slug TEXT NOT NULL,
    game_date TEXT,
    league TEXT,
    market TEXT NOT NULL CHECK(market IN ('TOTAL','BTTS')),
    market_question TEXT,
    condition_id TEXT,
    token_id TEXT,
    line REAL,
    side TEXT NOT NULL CHECK(side IN ('OVER','UNDER','YES','NO')),
    entry_price REAL,
    decimal_odds REAL,
    model_prob REAL,
    edge REAL,
    lam_home REAL,
    lam_away REAL,
    rho REAL,
    confidence REAL,
    size_pct REAL,
    size_usd REAL,
    kelly_fraction REAL,
    used_external INTEGER,
    fee_rate REAL,
    strategy TEXT,
    market_url TEXT,
    stats_log TEXT,
    status TEXT NOT NULL DEFAULT 'PENDENTE'
           CHECK(status IN ('PENDENTE','ACERTO','ERRO','ANULADO')),
    actual_total REAL,
    actual_btts INTEGER,
    settled_at TEXT,
    UNIQUE(game_slug, market, line, side)
);
CREATE INDEX IF NOT EXISTS idx_soccer_status ON predictions(status);
CREATE INDEX IF NOT EXISTS idx_soccer_date ON predictions(game_date);
"""

_FIELDS = (
    "game_slug", "game_date", "league", "market", "market_question", "condition_id",
    "token_id", "line", "side", "entry_price", "decimal_odds", "model_prob", "edge",
    "lam_home", "lam_away", "rho", "confidence", "size_pct", "size_usd",
    "kelly_fraction", "used_external", "fee_rate", "strategy", "market_url", "stats_log",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: str = DEFAULT_DB) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    return con


def record_prediction(pred: dict, db_path: str = DEFAULT_DB) -> int:
    row = {k: pred.get(k) for k in _FIELDS}
    row["side"] = (row["side"] or "").upper()
    row["market"] = (row["market"] or "").upper()
    if "stats" in pred and row.get("stats_log") is None:
        row["stats_log"] = json.dumps(pred["stats"], ensure_ascii=False, default=str)
    if row.get("used_external") is not None:
        row["used_external"] = int(bool(row["used_external"]))
    if row.get("line") is None:
        row["line"] = -1.0  # BTTS has no line; sentinel keeps the UNIQUE key stable

    now = _now()
    cols = ", ".join(_FIELDS)
    ph = ", ".join(f":{f}" for f in _FIELDS)
    upd = ", ".join(f"{f}=excluded.{f}" for f in _FIELDS)
    sql = (
        f"INSERT INTO predictions (created_at, updated_at, status, {cols}) "
        f"VALUES (:created_at, :updated_at, 'PENDENTE', {ph}) "
        f"ON CONFLICT(game_slug, market, line, side) DO UPDATE SET "
        f"updated_at=excluded.updated_at, {upd} WHERE predictions.status='PENDENTE'"
    )
    con = connect(db_path)
    try:
        with con:
            con.execute(sql, dict(row, created_at=now, updated_at=now))
            cur = con.execute(
                "SELECT id FROM predictions WHERE game_slug=:g AND market=:m "
                "AND line=:l AND side=:s",
                {"g": row["game_slug"], "m": row["market"], "l": row["line"], "s": row["side"]})
            return cur.fetchone()["id"]
    finally:
        con.close()


def compute_status(market: str, side: str, line, actual_total=None, actual_btts=None) -> str:
    """ACERTO/ERRO/ANULADO from the settled result."""
    market = (market or "").upper()
    side = (side or "").upper()
    if market == "BTTS":
        if actual_btts is None:
            return "PENDENTE"
        yes = bool(actual_btts)
        won = (side == "YES" and yes) or (side == "NO" and not yes)
        return "ACERTO" if won else "ERRO"
    # TOTAL
    if actual_total is None:
        return "PENDENTE"
    if abs(actual_total - line) < 1e-9:
        return "ANULADO"  # push (integer line)
    over_won = actual_total > line
    if side == "OVER":
        return "ACERTO" if over_won else "ERRO"
    return "ERRO" if over_won else "ACERTO"


def settle_game(game_slug: str, db_path: str = DEFAULT_DB, *,
                actual_total=None, actual_btts=None) -> list[dict]:
    """Settle all pending predictions for a game from its final goals/BTTS."""
    if actual_btts is None and actual_total is not None:
        actual_btts = None  # caller may pass goals only; BTTS rows stay pending
    con = connect(db_path)
    try:
        rows = con.execute("SELECT id, market, side, line FROM predictions "
                           "WHERE game_slug=? AND status='PENDENTE'", (game_slug,)).fetchall()
        out = []
        with con:
            for r in rows:
                st = compute_status(r["market"], r["side"], r["line"],
                                    actual_total=actual_total, actual_btts=actual_btts)
                if st == "PENDENTE":
                    continue
                con.execute("UPDATE predictions SET status=?, actual_total=?, actual_btts=?, "
                            "settled_at=?, updated_at=? WHERE id=?",
                            (st, actual_total,
                             None if actual_btts is None else int(bool(actual_btts)),
                             _now(), _now(), r["id"]))
                out.append({"id": r["id"], "market": r["market"], "side": r["side"], "status": st})
        return out
    finally:
        con.close()


def get_predictions(db_path: str = DEFAULT_DB, status: str | None = None,
                    game_date: str | None = None) -> list[dict]:
    con = connect(db_path)
    try:
        q, params, clauses = "SELECT * FROM predictions", [], []
        if status:
            clauses.append("status=?"); params.append(status.upper())
        if game_date:
            clauses.append("game_date=?"); params.append(game_date)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY created_at DESC, id DESC"
        return [dict(r) for r in con.execute(q, params).fetchall()]
    finally:
        con.close()


def summary(db_path: str = DEFAULT_DB) -> dict:
    con = connect(db_path)
    try:
        counts = {s: 0 for s in VALID_STATUS}
        for r in con.execute("SELECT status, COUNT(*) c FROM predictions GROUP BY status"):
            counts[r["status"]] = r["c"]
        settled = counts["ACERTO"] + counts["ERRO"]
        return {
            "total": sum(counts.values()), "pendente": counts["PENDENTE"],
            "acerto": counts["ACERTO"], "erro": counts["ERRO"], "anulado": counts["ANULADO"],
            "settled": settled,
            "win_rate": round(counts["ACERTO"] / settled, 4) if settled else None,
        }
    finally:
        con.close()


def compute_pnl(row: dict) -> float:
    status, size, price = row.get("status"), float(row.get("size_usd") or 0), float(row.get("entry_price") or 0)
    if status == "ACERTO" and price > 0:
        return size * (1.0 / price - 1.0)
    if status == "ERRO":
        return -size
    return 0.0
