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
import re
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

CREATE TABLE IF NOT EXISTS model_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    game_slug TEXT NOT NULL,
    game_date TEXT,
    league TEXT,
    market TEXT,
    line REAL,
    ref_side TEXT,        -- calibration reference side (OVER / YES)
    ref_prob REAL,        -- model P(ref_side)
    ref_price REAL,       -- market price of ref_side
    pick_side TEXT,       -- model's preferred side (may differ)
    pick_edge REAL,
    used_external INTEGER,
    model_params TEXT,    -- JSON (lam_home/lam_away/rho)
    bet INTEGER NOT NULL DEFAULT 0,   -- 1 = became a recorded suggestion, 0 = not bet
    skip_reason TEXT,
    market_url TEXT,
    ref_token TEXT,       -- token of the reference side (to snapshot the close)
    close_price REAL,     -- reference-side price near close (for CLV)
    actual_total REAL,
    actual_btts INTEGER,
    ref_outcome INTEGER,  -- 1 if ref_side won, 0 if lost (filled at settlement)
    status TEXT NOT NULL DEFAULT 'PENDENTE',
    UNIQUE(game_slug, market, line)
);
CREATE INDEX IF NOT EXISTS idx_soccer_mlog_date ON model_log(game_date);
"""

_MODEL_LOG_FIELDS = (
    "game_slug", "game_date", "league", "market", "line", "ref_side", "ref_prob",
    "ref_price", "ref_token", "pick_side", "pick_edge", "used_external", "model_params",
    "bet", "skip_reason", "market_url",
)
_MODEL_LOG_ADDED = {"ref_token": "TEXT", "close_price": "REAL"}
_MLOG_SUFFIX_RE = re.compile(r"-(?:total-\d{1,2}(?:pt5)?|btts|both-teams-to-score|gg)$")

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
    mlog = {r["name"] for r in con.execute("PRAGMA table_info(model_log)")}
    for col, decl in _MODEL_LOG_ADDED.items():
        if mlog and col not in mlog:
            con.execute(f"ALTER TABLE model_log ADD COLUMN {col} {decl}")
    con.commit()
    return con


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


def supersede_pending(db_path: str, game_slug: str, keep_ids) -> int:
    """Void (ANULADO) still-PENDENTE predictions for a game except `keep_ids`.

    When a re-run records a different best line for a game (e.g. the total line
    moved from 2.5 to 3.5), the stale PENDENTE entry from the earlier run is
    neutralized — stake returned, excluded from win rates — so one game never
    carries two open paper positions. `keep_ids` spans both markets (TOTAL+BTTS),
    so a stale total line never voids the game's BTTS bet. Settled rows are never
    touched. Returns the number voided.
    """
    keep = [int(i) for i in keep_ids if i is not None]
    placeholders = ",".join("?" for _ in keep) or "NULL"
    con = connect(db_path)
    try:
        with con:
            cur = con.execute(
                f"UPDATE predictions SET status='ANULADO', settled_at=? "
                f"WHERE game_slug=? AND status='PENDENTE' AND id NOT IN ({placeholders})",
                [_now(), game_slug, *keep])
            return cur.rowcount
    finally:
        con.close()


def record_model_log(entry: dict, db_path: str = DEFAULT_DB) -> None:
    """Shadow-log EVERY modeled market (bet or not) for calibration analysis.

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
        row["line"] = -1.0  # BTTS sentinel, keeps the UNIQUE key stable
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
        q, args = "SELECT * FROM model_log", []
        conds = []
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


# ---------------------------------------------------------------------------
# Performance analytics (daily/weekly/monthly) for the dashboard
# ---------------------------------------------------------------------------

from datetime import date as _date, timedelta as _td  # noqa: E402


def _window_bounds(window: str, today: "_date"):
    if window == "daily":
        return today, today
    if window == "weekly":
        return today - _td(days=today.weekday()), today
    return today.replace(day=1), today


def _in_window(gd, start, end) -> bool:
    if not gd:
        return False
    try:
        d = _date.fromisoformat(gd)
    except ValueError:
        return False
    return start <= d <= end


def _aggregate(rows: list[dict]) -> dict:
    counts = {"acerto": 0, "erro": 0, "pendente": 0, "anulado": 0}
    pnl = invested = 0.0
    over = {"a": 0, "n": 0}
    under = {"a": 0, "n": 0}
    for r in rows:
        st, side = r.get("status"), (r.get("side") or "").upper()
        pnl += compute_pnl(r)
        if st == "ACERTO":
            counts["acerto"] += 1
        elif st == "ERRO":
            counts["erro"] += 1
        elif st == "ANULADO":
            counts["anulado"] += 1
        else:
            counts["pendente"] += 1
        if st in ("ACERTO", "ERRO"):
            invested += float(r.get("size_usd") or 0)
            bucket = over if side == "OVER" else under if side == "UNDER" else None
            if bucket is not None:
                bucket["n"] += 1
                if st == "ACERTO":
                    bucket["a"] += 1
    settled = counts["acerto"] + counts["erro"]
    return {
        "counts": counts, "settled": settled, "pnl": round(pnl, 2),
        "invested": round(invested, 2),
        "roi": round(pnl / invested, 4) if invested > 0 else None,
        "win_rate": round(counts["acerto"] / settled, 4) if settled else None,
        "win_rate_over": round(over["a"] / over["n"], 4) if over["n"] else None,
        "win_rate_under": round(under["a"] / under["n"], 4) if under["n"] else None,
    }


