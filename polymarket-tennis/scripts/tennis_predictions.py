#!/usr/bin/env python3
"""SQLite predictions store for the tennis match-winner skill (pure stdlib).

Mirrors the soccer/MLB stores' API so the shared calibration/CLV tooling applies:
- `predictions`: one recorded moneyline entry per (match, chosen side), PENDENTE ->
  ACERTO/ERRO/ANULADO. Re-recording a still-PENDENTE row refreshes the snapshot
  (captures line movement); settled rows are never overwritten.
- `model_log`: shadow log of EVERY modeled match (bet or not) with the model's
  probability for a reference side (player A) and the eventual outcome — the unbiased
  basis for Brier/log-loss/calibration and CLV (ref_token/close_price).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone

DEFAULT_DB = os.path.expanduser("~/.polymarket-tennis/predictions.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    match_slug      TEXT NOT NULL,
    match_date      TEXT,
    tour            TEXT,
    surface         TEXT,
    market_question TEXT,
    condition_id    TEXT,
    token_id        TEXT,
    side            TEXT NOT NULL,          -- chosen player label
    opponent        TEXT,
    entry_price     REAL,
    decimal_odds    REAL,
    model_prob      REAL,
    edge            REAL,
    elo_side        REAL,
    elo_opp         REAL,
    confidence      REAL,
    size_pct        REAL,
    size_usd        REAL,
    kelly_fraction  REAL,
    used_external   INTEGER,
    fee_rate        REAL,
    strategy        TEXT,
    market_url      TEXT,
    stats_log       TEXT,
    status          TEXT NOT NULL DEFAULT 'PENDENTE'
                    CHECK(status IN ('PENDENTE','ACERTO','ERRO','ANULADO')),
    actual_winner   TEXT,
    settled_at      TEXT,
    UNIQUE(match_slug, side)
);
CREATE INDEX IF NOT EXISTS idx_tennis_status ON predictions(status);
CREATE INDEX IF NOT EXISTS idx_tennis_date ON predictions(match_date);

CREATE TABLE IF NOT EXISTS model_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    match_slug      TEXT NOT NULL,
    match_date      TEXT,
    tour            TEXT,
    surface         TEXT,
    ref_side        TEXT,        -- the reference player (player A)
    ref_prob        REAL,        -- model P(ref_side wins)
    ref_price       REAL,        -- market price of ref_side
    ref_token       TEXT,        -- token of ref_side (to snapshot the close)
    close_price     REAL,        -- ref_side price near close (for CLV)
    pick_side       TEXT,        -- model's preferred side (may differ)
    pick_edge       REAL,
    used_external   INTEGER,
    model_params    TEXT,        -- JSON (elo_a / elo_b / surface)
    bet             INTEGER NOT NULL DEFAULT 0,   -- 1 = recorded, 0 = not bet
    skip_reason     TEXT,
    market_url      TEXT,
    ref_outcome     INTEGER,     -- 1 if ref_side won, 0 if lost (filled at settlement)
    status          TEXT NOT NULL DEFAULT 'PENDENTE',
    UNIQUE(match_slug)
);
CREATE INDEX IF NOT EXISTS idx_tennis_mlog_date ON model_log(match_date);
"""

_FIELDS = (
    "match_slug", "match_date", "tour", "surface", "market_question", "condition_id",
    "token_id", "side", "opponent", "entry_price", "decimal_odds", "model_prob", "edge",
    "elo_side", "elo_opp", "confidence", "size_pct", "size_usd", "kelly_fraction",
    "used_external", "fee_rate", "strategy", "market_url", "stats_log",
)
_MODEL_LOG_FIELDS = (
    "match_slug", "match_date", "tour", "surface", "ref_side", "ref_prob", "ref_price",
    "ref_token", "pick_side", "pick_edge", "used_external", "model_params", "bet",
    "skip_reason", "market_url",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: str = DEFAULT_DB) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    return con


# ---------------------------------------------------------------------------
# Predictions
# ---------------------------------------------------------------------------


