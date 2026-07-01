"""Copy-trade decision core — pure given (trade, orderbook, state).

Every function here takes an already-fetched order book (network is the poller's job),
so the whole module is unit-testable offline with synthetic books. It reuses the
in-repo slippage sizer and book-walk fill simulator via `deps`.

Rules (from the plan / CLAUDE.md constraints):
  BUY  — size so weighted-avg fill slippage <= 20%, capped at $100, floored at $5.
         Below the floor (book too thin) or out of cash -> SKIPPED, logged.
  SELL — mirror the fraction the tracked wallet sold; if that paper sell would
         exceed 20% slippage -> SKIPPED (do not execute).
"""
from __future__ import annotations

import db
from deps import compute_max_size_for_slippage, sanitize_text, simulate_fill, to_float

SLIPPAGE_CAP = 0.20
MAX_USD = 100.0
MIN_USD = 5.0
_VOID_EPS = 1e-4  # |realized pnl| below this is a scratch (VOID), not win/loss


# ---------------------------------------------------------------------------
# Trade field extraction (Data API /trades records are untrusted UGC -> sanitized)
# ---------------------------------------------------------------------------
def cond_id(trade: dict) -> str:
    return str(trade.get("conditionId") or trade.get("market") or "")


def token_id(trade: dict) -> str:
    return str(trade.get("asset") or trade.get("tokenId") or trade.get("token_id") or "")


def trade_side(trade: dict) -> str:
    """Normalize the wallet's action to BUY or SELL (BID->BUY, ASK->SELL)."""
    s = (trade.get("side") or trade.get("type") or "").upper()
    if s in ("BID",):
        return "BUY"
    if s in ("ASK",):
        return "SELL"
    return s


def _market_fields(trade: dict) -> dict:
    title = sanitize_text(trade.get("title"))
    slug = trade.get("slug", "") or ""
    eslug = trade.get("eventSlug", "") or ""
    url = None
    ref = eslug or slug
    if ref:
        url = f"https://polymarket.com/event/{ref}"
    return {
        "market_question": title,
        "market_slug": slug,
        "market_url": url,
        "outcome": sanitize_text(trade.get("outcome") or "").upper() or None,
    }


def _base_entry(wallet_id: int, trade: dict, action: str) -> dict:
    f = _market_fields(trade)
    return {
        "wallet_id": wallet_id,
        "condition_id": cond_id(trade),
        "token_id": token_id(trade),
        "market_question": f["market_question"],
        "market_slug": f["market_slug"],
        "market_url": f["market_url"],
        "copy_action": action,
        "source_price": to_float(trade.get("price")),
        "source_trade_ts": to_float(trade.get("timestamp") or trade.get("matchTime")),
        "requested_usd": None,
        "executed_usd": None,
        "shares": None,
        "avg_fill_price": None,
        "best_price": None,
        "slippage_pct": None,
        "volume_24h": None,
        "status": "SKIPPED",
        "skip_reason": None,
        "result_status": "OPEN",
        "current_price": None,
        "realized_pnl": None,
    }


def _actual_slippage(avg_fill: float, best: float, side: str) -> float:
    if best <= 0:
        return 0.0
    return (avg_fill - best) / best if side == "BUY" else (best - avg_fill) / best


# ---------------------------------------------------------------------------
# BUY
# ---------------------------------------------------------------------------
def process_buy(wallet_id: int, trade: dict, orderbook: dict, volume_24h: float,
                db_path: str = db.DEFAULT_DB) -> dict:
    """Copy a tracked wallet's BUY into the paper portfolio, slippage-bounded."""
    entry = _base_entry(wallet_id, trade, "BUY")
    entry["volume_24h"] = volume_24h
    best_ask = to_float(orderbook.get("best_ask"))
    entry["best_price"] = best_ask

    sz = compute_max_size_for_slippage(orderbook, "BUY", SLIPPAGE_CAP)
    max_usd = to_float(sz.get("max_usd"))
    target = min(max_usd, MAX_USD)
    entry["requested_usd"] = round(target, 4)

    cash = db.get_cash(db_path)
    if target < MIN_USD:
        entry["skip_reason"] = (
            f"slippage: max ${max_usd:.2f} within 20% < ${MIN_USD:.0f} floor"
        )
    elif cash < MIN_USD:
        entry["skip_reason"] = f"insufficient paper cash (${cash:.2f})"
    else:
        target = min(target, cash)  # >= MIN_USD (both operands are)
        fill = simulate_fill(orderbook, "BUY", target, 0.0)
        shares = to_float(fill.get("shares_filled"))
        avg = to_float(fill.get("avg_price"))
        cost = to_float(fill.get("total_cost"))
        spent = round(cost - to_float(fill.get("fee")), 4)
        db.adjust_cash(-cost, db_path)

        pos = db.get_paper_position(wallet_id, entry["condition_id"], db_path)
        old_sh = to_float(pos.get("shares")) if pos else 0.0
        old_avg = to_float(pos.get("avg_entry")) if pos else 0.0
        new_sh = old_sh + shares
        new_avg = ((old_sh * old_avg) + (shares * avg)) / new_sh if new_sh > 0 else avg
        db.upsert_paper_position(wallet_id, entry["condition_id"], {
            "token_id": entry["token_id"],
            "market_question": entry["market_question"],
            "market_url": entry["market_url"],
            "side": _market_fields(trade)["outcome"],
            "shares": round(new_sh, 6),
            "avg_entry": round(new_avg, 6),
            "closed": 0,
        }, db_path)

        entry.update({
            "status": "EXECUTED",
            "executed_usd": spent,
            "shares": round(shares, 6),
            "avg_fill_price": round(avg, 6),
            "slippage_pct": round(_actual_slippage(avg, best_ask, "BUY"), 6),
            "result_status": "OPEN",
            "current_price": round(avg, 6),
        })

    entry["id"] = db.insert_entry(entry, db_path)
    return entry


