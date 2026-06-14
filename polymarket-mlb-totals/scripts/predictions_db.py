#!/usr/bin/env python3
"""Persistent store for MLB total-runs PREDICTIONS (for later analysis).

Each row records a prediction the model made, the full statistical/mathematical
audit log behind it (`stats_log`, JSON), and the resulting status:
  - PENDENTE : the game/bet has not settled yet (default)
  - ACERTO   : the prediction was correct after settlement
  - ERRO     : the prediction was wrong after settlement
  - ANULADO  : push/void (only possible on an integer line where total == line)

This is a dedicated SQLite DB for this skill (default
~/.polymarket-mlb-totals/predictions.db) — it does NOT touch the paper trader's
portfolio DB. Pure stdlib (sqlite3 + json).
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = str(Path.home() / ".polymarket-mlb-totals" / "predictions.db")

VALID_STATUS = ("PENDENTE", "ACERTO", "ERRO", "ANULADO")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    game_slug       TEXT NOT NULL,
    game_date       TEXT,
    market_question TEXT,
    condition_id    TEXT,
    token_id        TEXT,
    line            REAL NOT NULL,
    side            TEXT NOT NULL CHECK(side IN ('OVER','UNDER')),
    entry_price     REAL,
    decimal_odds    REAL,
    model_prob      REAL,
    edge            REAL,
    mu              REAL,
    variance        REAL,
    dispersion      REAL,
    park_factor     REAL,
    confidence      REAL,
    size_pct        REAL,
    size_usd        REAL,
    kelly_fraction  REAL,
    used_external   INTEGER,
    fee_rate        REAL,
    strategy        TEXT,
    stats_log       TEXT,                       -- JSON: full math/stats audit
    status          TEXT NOT NULL DEFAULT 'PENDENTE'
                    CHECK(status IN ('PENDENTE','ACERTO','ERRO','ANULADO')),
    actual_total    REAL,
    settled_at      TEXT,
    UNIQUE(game_slug, line, side)
);
CREATE INDEX IF NOT EXISTS idx_predictions_status ON predictions(status);
CREATE INDEX IF NOT EXISTS idx_predictions_date ON predictions(game_date);
"""

# Columns set on insert (created_at/updated_at/status handled separately).
_FIELDS = (
    "game_slug", "game_date", "market_question", "condition_id", "token_id",
    "line", "side", "entry_price", "decimal_odds", "model_prob", "edge", "mu",
    "variance", "dispersion", "park_factor", "confidence", "size_pct",
    "size_usd", "kelly_fraction", "used_external", "fee_rate", "strategy",
    "stats_log",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: str = DEFAULT_DB) -> sqlite3.Connection:
    """Open (and initialize) the predictions DB, creating the directory."""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    return con


def record_prediction(pred: dict, db_path: str = DEFAULT_DB) -> int:
    """Insert (or refresh a still-PENDENTE) prediction; return its row id.

    `pred` may carry a `stats` dict (the statistical/mathematical inputs); it is
    JSON-encoded into `stats_log`. Re-recording the same (game_slug, line, side)
    while PENDENTE updates the snapshot (captures line movement); a settled row
    is never overwritten.
    """
    row = {k: pred.get(k) for k in _FIELDS}
    row["side"] = (row["side"] or "").upper()
    if "stats" in pred and row.get("stats_log") is None:
        row["stats_log"] = json.dumps(pred["stats"], ensure_ascii=False, default=str)
    if row.get("used_external") is not None:
        row["used_external"] = int(bool(row["used_external"]))

    now = _now()
    cols = ", ".join(_FIELDS)
    placeholders = ", ".join(f":{f}" for f in _FIELDS)
    update_cols = ", ".join(f"{f}=excluded.{f}" for f in _FIELDS)

    sql = (
        f"INSERT INTO predictions (created_at, updated_at, status, {cols}) "
        f"VALUES (:created_at, :updated_at, 'PENDENTE', {placeholders}) "
        f"ON CONFLICT(game_slug, line, side) DO UPDATE SET "
        f"updated_at=excluded.updated_at, {update_cols} "
        f"WHERE predictions.status = 'PENDENTE'"
    )
    params = dict(row, created_at=now, updated_at=now)

    con = connect(db_path)
    try:
        with con:
            con.execute(sql, params)
            cur = con.execute(
                "SELECT id FROM predictions WHERE game_slug=:g AND line=:l AND side=:s",
                {"g": row["game_slug"], "l": row["line"], "s": row["side"]},
            )
            return cur.fetchone()["id"]
    finally:
        con.close()


