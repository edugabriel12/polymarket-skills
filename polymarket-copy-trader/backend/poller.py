"""Polling loop: pull each saved wallet's new trades and copy them.

Flow per wallet (best-effort; a failing trade is skipped, never the whole poll):
  1. fetch_trades -> keep only trades newer than the wallet's cursor & baseline
  2. process chronologically: update the tracked wallet's holdings tally, fetch the
     token order book, dispatch BUY/SELL to copy_engine
  3. advance the cursor, then settle any resolved open positions

Baseline: when a wallet is first saved we stamp its latest trade ts as the baseline,
so only trades AFTER it was added are ever copied (pre-existing history is ignored,
mirroring the wallet-dashboard watcher).
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict

import clog
import copy_engine as ce
import db
import deps
import weather_filter as wf

_SETTLE_EPS = 0.02  # midpoint within this of 0/1 => market treated as resolved
_MAX_TRADES = 2000

# Only copy weather markets by default. Set COPY_WEATHER_ONLY=0 to copy all markets.
WEATHER_ONLY = os.environ.get("COPY_WEATHER_ONLY", "1") not in ("0", "", "false", "False")


def _is_weather(trade: dict, api, tag_cache: dict) -> bool:
    """Weather-market check with a keyword fast-path and a cached Gamma-tag fallback."""
    if wf.matches_keywords(wf.market_text(trade)):
        return True
    slug = trade.get("eventSlug") or ""
    if not slug:
        return False
    if slug not in tag_cache:
        try:
            tag_cache[slug] = deps.fetch_event_tags(api, slug)
        except Exception:  # noqa: BLE001 — tag lookup is best-effort
            tag_cache[slug] = []
    return wf.is_weather(trade, tag_cache[slug])


def _log(msg: str) -> None:
    print(f"[copy-poller] {msg}", file=sys.stderr, flush=True)


def latest_trade_ts(address: str, api=None) -> float:
    """Newest trade timestamp for a wallet (0.0 if none / unreachable)."""
    api = api or deps.APIClient()
    try:
        trades = deps.fetch_trades(api, address, _MAX_TRADES)
    except Exception as e:  # noqa: BLE001
        _log(f"baseline fetch failed for {address}: {e}")
        return 0.0
    return max((deps.trade_ts(t) for t in trades), default=0.0)


def poll_wallet(wallet: dict, api=None, db_path: str = db.DEFAULT_DB) -> int:
    """Copy a single wallet's new trades. Returns number of entries recorded."""
    api = api or deps.APIClient()
    address = wallet["address"]
    try:
        trades = deps.fetch_trades(api, address, _MAX_TRADES)
    except Exception as e:  # noqa: BLE001
        _log(f"{wallet['name']}: trade fetch failed — {e}")
        return 0

    floor_ts = max(wallet.get("cursor_ts") or 0.0, wallet.get("baseline_ts") or 0.0)
    fresh = [(deps.trade_ts(t), t) for t in trades]
    fresh = [(ts, t) for ts, t in fresh if ts > floor_ts]
    fresh.sort(key=lambda x: x[0])

    if fresh:
        clog.section(f"carteira '{wallet['name']}' ({address[:6]}…{address[-4:]}) "
                     f"— {len(fresh)} novo(s) trade(s) desde ts={floor_ts:.0f}")

    recorded = 0
    max_ts = floor_ts
    tag_cache: dict[str, list] = {}
    for ts, trade in fresh:
        max_ts = max(max_ts, ts)
        side = ce.trade_side(trade)
        cond = ce.cond_id(trade)
        token = ce.token_id(trade)
        if not cond or side not in ("BUY", "SELL"):
            clog.dbg(f"· ignorado (sem cond ou side desconhecido: '{side}') ts={ts:.0f}")
            continue
        # Restrict to weather markets (skip before any order-book fetch).
        if WEATHER_ONLY and not _is_weather(trade, api, tag_cache):
            clog.dbg(f"· ignorado (não é mercado de weather): "
                     f"'{deps.sanitize_text(trade.get('title')) or cond[:10]}'")
            continue
        try:
            ob = deps.fetch_orderbook(token)
        except Exception as e:  # noqa: BLE001 — one bad book must not stop the poll
            _log(f"{wallet['name']}: orderbook fetch failed for {token[:12]}… — {e}")
            continue
        vol = deps.market_volume(token)
        price = deps.to_float(trade.get("price"))
        size = deps.to_float(trade.get("size"))
        holding = db.get_holding(wallet["id"], cond, db_path)
        shares_before = deps.to_float(holding.get("shares"))

        try:
            if side == "BUY":
                ce.process_buy(wallet["id"], trade, ob, vol, db_path)
                new_sh = shares_before + size
                old_avg = deps.to_float(holding.get("avg_price"))
                new_avg = ((shares_before * old_avg) + (size * price)) / new_sh \
                    if new_sh > 0 else price
                db.set_holding(wallet["id"], cond, new_sh, new_avg, db_path)
            else:
                ce.process_sell(wallet["id"], trade, ob, sold_shares=size,
                                holder_shares_before=shares_before, db_path=db_path)
                db.set_holding(wallet["id"], cond, max(shares_before - size, 0.0),
                               deps.to_float(holding.get("avg_price")), db_path)
            recorded += 1
        except Exception as e:  # noqa: BLE001
            _log(f"{wallet['name']}: copy failed on {side} {cond[:12]}… — {e}")

    db.update_cursor(wallet["id"], max_ts, db_path)
    settle_wallet(wallet["id"], db_path)
    if recorded:
        _log(f"{wallet['name']}: recorded {recorded} entr{'y' if recorded == 1 else 'ies'}")
    return recorded


