#!/usr/bin/env python3
"""Performance analytics over recorded predictions (ROI, P&L, win rates).

Pure stdlib. Reads the predictions table (predictions_db.py) and aggregates by
period (daily / weekly / monthly) for the results dashboard. P&L assumes the
Polymarket binary payout of $1 per share:
  shares = size_usd / entry_price
  ACERTO -> pnl = shares - size_usd = size_usd * (1/entry_price - 1)
  ERRO   -> pnl = -size_usd
  PENDENTE / ANULADO (push, stake returned) -> pnl = 0
ROI and win rates use only SETTLED bets (ACERTO + ERRO).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import predictions_db as pdb


def compute_pnl(row: dict) -> float:
    """Realized P&L (USD) for one prediction row."""
    status = row.get("status")
    size = float(row.get("size_usd") or 0.0)
    price = float(row.get("entry_price") or 0.0)
    if status == "ACERTO" and price > 0:
        return size * (1.0 / price - 1.0)
    if status == "ERRO":
        return -size
    return 0.0  # PENDENTE / ANULADO


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _window_bounds(window: str, today: date) -> tuple[date, date]:
    """Inclusive (start, end) date bounds for daily/weekly/monthly."""
    if window == "daily":
        return today, today
    if window == "weekly":
        start = today - timedelta(days=today.weekday())  # Monday of this ISO week
        return start, today
    # monthly: first of the current calendar month
    return today.replace(day=1), today


def _in_window(game_date: str | None, start: date, end: date) -> bool:
    if not game_date:
        return False
    try:
        d = date.fromisoformat(game_date)
    except ValueError:
        return False
    return start <= d <= end


def _aggregate(rows: list[dict]) -> dict:
    """Aggregate a set of prediction rows into the metrics block."""
    counts = {"acerto": 0, "erro": 0, "pendente": 0, "anulado": 0}
    pnl = 0.0
    invested = 0.0
    over = {"acerto": 0, "settled": 0}
    under = {"acerto": 0, "settled": 0}

    for r in rows:
        status = r.get("status")
        side = (r.get("side") or "").upper()
        pnl += compute_pnl(r)
        if status == "ACERTO":
            counts["acerto"] += 1
        elif status == "ERRO":
            counts["erro"] += 1
        elif status == "ANULADO":
            counts["anulado"] += 1
        else:
            counts["pendente"] += 1

        if status in ("ACERTO", "ERRO"):
            invested += float(r.get("size_usd") or 0.0)
            bucket = over if side == "OVER" else under if side == "UNDER" else None
            if bucket is not None:
                bucket["settled"] += 1
                if status == "ACERTO":
                    bucket["acerto"] += 1

    settled = counts["acerto"] + counts["erro"]
    return {
        "counts": counts,
        "settled": settled,
        "pnl": round(pnl, 2),
        "invested": round(invested, 2),
        "roi": round(pnl / invested, 4) if invested > 0 else None,
        "win_rate": round(counts["acerto"] / settled, 4) if settled else None,
        "win_rate_over": (round(over["acerto"] / over["settled"], 4)
                          if over["settled"] else None),
        "win_rate_under": (round(under["acerto"] / under["settled"], 4)
                           if under["settled"] else None),
    }


def performance(db_path: str = pdb.DEFAULT_DB, today: date | None = None) -> dict:
    """Daily / weekly / monthly performance blocks from all predictions."""
    today = today or _today()
    rows = pdb.get_predictions(db_path)
    out = {}
    for window in ("daily", "weekly", "monthly"):
        start, end = _window_bounds(window, today)
        subset = [r for r in rows if _in_window(r.get("game_date"), start, end)]
        block = _aggregate(subset)
        block["window"] = window
        block["start"] = start.isoformat()
        block["end"] = end.isoformat()
        out[window] = block
    out["generated_at"] = datetime.now(timezone.utc).isoformat()
    return out


def pnl_by_day(db_path: str = pdb.DEFAULT_DB, days: int = 30) -> list[dict]:
    """Daily P&L series (last `days` calendar days) for charting."""
    rows = pdb.get_predictions(db_path)
    by_day: dict[str, float] = {}
    for r in rows:
        gd = r.get("game_date")
        if gd:
            by_day[gd] = by_day.get(gd, 0.0) + compute_pnl(r)
    series = [{"date": d, "pnl": round(v, 2)} for d, v in sorted(by_day.items())]
    return series[-days:]