def compute_status(side: str, line: float, actual_total: float) -> str:
    """ACERTO / ERRO / ANULADO for a settled Over/Under prediction."""
    side = (side or "").upper()
    if abs(actual_total - line) < 1e-9:
        return "ANULADO"  # push (integer line, total == line)
    over_won = actual_total > line
    if side == "OVER":
        return "ACERTO" if over_won else "ERRO"
    return "ERRO" if over_won else "ACERTO"


def settle_prediction(pred_id: int, actual_total: float,
                      db_path: str = DEFAULT_DB) -> str | None:
    """Settle one pending prediction by its actual game total. Returns the status."""
    con = connect(db_path)
    try:
        cur = con.execute(
            "SELECT side, line, status FROM predictions WHERE id=?", (pred_id,))
        rec = cur.fetchone()
        if rec is None or rec["status"] != "PENDENTE":
            return rec["status"] if rec else None
        status = compute_status(rec["side"], rec["line"], actual_total)
        with con:
            con.execute(
                "UPDATE predictions SET status=?, actual_total=?, settled_at=?, "
                "updated_at=? WHERE id=?",
                (status, actual_total, _now(), _now(), pred_id))
        return status
    finally:
        con.close()


def settle_game(game_slug: str, actual_total: float,
                db_path: str = DEFAULT_DB) -> list[dict]:
    """Settle all pending predictions for a game. Returns [{id, side, status}]."""
    con = connect(db_path)
    try:
        cur = con.execute(
            "SELECT id, side, line FROM predictions "
            "WHERE game_slug=? AND status='PENDENTE'", (game_slug,))
        rows = cur.fetchall()
        out = []
        with con:
            for r in rows:
                status = compute_status(r["side"], r["line"], actual_total)
                con.execute(
                    "UPDATE predictions SET status=?, actual_total=?, settled_at=?, "
                    "updated_at=? WHERE id=?",
                    (status, actual_total, _now(), _now(), r["id"]))
                out.append({"id": r["id"], "side": r["side"], "status": status})
        return out
    finally:
        con.close()


def get_predictions(db_path: str = DEFAULT_DB, status: str | None = None,
                    game_date: str | None = None) -> list[dict]:
    """Fetch prediction rows (optionally filtered), newest first."""
    con = connect(db_path)
    try:
        q = "SELECT * FROM predictions"
        clauses, params = [], []
        if status:
            clauses.append("status=?")
            params.append(status.upper())
        if game_date:
            clauses.append("game_date=?")
            params.append(game_date)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY created_at DESC, id DESC"
        return [dict(r) for r in con.execute(q, params).fetchall()]
    finally:
        con.close()


def summary(db_path: str = DEFAULT_DB) -> dict:
    """Aggregate counts + win rate for later calibration analysis."""
    con = connect(db_path)
    try:
        counts = {s: 0 for s in VALID_STATUS}
        for r in con.execute("SELECT status, COUNT(*) c FROM predictions GROUP BY status"):
            counts[r["status"]] = r["c"]
        settled = counts["ACERTO"] + counts["ERRO"]
        win_rate = round(counts["ACERTO"] / settled, 4) if settled else None
        return {
            "total": sum(counts.values()),
            "pendente": counts["PENDENTE"],
            "acerto": counts["ACERTO"],
            "erro": counts["ERRO"],
            "anulado": counts["ANULADO"],
            "settled": settled,
            "win_rate": win_rate,
        }
    finally:
        con.close()