def settle_wallet(wallet_id: int, db_path: str = db.DEFAULT_DB) -> None:
    """Refresh live prices on open BUY entries and settle resolved markets.

    Settlement realizes the position's REMAINING shares once (attributed to the
    oldest open buy entry) so it never double-counts P&L already realized by a
    copied sell."""
    open_entries = db.open_buy_entries(wallet_id, db_path)
    by_cond: dict[str, list] = defaultdict(list)
    for e in open_entries:
        by_cond[e["condition_id"]].append(e)

    for cond, elist in by_cond.items():
        token = elist[0].get("token_id")
        if not token:
            continue
        try:
            current = deps.fetch_midpoint(token)
        except Exception:  # noqa: BLE001 — price unavailable; leave OPEN
            continue

        if _SETTLE_EPS < current < 1 - _SETTLE_EPS:
            for e in elist:  # still open — just refresh the live price
                db.update_entry_result(e["id"], "OPEN", round(current, 6), None,
                                       db_path=db_path)
            continue

        final = 1.0 if current >= 1 - _SETTLE_EPS else 0.0
        result = "WIN" if final == 1.0 else "LOSS"
        pos = db.get_paper_position(wallet_id, cond, db_path)
        rem = deps.to_float(pos.get("shares")) if pos else 0.0
        avg_entry = deps.to_float(pos.get("avg_entry")) if pos else 0.0

        clog.dbg(f"LIQUIDAÇÃO: {result} '{elist[0].get('market_question') or cond[:10]}' "
                 f"midpoint={current:.4f} → fecha {clog.money_shares(rem)} @ entry {avg_entry:.4f} "
                 f"(payout {clog.usd(rem if final == 1.0 else 0.0)})")

        if pos and rem > 0:
            if final == 1.0:
                db.adjust_cash(rem * 1.0, db_path)  # winning shares pay $1 each
            db.upsert_paper_position(wallet_id, cond, {"shares": 0.0, "closed": 1}, db_path)

        elist.sort(key=lambda e: e["id"])
        for i, e in enumerate(elist):
            realized = round((final - avg_entry) * rem, 4) if i == 0 else 0.0
            db.update_entry_result(e["id"], result, final, realized, settled=True,
                                   db_path=db_path)


def run_once(api=None, db_path: str = db.DEFAULT_DB) -> int:
    """Poll every active wallet once. Returns total entries recorded."""
    api = api or deps.APIClient()
    total = 0
    for wallet in db.list_wallets(active_only=True, db_path=db_path):
        total += poll_wallet(wallet, api, db_path)
    return total
