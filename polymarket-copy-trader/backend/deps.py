"""Wiring to existing in-repo helpers — reused, never reimplemented.

Every symbol re-exported here is a pure function or a thin HTTP wrapper: importing
the source modules has no filesystem/DB side effects (verified: paper_engine only
touches its DB lazily inside _get_db; weather_edge_helpers is documented as
side-effect free). This keeps the copy-trader a thin orchestration layer over
code that already ships in the other skills.

Reused:
  - compute_max_size_for_slippage  (polymarket-analyzer)     — slippage sizer/gate
  - APIClient / fetch_trades / fetch_positions / sanitize_text / to_float / _end_in_past
                                     (polymarket-wallet-analyzer) — wallet trade feed
  - simulate_fill / fetch_price / fetch_midpoint / lookup_market
                                     (polymarket-paper-trader) — book-walk fill + prices
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))  # -> polymarket-skills/

for _rel in (
    "polymarket-scanner/scripts",
    "polymarket-analyzer/scripts",
    "polymarket-wallet-analyzer/scripts",
    "polymarket-paper-trader/scripts",
):
    _p = os.path.join(_ROOT, _rel)
    if _p not in sys.path:
        sys.path.insert(0, _p)

# --- Slippage-aware sizer / gate (analyzer) --------------------------------
from weather_edge_helpers import compute_max_size_for_slippage  # noqa: E402

# --- Wallet trade/position feed (wallet-analyzer) --------------------------
import analyze_wallet as _aw  # noqa: E402

APIClient = _aw.APIClient
is_address = _aw.is_address
fetch_trades = _aw.fetch_trades
fetch_positions = _aw.fetch_positions
sanitize_text = _aw.sanitize_text
to_float = _aw.to_float
_end_in_past = _aw._end_in_past

# --- Book-walk fill simulator + price/market lookups (paper-trader) ---------
import paper_engine as _pe  # noqa: E402

simulate_fill = _pe._simulate_fill
fetch_price = _pe.fetch_price
fetch_midpoint = _pe.fetch_midpoint
lookup_market = _pe.lookup_market
_raw_fetch_book = _pe.fetch_orderbook


# ---------------------------------------------------------------------------
# Normalized order book (shape consumed by both compute_max_size_for_slippage
# and simulate_fill): {bids desc, asks asc, best_bid, best_ask, midpoint}.
# ---------------------------------------------------------------------------
def fetch_orderbook(token_id: str, depth: int = 100) -> dict:
    """Fetch the CLOB order book and normalize it (floats, sorted, best levels)."""
    raw = _raw_fetch_book(token_id)
    bids = [
        {"price": float(b["price"]), "size": float(b["size"])}
        for b in (raw.get("bids") or [])
        if b.get("price") is not None and b.get("size") is not None
    ]
    asks = [
        {"price": float(a["price"]), "size": float(a["size"])}
        for a in (raw.get("asks") or [])
        if a.get("price") is not None and a.get("size") is not None
    ]
    bids.sort(key=lambda x: x["price"], reverse=True)
    asks.sort(key=lambda x: x["price"])
    best_bid = bids[0]["price"] if bids else 0.0
    best_ask = asks[0]["price"] if asks else 1.0
    return {
        "bids": bids[:depth],
        "asks": asks[:depth],
        "best_bid": best_bid,
        "best_ask": best_ask,
        "midpoint": round((best_bid + best_ask) / 2, 6),
    }


def market_volume(token_id: str) -> float:
    """Best-effort 24h USD volume for a token's market (Gamma). 0.0 on failure."""
    try:
        m = lookup_market(token_id)
    except Exception:  # noqa: BLE001 — volume is informational only
        return 0.0
    if not m:
        return 0.0
    for k in ("volume24hr", "volume24Hr", "volume_24hr", "volume24hrClob", "volume"):
        v = m.get(k)
        if v is not None:
            return to_float(v)
    return 0.0


def trade_ts(trade: dict) -> float:
    """Unix-seconds timestamp of a Data API trade, across field-name shapes.

    Accepts numeric epoch seconds or an ISO-8601 string. Returns 0.0 when absent."""
    for k in ("timestamp", "matchTime", "match_time", "time", "createdAt", "created_at"):
        v = trade.get(k)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            try:
                s = str(v).replace("Z", "+00:00")
                return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                continue
    return 0.0