# ---------------------------------------------------------------------------
# SELL
# ---------------------------------------------------------------------------
def process_sell(wallet_id: int, trade: dict, orderbook: dict,
                 sold_shares: float, holder_shares_before: float,
                 db_path: str = db.DEFAULT_DB) -> dict:
    """Copy a tracked wallet's SELL, proportional to the fraction it sold.

    Skips (does not execute) when the paper sell would breach the 20% slippage cap,
    or when the paper portfolio holds nothing in this market."""
    entry = _base_entry(wallet_id, trade, "SELL")
    best_bid = to_float(orderbook.get("best_bid"))
    entry["best_price"] = best_bid

    pos = db.get_paper_position(wallet_id, entry["condition_id"], db_path)
    pos_shares = to_float(pos.get("shares")) if pos else 0.0
    if not pos or pos_shares <= 0 or int(pos.get("closed") or 0) == 1:
        entry["skip_reason"] = "no paper position to sell"
        entry["id"] = db.insert_entry(entry, db_path)
        return entry

    frac = 1.0
    if holder_shares_before > 0:
        frac = min(1.0, sold_shares / holder_shares_before)
    sell_shares = pos_shares * frac
    entry["shares"] = round(sell_shares, 6)

    if best_bid <= 0 or sell_shares <= 0:
        entry["skip_reason"] = "no bid / nothing to sell"
        entry["id"] = db.insert_entry(entry, db_path)
        return entry

    intended_usd = sell_shares * best_bid
    entry["requested_usd"] = round(intended_usd, 4)
    sz = compute_max_size_for_slippage(orderbook, "SELL", SLIPPAGE_CAP)
    max_usd = to_float(sz.get("max_usd"))

    if intended_usd > max_usd + 1e-9:
        entry["skip_reason"] = (
            f"sell slippage > 20% (intended ${intended_usd:.2f} > max ${max_usd:.2f})"
        )
        entry["id"] = db.insert_entry(entry, db_path)
        return entry

    fill = simulate_fill(orderbook, "SELL", intended_usd, 0.0)
    sold = to_float(fill.get("shares_filled"))
    avg_sell = to_float(fill.get("avg_price"))
    proceeds = round(sold * avg_sell - to_float(fill.get("fee")), 4)
    db.adjust_cash(proceeds, db_path)

    avg_entry = to_float(pos.get("avg_entry"))
    realized = (avg_sell - avg_entry) * sold
    new_sh = pos_shares - sold
    closed = 1 if new_sh <= 1e-6 else 0
    db.upsert_paper_position(wallet_id, entry["condition_id"], {
        "shares": round(max(new_sh, 0.0), 6),
        "closed": closed,
    }, db_path)

    if realized > _VOID_EPS:
        result = "WIN"
    elif realized < -_VOID_EPS:
        result = "LOSS"
    else:
        result = "VOID"
    entry.update({
        "status": "EXECUTED",
        "executed_usd": proceeds,
        "shares": round(sold, 6),
        "avg_fill_price": round(avg_sell, 6),
        "slippage_pct": round(_actual_slippage(avg_sell, best_bid, "SELL"), 6),
        "result_status": result,
        "current_price": round(avg_sell, 6),
        "realized_pnl": round(realized, 4),
    })
    entry["id"] = db.insert_entry(entry, db_path)
    return entry
