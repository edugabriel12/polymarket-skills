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
import re
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
    market_url      TEXT,                       -- direct Polymarket market link
    stats_log       TEXT,                       -- JSON: full math/stats audit
    status          TEXT NOT NULL DEFAULT 'PENDENTE'
                    CHECK(status IN ('PENDENTE','ACERTO','ERRO','ANULADO')),
    actual_total    REAL,
    settled_at      TEXT,
    UNIQUE(game_slug, line, side)
);
CREATE INDEX IF NOT EXISTS idx_predictions_status ON predictions(status);
CREATE INDEX IF NOT EXISTS idx_predictions_date ON predictions(game_date);

CREATE TABLE IF NOT EXISTS model_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    game_slug       TEXT NOT NULL,
    game_date       TEXT,
    league          TEXT,
    market          TEXT,
    line            REAL,
    ref_side        TEXT,        -- calibration reference side (OVER)
    ref_prob        REAL,        -- model P(ref_side)
    ref_price       REAL,        -- market price of ref_side
    pick_side       TEXT,        -- model's preferred side (may differ)
    pick_edge       REAL,
    used_external   INTEGER,
    model_params    TEXT,        -- JSON (mu / market_mu / variance)
    bet             INTEGER NOT NULL DEFAULT 0,   -- 1 = recorded suggestion, 0 = not bet
    skip_reason     TEXT,
    market_url      TEXT,
    ref_token       TEXT,        -- token of the reference side (to snapshot the close)
    close_price     REAL,        -- reference-side price near close (for CLV)
    actual_total    REAL,
    actual_btts     INTEGER,     -- unused for MLB; kept for a shared settle path
    ref_outcome     INTEGER,     -- 1 if ref_side won, 0 if lost (filled at settlement)
    status          TEXT NOT NULL DEFAULT 'PENDENTE',
    UNIQUE(game_slug, market, line)
);
CREATE INDEX IF NOT EXISTS idx_mlog_date ON model_log(game_date);
"""

_MODEL_LOG_FIELDS = (
    "game_slug", "game_date", "league", "market", "line", "ref_side", "ref_prob",
    "ref_price", "ref_token", "pick_side", "pick_edge", "used_external", "model_params",
    "bet", "skip_reason", "market_url",
)
_MODEL_LOG_ADDED = {"ref_token": "TEXT", "close_price": "REAL", "actual_btts": "INTEGER"}

# Columns set on insert (created_at/updated_at/status handled separately).
_FIELDS = (
    "game_slug", "game_date", "market_question", "condition_id", "token_id",
    "line", "side", "entry_price", "decimal_odds", "model_prob", "edge", "mu",
    "variance", "dispersion", "park_factor", "confidence", "size_pct",
    "size_usd", "kelly_fraction", "used_external", "fee_rate", "strategy",
    "market_url", "stats_log",
)

# Columns added after the initial release; created on older DBs via ensure_columns.
_ADDED_COLUMNS = {"market_url": "TEXT"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: str = DEFAULT_DB) -> sqlite3.Connection:
    """Open (and initialize) the predictions DB, creating the directory."""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    _ensure_columns(con)
    return con


def _ensure_columns(con: sqlite3.Connection) -> None:
    """Idempotent migration: add columns introduced after the first release."""
    existing = {r["name"] for r in con.execute("PRAGMA table_info(predictions)")}
    for col, decl in _ADDED_COLUMNS.items():
        if col not in existing:
            con.execute(f"ALTER TABLE predictions ADD COLUMN {col} {decl}")
    mlog = {r["name"] for r in con.execute("PRAGMA table_info(model_log)")}
    for col, decl in _MODEL_LOG_ADDED.items():
        if mlog and col not in mlog:
            con.execute(f"ALTER TABLE model_log ADD COLUMN {col} {decl}")
    con.commit()


_MLOG_SUFFIX_RE = re.compile(r"-(?:total-\d{1,2}(?:pt5)?|btts|both-teams-to-score|gg)$")


def model_log_base(slug: str) -> str:
    return _MLOG_SUFFIX_RE.sub("", slug or "")


def _ref_outcome(market, line, actual_total, actual_btts):
    if (market or "").upper() == "BTTS":
        return None if actual_btts is None else (1 if actual_btts else 0)
    if actual_total is None or line is None:
        return None
    if abs(float(actual_total) - float(line)) < 1e-9:
        return None  # push
    return 1 if float(actual_total) > float(line) else 0


def settle_model_log(db_path, finals_total: dict, finals_btts: dict | None = None) -> int:
    """Fill ref_outcome for shadow rows from {base_game_slug: outcome}. Returns updated."""
    finals_btts = finals_btts or {}
    con = connect(db_path)
    updated = 0
    try:
        with con:
            for r in con.execute("SELECT * FROM model_log WHERE ref_outcome IS NULL"):
                d = dict(r)
                key = model_log_base(d.get("game_slug", ""))
                at, ab = finals_total.get(key), finals_btts.get(key)
                if at is None and ab is None:
                    continue
                out = _ref_outcome(d.get("market"), d.get("line"), at, ab)
                if out is None:
                    continue
                con.execute(
                    "UPDATE model_log SET ref_outcome=?, actual_total=?, actual_btts=?, "
                    "status='SETTLED' WHERE id=?",
                    (out, at, (1 if ab else 0) if ab is not None else None, d["id"]))
                updated += 1
    finally:
        con.close()
    return updated


def set_close_price(db_path, row_id: int, close_price: float) -> None:
    con = connect(db_path)
    try:
        with con:
            con.execute("UPDATE model_log SET close_price=? WHERE id=?", (close_price, row_id))
    finally:
        con.close()


def record_model_log(entry: dict, db_path: str = DEFAULT_DB) -> None:
    """Shadow-log EVERY modeled game (bet or not) for calibration analysis.

    Upserts one row per (game_slug, market, line) while PENDENTE so re-runs refresh
    the snapshot. `bet=0` marks a game the model did NOT bet (skip_reason explains).
    """
    row = {k: entry.get(k) for k in _MODEL_LOG_FIELDS}
    if isinstance(row.get("model_params"), (dict, list)):
        row["model_params"] = json.dumps(row["model_params"], ensure_ascii=False, default=str)
    if row.get("used_external") is not None:
        row["used_external"] = int(bool(row["used_external"]))
    row["bet"] = int(bool(row.get("bet")))
    if row.get("line") is None:
        row["line"] = -1.0
    now = _now()
    cols = ", ".join(_MODEL_LOG_FIELDS)
    ph = ", ".join(f":{f}" for f in _MODEL_LOG_FIELDS)
    upd = ", ".join(f"{f}=excluded.{f}" for f in _MODEL_LOG_FIELDS)
    sql = (
        f"INSERT INTO model_log (created_at, updated_at, status, {cols}) "
        f"VALUES (:created_at, :updated_at, 'PENDENTE', {ph}) "
        f"ON CONFLICT(game_slug, market, line) DO UPDATE SET "
        f"updated_at=excluded.updated_at, {upd} WHERE model_log.status='PENDENTE'"
    )
    con = connect(db_path)
    try:
        with con:
            con.execute(sql, dict(row, created_at=now, updated_at=now))
    finally:
        con.close()


def get_model_log(db_path: str = DEFAULT_DB, *, bet: int | None = None,
                  game_date: str | None = None) -> list[dict]:
    con = connect(db_path)
    try:
        q, args, conds = "SELECT * FROM model_log", [], []
        if bet is not None:
            conds.append("bet=?"); args.append(int(bet))
        if game_date:
            conds.append("game_date=?"); args.append(game_date)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY id"
        return [dict(r) for r in con.execute(q, args).fetchall()]
    finally:
        con.close()


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