def record_prediction(pred: dict, db_path: str = DEFAULT_DB) -> int:
    """Insert (or refresh a still-PENDENTE) moneyline prediction; return its row id."""
    row = {k: pred.get(k) for k in _FIELDS}
    if "stats" in pred and row.get("stats_log") is None:
        row["stats_log"] = json.dumps(pred["stats"], ensure_ascii=False, default=str)
    if row.get("used_external") is not None:
        row["used_external"] = int(bool(row["used_external"]))
    now = _now()
    cols = ", ".join(_FIELDS)
    ph = ", ".join(f":{f}" for f in _FIELDS)
    upd = ", ".join(f"{f}=excluded.{f}" for f in _FIELDS)
    sql = (f"INSERT INTO predictions (created_at, updated_at, status, {cols}) "
           f"VALUES (:created_at, :updated_at, 'PENDENTE', {ph}) "
           f"ON CONFLICT(match_slug, side) DO UPDATE SET updated_at=excluded.updated_at, "
           f"{upd} WHERE predictions.status='PENDENTE'")
    con = connect(db_path)
    try:
        with con:
            con.execute(sql, dict(row, created_at=now, updated_at=now))
            cur = con.execute("SELECT id FROM predictions WHERE match_slug=:m AND side=:s",
                              {"m": row["match_slug"], "s": row["side"]})
            return cur.fetchone()["id"]
    finally:
        con.close()


def get_predictions(db_path: str = DEFAULT_DB, status: str | None = None,
                    match_date: str | None = None) -> list[dict]:
    con = connect(db_path)
    try:
        q, args = "SELECT * FROM predictions", []
        clauses = []
        if status:
            clauses.append("status=?"); args.append(status.upper())
        if match_date:
            clauses.append("match_date=?"); args.append(match_date)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY created_at DESC, id DESC"
        return [dict(r) for r in con.execute(q, args).fetchall()]
    finally:
        con.close()


def compute_status(side: str, opponent: str, actual_winner: str) -> str:
    """ACERTO if the chosen side won, ERRO if the opponent won, ANULADO otherwise
    (walkover / void / unrecognized winner)."""
    w = (actual_winner or "").strip().lower()
    if not w:
        return "ANULADO"
    if w == (side or "").strip().lower():
        return "ACERTO"
    if w == (opponent or "").strip().lower():
        return "ERRO"
    return "ANULADO"


def settle_match(match_slug: str, actual_winner: str, db_path: str = DEFAULT_DB) -> list[dict]:
    """Settle all PENDENTE rows for a match given the winner's label. Returns settled rows."""
    con = connect(db_path)
    out = []
    try:
        with con:
            rows = con.execute(
                "SELECT * FROM predictions WHERE match_slug=? AND status='PENDENTE'",
                (match_slug,)).fetchall()
            for r in rows:
                st = compute_status(r["side"], r["opponent"], actual_winner)
                con.execute("UPDATE predictions SET status=?, actual_winner=?, settled_at=? "
                            "WHERE id=?", (st, actual_winner, _now(), r["id"]))
                d = dict(r); d["status"] = st; d["actual_winner"] = actual_winner
                out.append(d)
    finally:
        con.close()
    return out


def supersede_pending(db_path: str, match_slug: str, keep_ids) -> int:
    """Void (ANULADO) still-PENDENTE rows for a match except keep_ids (re-run cleanup)."""
    keep = [int(i) for i in keep_ids if i is not None]
    ph = ",".join("?" for _ in keep) or "NULL"
    con = connect(db_path)
    try:
        with con:
            cur = con.execute(
                f"UPDATE predictions SET status='ANULADO', settled_at=? "
                f"WHERE match_slug=? AND status='PENDENTE' AND id NOT IN ({ph})",
                [_now(), match_slug, *keep])
            return cur.rowcount
    finally:
        con.close()


def summary(db_path: str = DEFAULT_DB) -> dict:
    con = connect(db_path)
    try:
        counts = {s.lower(): 0 for s in ("PENDENTE", "ACERTO", "ERRO", "ANULADO")}
        for r in con.execute("SELECT status, COUNT(*) n FROM predictions GROUP BY status"):
            counts[r["status"].lower()] = r["n"]
        settled = counts["acerto"] + counts["erro"]
        counts["win_rate"] = (counts["acerto"] / settled) if settled else None
        return counts
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Shadow model_log (calibration / CLV parity with the other skills)
# ---------------------------------------------------------------------------


def model_log_base(slug: str) -> str:
    """Base match slug for model_log keying (tennis slugs carry no market suffix)."""
    return slug or ""


