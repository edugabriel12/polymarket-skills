#!/usr/bin/env python3
"""The unified "entrada" — the data contract pushed from the wallet-dashboard (the
brain) to Polymarket Sports (the storefront).

Both sources produce the same shape:
  - the statistical MODEL (hourly recalc)  → unit 1.0 (Alta), pregame ⇒ PRÉ-LIVE
  - a WATCHED WALLET (the copy-trade watcher) → unit from its confidence tier

Sports renders the entry by category with its Unidade Sugerida and a LIVE/PRÉ-LIVE
flag, fires Telegram, and (on settlement) folds it into the combined results. It
never sees or shows which source produced the entry — there is no model reference
on the Sports side. `source` is kept only for the wallet-dashboard's own separated
results and is NOT required by Sports.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

LIVE = "LIVE"
PRELIVE = "PRÉ-LIVE"
STATUSES = ("OPEN", "WON", "LOST", "VOID")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_key(*parts) -> str:
    """Stable idempotency key from its components (dedup on the Sports side)."""
    raw = "|".join(str(p).strip().lower() for p in parts if p is not None)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def live_flag(game_start_iso: str | None, now: datetime | None = None) -> str:
    """PRÉ-LIVE if the event hasn't started (or start unknown), else LIVE."""
    if not game_start_iso:
        return PRELIVE
    try:
        start = datetime.fromisoformat(str(game_start_iso).replace("Z", "+00:00"))
    except ValueError:
        return PRELIVE
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return LIVE if start <= now else PRELIVE


def make_entry(*, key: str, event: str, category: str, subcategory: str, side: str,
               odds: float, entry_price: float, unit: float, confidence: str | None,
               live: str, market_url: str | None = None, game_start: str | None = None,
               source: str = "", status: str = "OPEN", pnl: float | None = None,
               detected_at: str | None = None) -> dict:
    """Build a normalized entry. `source` ('model'/wallet name) is wallet-dashboard-internal."""
    return {
        "key": key,
        "event": event,
        "category": category,
        "subcategory": subcategory,
        "side": side,
        "odds": round(float(odds or 0), 4),
        "entry_price": round(float(entry_price or 0), 4),
        "unit": float(unit),
        "confidence": confidence,
        "live": live if live in (LIVE, PRELIVE) else PRELIVE,
        "market_url": market_url,
        "game_start": game_start,
        "source": source,
        "status": status if status in STATUSES else "OPEN",
        "pnl": None if pnl is None else round(float(pnl), 2),
        "detected_at": detected_at or _now_iso(),
    }


def public_view(entry: dict) -> dict:
    """The payload actually sent to Sports — strips `source` so no model/wallet origin leaks."""
    return {k: v for k, v in entry.items() if k != "source"}