def performance(db_path: str = DEFAULT_DB, today=None) -> dict:
    today = today or datetime.now(timezone.utc).date()
    rows = get_predictions(db_path)
    out = {}
    for w in ("daily", "weekly", "monthly"):
        start, end = _window_bounds(w, today)
        block = _aggregate([r for r in rows if _in_window(r.get("game_date"), start, end)])
        block.update(window=w, start=start.isoformat(), end=end.isoformat())
        out[w] = block
    out["generated_at"] = datetime.now(timezone.utc).isoformat()
    return out


def pnl_by_day(db_path: str = DEFAULT_DB, days: int = 30) -> list[dict]:
    by_day: dict[str, float] = {}
    for r in get_predictions(db_path):
        gd = r.get("game_date")
        if gd:
            by_day[gd] = by_day.get(gd, 0.0) + compute_pnl(r)
    series = [{"date": d, "pnl": round(v, 2)} for d, v in sorted(by_day.items())]
    return series[-days:]


def seed(db_path: str, reset: bool = False) -> int:
    """Seed demo soccer predictions (TOTAL + BTTS, mixed statuses) for the UI."""
    if reset:
        con = connect(db_path)
        with con:
            con.execute("DELETE FROM predictions")
        con.close()
    today = datetime.now(timezone.utc).date()
    games = [("epl", "ars", "che"), ("laliga", "rma", "bar"), ("seriea", "int", "juv"),
             ("bundesliga", "bay", "dor"), ("ligue1", "psg", "mar"), ("epl", "liv", "mci")]
    plan = [
        (3, "TOTAL", "OVER", 2.5, 0.52, "ACERTO"), (3, "BTTS", "YES", None, 0.55, "ACERTO"),
        (2, "TOTAL", "UNDER", 3.5, 0.50, "ERRO"), (2, "BTTS", "NO", None, 0.57, "ACERTO"),
        (1, "TOTAL", "OVER", 2.5, 0.48, "ACERTO"), (1, "BTTS", "YES", None, 0.53, "ERRO"),
        (0, "TOTAL", "OVER", 2.5, 0.50, "PENDENTE"), (0, "BTTS", "YES", None, 0.56, "PENDENTE"),
    ]
    n = 0
    for i, (days_ago, market, side, line, price, status) in enumerate(plan):
        league, home, away = games[i % len(games)]
        gd = (today - _td(days=days_ago)).isoformat()
        suffix = f"total-{str(line).replace('.', 'pt')}" if market == "TOTAL" else "btts"
        slug = f"{league}-{home}-{away}-{gd}-{suffix}"
        lam_h, lam_a = 1.6, 1.2
        rid = record_prediction({
            "game_slug": slug, "game_date": gd, "league": league, "market": market,
            "market_question": f"{home} vs {away}: {market}", "condition_id": "0x" + slug,
            "token_id": slug + "-" + side.lower(), "line": line, "side": side,
            "entry_price": price, "decimal_odds": round(1 / price, 3), "model_prob": 0.6,
            "edge": round(0.6 - price, 3), "lam_home": lam_h, "lam_away": lam_a, "rho": -0.10,
            "confidence": 0.6, "size_pct": 0.01, "size_usd": 120.0, "kelly_fraction": 0.18,
            "used_external": True, "fee_rate": 0.0, "strategy": "soccer-goals-dc",
            "market_url": f"https://polymarket.com/event/{slug}",
            "stats": {"model": "dixon_coles", "market": market, "lam_home": lam_h,
                      "lam_away": lam_a, "rho": -0.10, "line": line, "model_prob": 0.6,
                      "entry_price": price, "edge_after_fee": round(0.6 - price, 3),
                      "used_external": True, "confidence": 0.6},
        }, db_path)
        if status in ("ACERTO", "ERRO", "ANULADO"):
            if market == "BTTS":
                yes = (status == "ACERTO") == (side == "YES")
                settle_game(slug, db_path, actual_btts=yes)
            else:
                actual = (line + 1 if side == "OVER" else line - 1) if status == "ACERTO" else \
                         (line - 1 if side == "OVER" else line + 1)
                settle_game(slug, db_path, actual_total=actual)
        n += 1
    return n