def record_model_log(entry: dict, db_path: str = DEFAULT_DB) -> None:
    row = {k: entry.get(k) for k in _MODEL_LOG_FIELDS}
    if isinstance(row.get("model_params"), (dict, list)):
        row["model_params"] = json.dumps(row["model_params"], ensure_ascii=False, default=str)
    if row.get("used_external") is not None:
        row["used_external"] = int(bool(row["used_external"]))
    now = _now()
    cols = ", ".join(_MODEL_LOG_FIELDS)
    ph = ", ".join(f":{f}" for f in _MODEL_LOG_FIELDS)
    upd = ", ".join(f"{f}=excluded.{f}" for f in _MODEL_LOG_FIELDS)
    sql = (f"INSERT INTO model_log (created_at, updated_at, status, {cols}) "
           f"VALUES (:created_at, :updated_at, 'PENDENTE', {ph}) "
           f"ON CONFLICT(match_slug) DO UPDATE SET updated_at=excluded.updated_at, "
           f"{upd} WHERE model_log.status='PENDENTE'")
    con = connect(db_path)
    try:
        with con:
            con.execute(sql, dict(row, created_at=now, updated_at=now))
    finally:
        con.close()


def get_model_log(db_path: str = DEFAULT_DB) -> list[dict]:
    con = connect(db_path)
    try:
        return [dict(r) for r in con.execute("SELECT * FROM model_log").fetchall()]
    finally:
        con.close()


def settle_model_log(db_path: str, winners: dict) -> int:
    """Fill ref_outcome from {base_match_slug: winner_label}. Returns rows updated."""
    con = connect(db_path)
    updated = 0
    try:
        with con:
            for r in con.execute("SELECT * FROM model_log WHERE ref_outcome IS NULL"):
                d = dict(r)
                w = winners.get(model_log_base(d.get("match_slug", "")))
                if not w:
                    continue
                ref = (d.get("ref_side") or "").strip().lower()
                wl = w.strip().lower()
                outcome = 1 if wl == ref else 0
                con.execute("UPDATE model_log SET ref_outcome=?, status='SETTLED' WHERE id=?",
                            (outcome, d["id"]))
                updated += 1
        return updated
    finally:
        con.close()


def set_close_price(db_path: str, row_id: int, close_price: float) -> None:
    con = connect(db_path)
    try:
        with con:
            con.execute("UPDATE model_log SET close_price=? WHERE id=?", (close_price, row_id))
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Performance analytics (ROI / P&L) — same Polymarket $1 binary payout as the others
# ---------------------------------------------------------------------------


def compute_pnl(row: dict) -> float:
    """Realized P&L (USD) for one moneyline row. ACERTO -> size*(1/price-1); ERRO -> -size."""
    status = row.get("status")
    size = float(row.get("size_usd") or 0.0)
    price = float(row.get("entry_price") or 0.0)
    if status == "ACERTO" and price > 0:
        return size * (1.0 / price - 1.0)
    if status == "ERRO":
        return -size
    return 0.0


def _window_bounds(window, today):
    from datetime import timedelta
    if window == "daily":
        return today, today
    if window == "weekly":
        return today - timedelta(days=today.weekday()), today
    return today.replace(day=1), today


def performance(db_path: str = DEFAULT_DB, today=None) -> dict:
    """Daily/weekly/monthly blocks. No Over/Under split for moneyline (those rates are None)."""
    from datetime import date as _date
    today = today or datetime.now(timezone.utc).date()
    rows = get_predictions(db_path)
    out = {}
    for window in ("daily", "weekly", "monthly"):
        start, end = _window_bounds(window, today)
        counts = {"acerto": 0, "erro": 0, "pendente": 0, "anulado": 0}
        pnl = invested = 0.0
        for r in rows:
            try:
                d = _date.fromisoformat(r.get("match_date") or "")
            except ValueError:
                continue
            if not (start <= d <= end):
                continue
            st = r.get("status")
            counts[st.lower()] = counts.get(st.lower(), 0) + 1
            pnl += compute_pnl(r)
            if st in ("ACERTO", "ERRO"):
                invested += float(r.get("size_usd") or 0.0)
        settled = counts["acerto"] + counts["erro"]
        out[window] = {
            "window": window, "start": start.isoformat(), "end": end.isoformat(),
            "counts": counts, "settled": settled, "pnl": round(pnl, 2),
            "invested": round(invested, 2),
            "roi": round(pnl / invested, 4) if invested > 0 else None,
            "win_rate": round(counts["acerto"] / settled, 4) if settled else None,
            "win_rate_over": None, "win_rate_under": None,
        }
    out["generated_at"] = datetime.now(timezone.utc).isoformat()
    return out


def pnl_by_day(db_path: str = DEFAULT_DB, days: int = 30) -> list[dict]:
    by_day: dict[str, float] = {}
    for r in get_predictions(db_path):
        d = r.get("match_date")
        if d:
            by_day[d] = by_day.get(d, 0.0) + compute_pnl(r)
    series = [{"date": d, "pnl": round(v, 2)} for d, v in sorted(by_day.items())]
    return series[-days:]
