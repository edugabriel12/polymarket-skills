#!/usr/bin/env python3
"""
Polymarket Paper Trading Engine

Simulates trades against live Polymarket data with zero financial risk.
Uses SQLite for persistent storage across agent sessions.
Fetches real prices from the CLOB and Gamma APIs.
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_DIR = Path.home() / ".polymarket-paper"
DB_PATH = DB_DIR / "portfolio.db"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
DEFAULT_BALANCE = 1000.0

# Risk defaults (overridable per-portfolio)
DEFAULT_RISK = {
    "max_position_pct": 0.10,       # 10% of bankroll per trade
    "max_drawdown_pct": 0.30,       # 30% total drawdown halts trading
    "max_concurrent_positions": 50,
    "daily_loss_limit_pct": 0.05,   # 5% of starting bankroll
    "max_single_market_pct": 0.20,  # 20% portfolio in one market
    "human_approval_pct": 0.15,     # trades > 15% need human approval
}

# Polymarket fee tiers — most markets are fee-free.
# Crypto 5-min / 15-min markets use a dynamic maker/taker model.
# We model the common case (0%) and let callers override.
DEFAULT_FEE_RATE = 0.0

# Token ID format: numeric string, typically 50-100 digits
import re
_TOKEN_ID_RE = re.compile(r"^\d{20,120}$")


class NoOpenPositionError(RuntimeError):
    """close_position was asked to close a (token, side) with no open
    positions row. Subclasses RuntimeError so existing `except RuntimeError`
    guards (e.g. weather_edge_bot's resolution sweep) keep working, while
    letting the bot's cashout self-heal catch this case specifically."""


def _validate_token_id(token_id: str) -> str:
    """Validate a CLOB token ID before using it in URLs."""
    if not isinstance(token_id, str) or not _TOKEN_ID_RE.match(token_id):
        raise ValueError(
            f"Invalid token ID format: must be 20-120 digits, got: {token_id!r}"
        )
    return token_id


# Tokens de venue externa (ex. tickers Kalshi "KXHIGHNY-26JUL12-B85").
# Aceitos APENAS no caminho em que o caller fornece market_question + price
# explícitos — nesse caminho o token nunca entra em URL da CLOB/Gamma; a
# validação é defesa em profundidade contra lixo no DB.
_EXTERNAL_TOKEN_RE = re.compile(r"^[A-Za-z0-9._:-]{5,80}$")


def _validate_external_token_id(token_id: str) -> str:
    if not isinstance(token_id, str) or not _EXTERNAL_TOKEN_RE.match(token_id):
        raise ValueError(
            f"Invalid external token/ticker format: {token_id!r}"
        )
    return token_id


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _api_get(url: str, timeout: int = 15) -> dict | list:
    """GET JSON from a URL. Returns parsed JSON."""
    req = Request(url, headers={"User-Agent": "polymarket-paper-trader/1.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (URLError, HTTPError) as exc:
        raise RuntimeError(f"API request failed: {url} — {exc}") from exc


def fetch_orderbook(token_id: str) -> dict:
    """Fetch the live order book for a CLOB token."""
    _validate_token_id(token_id)
    return _api_get(f"{CLOB_API}/book?token_id={token_id}")


def fetch_midpoint(token_id: str) -> float:
    """Fetch the midpoint price for a token."""
    _validate_token_id(token_id)
    data = _api_get(f"{CLOB_API}/midpoint?token_id={token_id}")
    return float(data["mid"])


def fetch_price(token_id: str, side: str) -> float:
    """Fetch the best price for a side (buy/sell)."""
    _validate_token_id(token_id)
    data = _api_get(f"{CLOB_API}/price?token_id={token_id}&side={side}")
    return float(data["price"])


def lookup_market(token_id: str) -> dict | None:
    """Look up market metadata by CLOB token ID via Gamma API."""
    _validate_token_id(token_id)
    data = _api_get(
        f"{GAMMA_API}/markets?clob_token_ids={token_id}&limit=1"
    )
    if data and len(data) > 0:
        return data[0]
    return None


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def _get_db() -> sqlite3.Connection:
    """Open (and possibly initialize) the SQLite database."""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS portfolios (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL DEFAULT 'default',
            starting_balance REAL NOT NULL,
            cash_balance  REAL NOT NULL,
            peak_value    REAL NOT NULL,
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            risk_config   TEXT NOT NULL,
            active        INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS positions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            portfolio_id  INTEGER NOT NULL REFERENCES portfolios(id),
            token_id      TEXT NOT NULL,
            market_question TEXT,
            side          TEXT NOT NULL CHECK(side IN ('YES','NO')),
            shares        REAL NOT NULL DEFAULT 0,
            avg_entry     REAL NOT NULL DEFAULT 0,
            current_price REAL NOT NULL DEFAULT 0,
            opened_at     TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            closed        INTEGER NOT NULL DEFAULT 0,
            closed_at     TEXT
        );

        CREATE TABLE IF NOT EXISTS trades (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            portfolio_id  INTEGER NOT NULL REFERENCES portfolios(id),
            token_id      TEXT NOT NULL,
            market_question TEXT,
            side          TEXT NOT NULL CHECK(side IN ('YES','NO')),
            action        TEXT NOT NULL CHECK(action IN ('BUY','SELL')),
            shares        REAL NOT NULL,
            price         REAL NOT NULL,
            fee           REAL NOT NULL DEFAULT 0,
            total_cost    REAL NOT NULL,
            reasoning     TEXT,
            executed_at   TEXT NOT NULL,
            entry_avg     REAL
        );

        CREATE TABLE IF NOT EXISTS daily_snapshots (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            portfolio_id  INTEGER NOT NULL REFERENCES portfolios(id),
            date          TEXT NOT NULL,
            cash_balance  REAL NOT NULL,
            positions_value REAL NOT NULL,
            total_value   REAL NOT NULL,
            daily_pnl     REAL NOT NULL DEFAULT 0,
            UNIQUE(portfolio_id, date)
        );
    """)
    conn.commit()
    _migrate_positions_unique(conn)


def _migrate_positions_unique(conn: sqlite3.Connection):
    """One-time rebuild: drop the table-level
    UNIQUE(portfolio_id, token_id, side, closed) in favor of a PARTIAL
    unique index over open rows only.

    The old constraint allowed at most ONE closed=1 row per (portfolio,
    token, side): re-trading a token after a full close worked on BUY
    (new closed=0 row) but the second close's UPDATE ... SET closed=1
    collided with the historical closed row -> sqlite3.IntegrityError,
    leaving the position stuck open forever (observed 8x in the
    2026-07-06 ladder cashout incident). The partial index keeps the
    real invariant (one OPEN row per key) and allows unlimited closed
    history.

    Idempotent: the detection string vanishes after the rebuild. Safe:
    no other table references positions by FK, and the old constraint
    already guarantees <=1 open row per key, so creating the partial
    unique index over existing data cannot fail.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='positions'"
    ).fetchone()
    if row and "UNIQUE(portfolio_id, token_id, side, closed)" in (row["sql"] or ""):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""
            CREATE TABLE positions_new (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                portfolio_id  INTEGER NOT NULL REFERENCES portfolios(id),
                token_id      TEXT NOT NULL,
                market_question TEXT,
                side          TEXT NOT NULL CHECK(side IN ('YES','NO')),
                shares        REAL NOT NULL DEFAULT 0,
                avg_entry     REAL NOT NULL DEFAULT 0,
                current_price REAL NOT NULL DEFAULT 0,
                opened_at     TEXT NOT NULL,
                updated_at    TEXT NOT NULL,
                closed        INTEGER NOT NULL DEFAULT 0,
                closed_at     TEXT
            )""")
        # Explicit column list so the copy survives future column additions.
        conn.execute("""
            INSERT INTO positions_new
                (id, portfolio_id, token_id, market_question, side, shares,
                 avg_entry, current_price, opened_at, updated_at, closed,
                 closed_at)
            SELECT id, portfolio_id, token_id, market_question, side, shares,
                   avg_entry, current_price, opened_at, updated_at, closed,
                   closed_at
            FROM positions""")
        conn.execute("DROP TABLE positions")
        conn.execute("ALTER TABLE positions_new RENAME TO positions")
        conn.commit()
    # DROP TABLE above also removes any index on it — (re)create outside the
    # detection branch so fresh DBs get the index too.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_positions_one_open "
        "ON positions(portfolio_id, token_id, side) WHERE closed = 0")
    conn.commit()


# ---------------------------------------------------------------------------
# Portfolio operations
# ---------------------------------------------------------------------------

def init_portfolio(
    starting_balance: float = DEFAULT_BALANCE,
    name: str = "default",
    risk_config: dict | None = None,
) -> dict:
    """Create a new paper-trading portfolio."""
    if starting_balance <= 0:
        raise ValueError("Starting balance must be positive")

    risk = {**DEFAULT_RISK, **(risk_config or {})}
    now = datetime.now(timezone.utc).isoformat()

    conn = _get_db()
    try:
        # Deactivate existing portfolios with the same name
        conn.execute(
            "UPDATE portfolios SET active = 0 WHERE name = ? AND active = 1",
            (name,),
        )
        cur = conn.execute(
            """INSERT INTO portfolios
               (name, starting_balance, cash_balance, peak_value,
                created_at, updated_at, risk_config, active)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1)""",
            (name, starting_balance, starting_balance, starting_balance,
             now, now, json.dumps(risk)),
        )
        conn.commit()
        pid = cur.lastrowid
    finally:
        conn.close()

    return {
        "portfolio_id": pid,
        "name": name,
        "starting_balance": starting_balance,
        "cash_balance": starting_balance,
        "positions": [],
        "total_value": starting_balance,
        "pnl": 0.0,
        "pnl_pct": 0.0,
        "created_at": now,
    }


def _active_portfolio(conn: sqlite3.Connection, name: str = "default") -> dict:
    """Fetch the active portfolio row or raise."""
    row = conn.execute(
        "SELECT * FROM portfolios WHERE name = ? AND active = 1 ORDER BY id DESC LIMIT 1",
        (name,),
    ).fetchone()
    if not row:
        raise RuntimeError(
            f"No active portfolio '{name}'. Run: python paper_engine.py --action init"
        )
    return dict(row)


def get_portfolio(name: str = "default", refresh_prices: bool = True) -> dict:
    """Return the current portfolio state with live-priced positions."""
    conn = _get_db()
    try:
        pf = _active_portfolio(conn, name)
        pid = pf["id"]

        positions = conn.execute(
            "SELECT * FROM positions WHERE portfolio_id = ? AND closed = 0",
            (pid,),
        ).fetchall()

        pos_list = []
        positions_value = 0.0
        for p in positions:
            p = dict(p)
            if refresh_prices:
                try:
                    p["current_price"] = fetch_midpoint(p["token_id"])
                    conn.execute(
                        "UPDATE positions SET current_price = ?, updated_at = ? WHERE id = ?",
                        (p["current_price"],
                         datetime.now(timezone.utc).isoformat(), p["id"]),
                    )
                except Exception:
                    pass  # keep stale price
            value = p["shares"] * p["current_price"]
            unrealized_pnl = (p["current_price"] - p["avg_entry"]) * p["shares"]
            pos_list.append({
                "token_id": p["token_id"],
                "market_question": p["market_question"],
                "side": p["side"],
                "shares": p["shares"],
                "avg_entry": p["avg_entry"],
                "current_price": p["current_price"],
                "value": round(value, 4),
                "unrealized_pnl": round(unrealized_pnl, 4),
                "opened_at": p["opened_at"],
            })
            positions_value += value

        total_value = pf["cash_balance"] + positions_value
        starting = pf["starting_balance"]
        pnl = total_value - starting

        # Update peak
        if total_value > pf["peak_value"]:
            conn.execute(
                "UPDATE portfolios SET peak_value = ?, updated_at = ? WHERE id = ?",
                (total_value, datetime.now(timezone.utc).isoformat(), pid),
            )

        conn.commit()

        return {
            "portfolio_id": pid,
            "name": pf["name"],
            "starting_balance": starting,
            "cash_balance": round(pf["cash_balance"], 4),
            "positions_value": round(positions_value, 4),
            "total_value": round(total_value, 4),
            "pnl": round(pnl, 4),
            "pnl_pct": round(pnl / starting * 100, 2) if starting else 0,
            "peak_value": round(max(pf["peak_value"], total_value), 4),
            "drawdown_pct": round(
                (max(pf["peak_value"], total_value) - total_value)
                / max(pf["peak_value"], total_value) * 100, 2
            ) if max(pf["peak_value"], total_value) > 0 else 0,
            "positions": pos_list,
            "num_open_positions": len(pos_list),
            "created_at": pf["created_at"],
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Order book fill simulation
# ---------------------------------------------------------------------------

def _simulate_fill(
    orderbook: dict,
    side: str,
    size_usd: float,
    fee_rate: float = DEFAULT_FEE_RATE,
) -> dict:
    """
    Walk the order book to simulate a realistic fill.

    For a BUY: we consume asks (ascending price).
    For a SELL: we consume bids (descending price).

    Returns: {avg_price, shares_filled, total_cost, fee}
    """
    if side == "BUY":
        levels = orderbook.get("asks", [])
        # asks are already sorted ascending by CLOB
        levels = sorted(levels, key=lambda x: float(x["price"]))
    else:
        levels = orderbook.get("bids", [])
        levels = sorted(levels, key=lambda x: float(x["price"]), reverse=True)

    if not levels:
        raise RuntimeError(
            f"No {'asks' if side == 'BUY' else 'bids'} in order book — "
            "market may be illiquid or closed"
        )

    remaining_usd = size_usd
    total_shares = 0.0
    total_spent = 0.0

    for level in levels:
        price = float(level["price"])
        available_shares = float(level["size"])

        if price <= 0:
            continue

        # How many shares can we buy/sell at this level with remaining USD?
        max_shares_at_level = remaining_usd / price
        fill_shares = min(available_shares, max_shares_at_level)
        fill_cost = fill_shares * price

        total_shares += fill_shares
        total_spent += fill_cost
        remaining_usd -= fill_cost

        if remaining_usd < 0.001:  # close enough to zero
            break

    if total_shares == 0:
        raise RuntimeError("Could not fill any shares — check order size and book depth")

    avg_price = total_spent / total_shares
    fee = total_spent * fee_rate

    return {
        "avg_price": round(avg_price, 6),
        "shares_filled": round(total_shares, 4),
        "total_cost": round(total_spent + fee, 4),
        "fee": round(fee, 4),
        "levels_consumed": min(len(levels), 10),  # info only
    }


# ---------------------------------------------------------------------------
# Risk validation
# ---------------------------------------------------------------------------

def _validate_risk(
    portfolio: dict,
    risk_config: dict,
    side: str,
    size_usd: float,
    token_id: str,
) -> tuple[bool, str]:
    """Check trade against risk rules. Returns (ok, reason)."""
    total_value = portfolio["total_value"]
    starting = portfolio["starting_balance"]
    if total_value <= 0:
        return False, "Portfolio value is zero or negative"

    # Max position size
    max_pos = total_value * risk_config.get("max_position_pct", 0.10)
    if size_usd > max_pos:
        return False, (
            f"Trade size ${size_usd:.2f} exceeds max position "
            f"${max_pos:.2f} ({risk_config['max_position_pct']*100:.0f}% of portfolio)"
        )

    # Max drawdown
    peak = portfolio.get("peak_value", starting)
    if peak > 0:
        current_dd = (peak - total_value) / peak
        if current_dd >= risk_config.get("max_drawdown_pct", 0.30):
            return False, (
                f"Max drawdown exceeded: {current_dd*100:.1f}% "
                f"(limit {risk_config['max_drawdown_pct']*100:.0f}%)"
            )

    # Max concurrent positions (only for new positions)
    if side == "BUY":
        max_conc = risk_config.get("max_concurrent_positions", 5)
        if portfolio["num_open_positions"] >= max_conc:
            # Check if this is adding to an existing position
            existing = [p for p in portfolio["positions"]
                        if p["token_id"] == token_id]
            if not existing:
                return False, (
                    f"Max concurrent positions reached: "
                    f"{portfolio['num_open_positions']}/{max_conc}"
                )

    # Single market exposure
    existing_value = sum(
        p["value"] for p in portfolio["positions"]
        if p["token_id"] == token_id
    )
    new_exposure = existing_value + size_usd
    max_market = total_value * risk_config.get("max_single_market_pct", 0.20)
    if new_exposure > max_market:
        return False, (
            f"Single market exposure ${new_exposure:.2f} exceeds limit "
            f"${max_market:.2f} ({risk_config['max_single_market_pct']*100:.0f}%)"
        )

    # Human approval threshold
    approval_pct = risk_config.get("human_approval_pct", 0.15)
    if size_usd > total_value * approval_pct:
        return False, (
            f"Trade size ${size_usd:.2f} exceeds human approval threshold "
            f"({approval_pct*100:.0f}% of portfolio = ${total_value*approval_pct:.2f}). "
            f"Reduce size or set force=True to override."
        )

    return True, "OK"


def _check_daily_loss(
    conn: sqlite3.Connection,
    pid: int,
    starting_balance: float,
    risk_config: dict,
) -> tuple[bool, str]:
    """Check if daily loss limit has been exceeded."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Sum today's realized losses from SELL trades using the entry_avg
    # snapshot recorded at trade time (not the current positions table).
    row = conn.execute(
        """SELECT COALESCE(SUM(
            CASE WHEN action='SELL' AND entry_avg IS NOT NULL
                 THEN (price - entry_avg) * shares
                 ELSE 0 END
        ), 0) as daily_realized
        FROM trades
        WHERE portfolio_id = ? AND date(executed_at) = ?""",
        (pid, today),
    ).fetchone()

    daily_loss = abs(min(0, row["daily_realized"])) if row else 0
    limit = starting_balance * risk_config.get("daily_loss_limit_pct", 0.05)

    if daily_loss >= limit:
        return False, (
            f"Daily loss limit exceeded: ${daily_loss:.2f} "
            f"(limit ${limit:.2f} = {risk_config['daily_loss_limit_pct']*100:.0f}% "
            f"of starting balance)"
        )
    return True, "OK"


# ---------------------------------------------------------------------------
# Trade execution
# ---------------------------------------------------------------------------

def place_order(
    token_id: str,
    side: str,
    size: float,
    price: float | None = None,
    reasoning: str = "",
    portfolio_name: str = "default",
    fee_rate: float = DEFAULT_FEE_RATE,
    force: bool = False,
    market_question: str | None = None,
) -> dict:
    """
    Place a paper trade.

    Args:
        token_id: CLOB token ID — ou ticker de venue externa (ex. Kalshi)
            quando market_question é fornecido
        side: 'YES' or 'NO'
        size: Amount in USD to spend
        price: Limit price (None = market order using live book)
        reasoning: Why this trade was made
        portfolio_name: Which portfolio to trade in
        fee_rate: Fee rate override (default 0 for most markets)
        force: Skip risk checks (except balance)
        market_question: quando fornecido, PULA o lookup_market na Gamma
            (venue externa: o caller já sabe a question e o token não é um
            CLOB token ID — ex. bot Kalshi, que também fornece `price`
            calculado do book da própria Kalshi)

    Returns: Trade execution result dict.
    """
    side = side.upper()
    if side not in ("YES", "NO"):
        raise ValueError(f"Side must be YES or NO, got: {side}")
    if size <= 0:
        raise ValueError("Size must be positive")

    # Fetch market data and simulate fill BEFORE acquiring the write lock
    # so we don't hold the lock during network I/O.
    if market_question is None:
        market_info = lookup_market(token_id)
        market_question = market_info["question"] if market_info else "Unknown market"
    else:
        # Venue externa: sem Gamma/CLOB — valida o formato do ticker apenas.
        _validate_external_token_id(token_id)

    if price is not None:
        # Limit order: fill at specified price
        shares = size / price
        fee = size * fee_rate
        fill = {
            "avg_price": price,
            "shares_filled": round(shares, 4),
            "total_cost": round(size + fee, 4),
            "fee": round(fee, 4),
        }
    else:
        # Market order: walk the real order book
        orderbook = fetch_orderbook(token_id)
        fill = _simulate_fill(orderbook, "BUY", size, fee_rate)

    # Get portfolio state for risk checks (also does network I/O)
    portfolio_state = get_portfolio(portfolio_name, refresh_prices=True)

    conn = _get_db()
    try:
        # Acquire exclusive write lock for atomic balance check + debit
        conn.execute("BEGIN IMMEDIATE")

        pf = _active_portfolio(conn, portfolio_name)
        pid = pf["id"]
        risk_config = json.loads(pf["risk_config"])

        # Balance check (always enforced) — re-read inside transaction
        if size > pf["cash_balance"]:
            conn.rollback()
            raise RuntimeError(
                f"Insufficient balance: need ${size:.2f}, "
                f"have ${pf['cash_balance']:.2f}"
            )

        # Risk validation
        if not force:
            ok, reason = _validate_risk(
                portfolio_state, risk_config, "BUY", size, token_id
            )
            if not ok:
                conn.rollback()
                raise RuntimeError(f"Risk check failed: {reason}")

            ok, reason = _check_daily_loss(
                conn, pid, pf["starting_balance"], risk_config
            )
            if not ok:
                conn.rollback()
                raise RuntimeError(f"Risk check failed: {reason}")

        now = datetime.now(timezone.utc).isoformat()

        # Update or create position
        existing = conn.execute(
            """SELECT * FROM positions
               WHERE portfolio_id = ? AND token_id = ? AND side = ? AND closed = 0""",
            (pid, token_id, side),
        ).fetchone()

        if existing:
            existing = dict(existing)
            old_shares = existing["shares"]
            old_avg = existing["avg_entry"]
            new_shares = old_shares + fill["shares_filled"]
            # Weighted average entry
            new_avg = (
                (old_avg * old_shares + fill["avg_price"] * fill["shares_filled"])
                / new_shares
            )
            conn.execute(
                """UPDATE positions
                   SET shares = ?, avg_entry = ?, current_price = ?,
                       updated_at = ?
                   WHERE id = ?""",
                (round(new_shares, 4), round(new_avg, 6),
                 fill["avg_price"], now, existing["id"]),
            )
        else:
            conn.execute(
                """INSERT INTO positions
                   (portfolio_id, token_id, market_question, side, shares,
                    avg_entry, current_price, opened_at, updated_at, closed)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                (pid, token_id, market_question, side,
                 fill["shares_filled"], fill["avg_price"],
                 fill["avg_price"], now, now),
            )

        # Deduct from balance
        new_balance = pf["cash_balance"] - fill["total_cost"]
        conn.execute(
            "UPDATE portfolios SET cash_balance = ?, updated_at = ? WHERE id = ?",
            (round(new_balance, 4), now, pid),
        )

        # Compute avg entry at trade time for accurate daily loss tracking
        if existing:
            existing = dict(existing) if not isinstance(existing, dict) else existing
            trade_entry_avg = existing["avg_entry"]
        else:
            trade_entry_avg = fill["avg_price"]

        # Record trade (includes entry_avg snapshot for daily loss calculation)
        conn.execute(
            """INSERT INTO trades
               (portfolio_id, token_id, market_question, side, action,
                shares, price, fee, total_cost, reasoning, executed_at,
                entry_avg)
               VALUES (?, ?, ?, ?, 'BUY', ?, ?, ?, ?, ?, ?, ?)""",
            (pid, token_id, market_question, side,
             fill["shares_filled"], fill["avg_price"], fill["fee"],
             fill["total_cost"], reasoning, now, fill["avg_price"]),
        )

        conn.commit()

        return {
            "status": "filled",
            "action": "BUY",
            "side": side,
            "token_id": token_id,
            "market": market_question,
            "shares": fill["shares_filled"],
            "avg_price": fill["avg_price"],
            "fee": fill["fee"],
            "total_cost": fill["total_cost"],
            "new_balance": round(new_balance, 4),
            "reasoning": reasoning,
            "executed_at": now,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def close_position(
    token_id: str,
    side: str | None = None,
    portfolio_name: str = "default",
    fee_rate: float = DEFAULT_FEE_RATE,
    reasoning: str = "",
    force_exit_price: float | None = None,
) -> dict:
    """
    Close an open position.

    Normal path (force_exit_price=None): walks orderbook bids to simulate
    a market-sell fill at current prices.

    Resolution path (force_exit_price set): credits all shares at the
    given price with no orderbook fetch. Used by the bot when a market
    resolves on Polymarket — the orderbook may be stale/empty and the
    actual payout is the resolution value, not the bid.

    Args:
        token_id: The CLOB token to close
        side: YES or NO (auto-detected if only one position exists)
        portfolio_name: Which portfolio
        fee_rate: Override fee rate
        reasoning: Why closing
        force_exit_price: When set (0.0 ≤ price ≤ 1.0), bypass orderbook
            and fill all shares at this exact price.

    Returns: Close execution result.
    """
    if force_exit_price is None:
        # Normal path: orderbook walk
        orderbook = fetch_orderbook(token_id)
        bids = sorted(
            orderbook.get("bids", []),
            key=lambda x: float(x["price"]),
            reverse=True,
        )
        if not bids:
            raise RuntimeError("No bids in order book — cannot close position")
    else:
        if not (0.0 <= force_exit_price <= 1.0):
            raise ValueError(
                f"force_exit_price {force_exit_price} out of range [0, 1]"
            )
        bids = None  # signal "skip orderbook walk"

    conn = _get_db()
    try:
        # Acquire exclusive write lock for atomic credit
        conn.execute("BEGIN IMMEDIATE")

        pf = _active_portfolio(conn, portfolio_name)
        pid = pf["id"]

        if side:
            side = side.upper()
            positions = conn.execute(
                """SELECT * FROM positions
                   WHERE portfolio_id = ? AND token_id = ? AND side = ? AND closed = 0""",
                (pid, token_id, side),
            ).fetchall()
        else:
            positions = conn.execute(
                """SELECT * FROM positions
                   WHERE portfolio_id = ? AND token_id = ? AND closed = 0""",
                (pid, token_id),
            ).fetchall()

        if not positions:
            conn.rollback()
            raise NoOpenPositionError(
                f"No open position for token {token_id}"
                + (f" side={side}" if side else "")
            )

        results = []
        for pos in positions:
            pos = dict(pos)

            if force_exit_price is not None:
                # Resolution path: all shares fill at the forced price.
                shares_sold = pos["shares"]
                total_proceeds = shares_sold * force_exit_price
                remaining_shares = 0
            else:
                remaining_shares = pos["shares"]
                total_proceeds = 0.0
                for level in bids:
                    lvl_price = float(level["price"])
                    lvl_size = float(level["size"])
                    sell_shares = min(remaining_shares, lvl_size)
                    total_proceeds += sell_shares * lvl_price
                    remaining_shares -= sell_shares
                    if remaining_shares < 0.0001:
                        break

                shares_sold = pos["shares"] - remaining_shares
                if shares_sold <= 0:
                    conn.rollback()
                    raise RuntimeError("Could not sell any shares at current bids")

            avg_sell_price = total_proceeds / shares_sold if shares_sold > 0 else 0
            fee = total_proceeds * fee_rate
            net_proceeds = total_proceeds - fee

            pnl = (avg_sell_price - pos["avg_entry"]) * shares_sold - fee

            now = datetime.now(timezone.utc).isoformat()

            # Mark position closed
            conn.execute(
                "UPDATE positions SET closed = 1, closed_at = ?, updated_at = ? WHERE id = ?",
                (now, now, pos["id"]),
            )

            # Credit proceeds to balance
            new_balance = pf["cash_balance"] + net_proceeds
            conn.execute(
                "UPDATE portfolios SET cash_balance = ?, updated_at = ? WHERE id = ?",
                (round(new_balance, 4), now, pid),
            )
            pf["cash_balance"] = new_balance

            # Record trade with entry_avg snapshot for daily loss tracking
            conn.execute(
                """INSERT INTO trades
                   (portfolio_id, token_id, market_question, side, action,
                    shares, price, fee, total_cost, reasoning, executed_at,
                    entry_avg)
                   VALUES (?, ?, ?, ?, 'SELL', ?, ?, ?, ?, ?, ?, ?)""",
                (pid, token_id, pos["market_question"], pos["side"],
                 round(shares_sold, 4), round(avg_sell_price, 6),
                 round(fee, 4), round(net_proceeds, 4), reasoning, now,
                 pos["avg_entry"]),
            )

            results.append({
                "status": "closed",
                "action": "SELL",
                "side": pos["side"],
                "token_id": token_id,
                "market": pos["market_question"],
                "shares_sold": round(shares_sold, 4),
                "avg_sell_price": round(avg_sell_price, 6),
                "avg_entry_price": pos["avg_entry"],
                "fee": round(fee, 4),
                "net_proceeds": round(net_proceeds, 4),
                "realized_pnl": round(pnl, 4),
                "new_balance": round(new_balance, 4),
                "executed_at": now,
            })

        conn.commit()
        return results[0] if len(results) == 1 else results
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def has_open_position(token_id: str, side: str | None = None,
                      portfolio_name: str = "default") -> bool:
    """Read-only ground truth: does an open (closed=0) positions row exist
    for (portfolio, token[, side])?

    Used by the weather bot's cashout self-heal to distinguish "position
    vanished" (a sibling entry sharing the same outcome token already sold
    the merged position -> stop retrying, write a phantom cashout) from
    transient close failures like "No bids in order book" (position still
    real -> keep retrying next tick).
    """
    conn = _get_db()
    try:
        pf = _active_portfolio(conn, portfolio_name)
        q = ("SELECT 1 FROM positions "
             "WHERE portfolio_id = ? AND token_id = ? AND closed = 0")
        params: list = [pf["id"], token_id]
        if side:
            q += " AND side = ?"
            params.append(side.upper())
        return conn.execute(q + " LIMIT 1", params).fetchone() is not None
    finally:
        conn.close()


def set_position_price(token_id: str, price: float,
                       portfolio_name: str = "default",
                       side: str | None = None) -> int:
    """Atualiza positions.current_price das posições abertas de um token.

    Para venues externas (ex. Kalshi): o refresh automático de preços do
    get_portfolio usa fetch_midpoint da CLOB, que falha silenciosamente com
    tickers não-Polymarket (try/except pass) e deixaria o valor de portfólio
    — e portanto o drawdown-halt — congelado no preço de entrada. O monitor
    do bot Kalshi chama isto a cada tick com o bid real da Kalshi.

    Retorna o número de linhas atualizadas (0 se não há posição aberta).
    """
    if not (0.0 <= price <= 1.0):
        raise ValueError(f"price {price} out of range [0, 1]")
    conn = _get_db()
    try:
        pf = _active_portfolio(conn, portfolio_name)
        q = ("UPDATE positions SET current_price = ?, updated_at = ? "
             "WHERE portfolio_id = ? AND token_id = ? AND closed = 0")
        params: list = [price, datetime.now(timezone.utc).isoformat(),
                        pf["id"], token_id]
        if side:
            q += " AND side = ?"
            params.append(side.upper())
        cur = conn.execute(q, params)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def get_trades(
    portfolio_name: str = "default",
    limit: int = 50,
) -> list[dict]:
    """Return trade history, most recent first."""
    conn = _get_db()
    try:
        pf = _active_portfolio(conn, portfolio_name)
        rows = conn.execute(
            """SELECT * FROM trades
               WHERE portfolio_id = ?
               ORDER BY executed_at DESC
               LIMIT ?""",
            (pf["id"], limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Daily snapshot
# ---------------------------------------------------------------------------

def take_snapshot(portfolio_name: str = "default") -> dict:
    """Record a daily portfolio snapshot for performance tracking."""
    state = get_portfolio(portfolio_name, refresh_prices=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    conn = _get_db()
    try:
        pid = state["portfolio_id"]

        # Get yesterday's snapshot for daily P&L
        prev = conn.execute(
            """SELECT total_value FROM daily_snapshots
               WHERE portfolio_id = ? AND date < ?
               ORDER BY date DESC LIMIT 1""",
            (pid, today),
        ).fetchone()

        prev_value = prev["total_value"] if prev else state["starting_balance"]
        daily_pnl = state["total_value"] - prev_value

        conn.execute(
            """INSERT OR REPLACE INTO daily_snapshots
               (portfolio_id, date, cash_balance, positions_value,
                total_value, daily_pnl)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (pid, today, state["cash_balance"], state["positions_value"],
             state["total_value"], round(daily_pnl, 4)),
        )
        conn.commit()

        return {
            "date": today,
            "total_value": state["total_value"],
            "daily_pnl": round(daily_pnl, 4),
            "cash": state["cash_balance"],
            "positions_value": state["positions_value"],
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _format_portfolio(pf: dict) -> str:
    """Format portfolio state for human-readable output."""
    lines = [
        f"=== Portfolio: {pf['name']} ===",
        f"Starting Balance:  ${pf['starting_balance']:>10,.2f}",
        f"Cash Balance:      ${pf['cash_balance']:>10,.2f}",
        f"Positions Value:   ${pf['positions_value']:>10,.2f}",
        f"Total Value:       ${pf['total_value']:>10,.2f}",
        f"P&L:               ${pf['pnl']:>10,.2f} ({pf['pnl_pct']:+.2f}%)",
        f"Peak Value:        ${pf['peak_value']:>10,.2f}",
        f"Drawdown:          {pf['drawdown_pct']:>10.2f}%",
        f"Open Positions:    {pf['num_open_positions']:>10d}",
        f"Created:           {pf['created_at']}",
    ]
    if pf["positions"]:
        lines.append("\n--- Open Positions ---")
        for p in pf["positions"]:
            pnl_str = f"${p['unrealized_pnl']:+,.2f}"
            lines.append(
                f"  {p['side']:>3} {p['shares']:>8.2f} shares @ "
                f"${p['avg_entry']:.4f} -> ${p['current_price']:.4f}  "
                f"P&L: {pnl_str}"
            )
            if p["market_question"]:
                lines.append(f"      {p['market_question'][:70]}")
    return "\n".join(lines)


def _format_trades(trades: list[dict]) -> str:
    """Format trade list for human-readable output."""
    if not trades:
        return "No trades recorded."
    lines = ["=== Trade History ==="]
    for t in trades:
        lines.append(
            f"  [{t['executed_at'][:19]}] {t['action']:>4} {t['side']:>3} "
            f"{t['shares']:>8.2f} @ ${t['price']:.4f} "
            f"(cost: ${t['total_cost']:.2f}, fee: ${t['fee']:.2f})"
        )
        if t.get("market_question"):
            lines.append(f"    {t['market_question'][:70]}")
        if t.get("reasoning"):
            lines.append(f"    Reason: {t['reasoning'][:70]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# PaperEngine — OO wrapper around the module functions
# ---------------------------------------------------------------------------


class PaperEngine:
    """Thin OO wrapper providing a stable instance-based API for callers
    that prefer method calls over module functions (e.g., weather_edge_bot).

    The wrapper only re-shapes call signatures and result keys; all real
    work happens in the module-level `place_order`, `close_position`, and
    `get_portfolio` functions.
    """

    def __init__(self, portfolio: str = "default"):
        self.portfolio = portfolio

    def get_portfolio(self, refresh_prices: bool = True) -> dict:
        return get_portfolio(self.portfolio, refresh_prices=refresh_prices)

    def open_position(self, *, token_id: str, side: str, size_usd: float,
                       market_question: str = "",   # se vazio, engine faz lookup
                       fee_rate: float = DEFAULT_FEE_RATE,
                       confidence: float = 0.5,     # informational, ignored
                       reasoning: str = "",
                       price: float | None = None) -> dict:
        # market_question/price propagados (venue externa, ex. Kalshi):
        # question fornecida pula o lookup_market na Gamma; price fornecido
        # vira limit fill sem tocar o orderbook da CLOB.
        result = place_order(
            token_id=token_id, side=side, size=size_usd,
            portfolio_name=self.portfolio,
            fee_rate=fee_rate, reasoning=reasoning,
            price=price,
            market_question=market_question or None,
        )
        # Normalize result keys to what weather_edge_bot.run_execute expects.
        if result.get("status") == "filled":
            return {
                **result,
                "status": "executed",
                "cost_usd": result.get("total_cost"),
                "shares_filled": result.get("shares"),
                "avg_price": result.get("avg_price"),
            }
        return result

    def close_position(self, *, token_id: str, side: str | None = None,
                        reasoning: str = "",
                        force_exit_price: float | None = None,
                        fee_rate: float = DEFAULT_FEE_RATE) -> dict:
        # force_exit_price/fee_rate propagados: closes de venue externa
        # (Kalshi) SEMPRE usam force_exit_price (o caminho normal busca o
        # book na CLOB, que não conhece o ticker).
        return close_position(
            token_id=token_id, side=side,
            portfolio_name=self.portfolio, reasoning=reasoning,
            force_exit_price=force_exit_price, fee_rate=fee_rate,
        )

    def has_open_position(self, *, token_id: str,
                           side: str | None = None) -> bool:
        return has_open_position(token_id=token_id, side=side,
                                 portfolio_name=self.portfolio)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Polymarket Paper Trading Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --action init --balance 1000
  %(prog)s --action buy --token TOKEN_ID --side YES --size 50
  %(prog)s --action sell --token TOKEN_ID --side YES --size 50
  %(prog)s --action close --token TOKEN_ID
  %(prog)s --action portfolio
  %(prog)s --action trades
  %(prog)s --action snapshot
        """,
    )
    parser.add_argument("--action", required=True,
                        choices=["init", "buy", "sell", "close",
                                 "portfolio", "trades", "snapshot"],
                        help="Action to perform")
    parser.add_argument("--balance", type=float, default=DEFAULT_BALANCE,
                        help="Starting balance (init only)")
    parser.add_argument("--name", default="default",
                        help="Portfolio name")
    parser.add_argument("--token", help="CLOB token ID")
    parser.add_argument("--side", choices=["YES", "NO", "yes", "no"],
                        help="Trade side")
    parser.add_argument("--size", type=float, help="Trade size in USD")
    parser.add_argument("--price", type=float, default=None,
                        help="Limit price (omit for market order)")
    parser.add_argument("--reason", default="", help="Trade reasoning")
    parser.add_argument("--fee-rate", type=float, default=DEFAULT_FEE_RATE,
                        help="Fee rate override")
    parser.add_argument("--force", action="store_true",
                        help="Skip risk checks")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON")
    parser.add_argument("--limit", type=int, default=50,
                        help="Max trades to show")

    args = parser.parse_args()

    try:
        if args.action == "init":
            result = init_portfolio(args.balance, args.name)
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                print(f"Portfolio '{result['name']}' initialized with "
                      f"${result['starting_balance']:,.2f}")

        elif args.action in ("buy", "sell"):
            if not args.token:
                parser.error("--token is required for buy/sell")
            if not args.side:
                parser.error("--side is required for buy/sell")
            if not args.size:
                parser.error("--size is required for buy/sell")

            result = place_order(
                token_id=args.token,
                side=args.side.upper(),
                size=args.size,
                price=args.price,
                reasoning=args.reason,
                portfolio_name=args.name,
                fee_rate=args.fee_rate,
                force=args.force,
            )
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                print(
                    f"{result['action']} {result['side']} "
                    f"{result['shares']:.2f} shares @ "
                    f"${result['avg_price']:.4f}\n"
                    f"Market: {result['market']}\n"
                    f"Total cost: ${result['total_cost']:.2f} "
                    f"(fee: ${result['fee']:.2f})\n"
                    f"New balance: ${result['new_balance']:.2f}"
                )

        elif args.action == "close":
            if not args.token:
                parser.error("--token is required for close")
            result = close_position(
                token_id=args.token,
                side=args.side.upper() if args.side else None,
                portfolio_name=args.name,
                fee_rate=args.fee_rate,
                reasoning=args.reason,
            )
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                if isinstance(result, list):
                    for r in result:
                        print(
                            f"Closed {r['side']} position: "
                            f"{r['shares_sold']:.2f} shares @ "
                            f"${r['avg_sell_price']:.4f}\n"
                            f"Realized P&L: ${r['realized_pnl']:+,.2f}\n"
                            f"New balance: ${r['new_balance']:.2f}"
                        )
                else:
                    print(
                        f"Closed {result['side']} position: "
                        f"{result['shares_sold']:.2f} shares @ "
                        f"${result['avg_sell_price']:.4f}\n"
                        f"Realized P&L: ${result['realized_pnl']:+,.2f}\n"
                        f"New balance: ${result['new_balance']:.2f}"
                    )

        elif args.action == "portfolio":
            result = get_portfolio(args.name, refresh_prices=True)
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                print(_format_portfolio(result))

        elif args.action == "trades":
            result = get_trades(args.name, args.limit)
            if args.json:
                print(json.dumps(result, indent=2, default=str))
            else:
                print(_format_trades(result))

        elif args.action == "snapshot":
            result = take_snapshot(args.name)
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                print(
                    f"Snapshot for {result['date']}: "
                    f"${result['total_value']:,.2f} "
                    f"(daily P&L: ${result['daily_pnl']:+,.2f})"
                )

    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Inline tests (offline, synthetic temp DBs — no network)
# ---------------------------------------------------------------------------

_OLD_POSITIONS_SCHEMA = """
    CREATE TABLE portfolios (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        name          TEXT NOT NULL DEFAULT 'default',
        starting_balance REAL NOT NULL,
        cash_balance  REAL NOT NULL,
        peak_value    REAL NOT NULL,
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL,
        risk_config   TEXT NOT NULL,
        active        INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE positions (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        portfolio_id  INTEGER NOT NULL REFERENCES portfolios(id),
        token_id      TEXT NOT NULL,
        market_question TEXT,
        side          TEXT NOT NULL CHECK(side IN ('YES','NO')),
        shares        REAL NOT NULL DEFAULT 0,
        avg_entry     REAL NOT NULL DEFAULT 0,
        current_price REAL NOT NULL DEFAULT 0,
        opened_at     TEXT NOT NULL,
        updated_at    TEXT NOT NULL,
        closed        INTEGER NOT NULL DEFAULT 0,
        closed_at     TEXT,
        UNIQUE(portfolio_id, token_id, side, closed)
    );
"""


def _swap_test_db():
    """Point the module at a fresh temp DB; returns (tmp_path, restore_fn)."""
    import tempfile
    global DB_DIR, DB_PATH
    old_dir, old_path = DB_DIR, DB_PATH
    tmpdir = Path(tempfile.mkdtemp())
    DB_DIR, DB_PATH = tmpdir, tmpdir / "portfolio_test.db"

    def restore():
        global DB_DIR, DB_PATH
        DB_DIR, DB_PATH = old_dir, old_path
    return DB_PATH, restore


def _test_migration_positions():
    """Migration v-old->partial-index: rebuild preserves rows, the
    production IntegrityError disappears, one-open invariant survives,
    and re-running is a no-op."""
    tok = "1" * 30
    now = "2026-07-06T00:00:00+00:00"
    db_path, restore = _swap_test_db()
    try:
        # 1. Build the OLD schema by hand and seed the exact production
        #    failure shape: one closed=1 row + one closed=0 row, same key.
        conn = sqlite3.connect(str(db_path))
        conn.executescript(_OLD_POSITIONS_SCHEMA)
        conn.execute(
            "INSERT INTO portfolios (name, starting_balance, cash_balance, "
            "peak_value, created_at, updated_at, risk_config, active) "
            "VALUES ('default', 1000, 1000, 1000, ?, ?, '{}', 1)", (now, now))
        for closed in (1, 0):
            conn.execute(
                "INSERT INTO positions (portfolio_id, token_id, side, shares, "
                "avg_entry, current_price, opened_at, updated_at, closed) "
                "VALUES (1, ?, 'NO', 10, 0.5, 0.5, ?, ?, ?)",
                (tok, now, now, closed))
        conn.commit()
        # Reproduce the incident: second close collides with the closed row.
        try:
            conn.execute(
                "UPDATE positions SET closed = 1 WHERE token_id = ? AND closed = 0",
                (tok,))
            raise AssertionError("expected IntegrityError under OLD schema")
        except sqlite3.IntegrityError:
            conn.rollback()
        conn.close()
        print("Test 1 PASS: old schema reproduces the UNIQUE collision")

        # 2. _get_db() triggers the migration.
        conn = _get_db()
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='positions'"
        ).fetchone()["sql"]
        assert "UNIQUE(portfolio_id, token_id, side, closed)" not in sql, sql
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' "
            "AND name='idx_positions_one_open'").fetchone(), "partial index missing"
        n, = conn.execute("SELECT COUNT(*) FROM positions").fetchone()
        assert n == 2, f"rows lost in rebuild: {n}"
        print("Test 2 PASS: migration rebuilt table, index created, rows intact")

        # 3. The same UPDATE now succeeds -> two closed rows coexist.
        conn.execute(
            "UPDATE positions SET closed = 1 WHERE token_id = ? AND closed = 0",
            (tok,))
        conn.commit()
        n_closed, = conn.execute(
            "SELECT COUNT(*) FROM positions WHERE closed = 1").fetchone()
        assert n_closed == 2, n_closed
        print("Test 3 PASS: second close no longer collides (2 closed rows)")

        # 4. Partial index still enforces ONE open row per key.
        conn.execute(
            "INSERT INTO positions (portfolio_id, token_id, side, shares, "
            "avg_entry, current_price, opened_at, updated_at, closed) "
            "VALUES (1, ?, 'NO', 5, 0.4, 0.4, ?, ?, 0)", (tok, now, now))
        conn.commit()  # isolate: the rollback below must not undo this row
        try:
            conn.execute(
                "INSERT INTO positions (portfolio_id, token_id, side, shares, "
                "avg_entry, current_price, opened_at, updated_at, closed) "
                "VALUES (1, ?, 'NO', 5, 0.4, 0.4, ?, ?, 0)", (tok, now, now))
            raise AssertionError("expected IntegrityError: 2nd open row")
        except sqlite3.IntegrityError:
            conn.rollback()
        conn.close()
        print("Test 4 PASS: partial index rejects a second OPEN row")

        # 5. Idempotency: re-open (migration re-runs) without error.
        conn = _get_db()
        n, = conn.execute("SELECT COUNT(*) FROM positions").fetchone()
        assert n == 3, n  # 2 closed + 1 open from test 4
        conn.close()
        print("Test 5 PASS: migration idempotent on second run")
        print("\nAll --test-migration-positions PASS")
    finally:
        restore()


def _test_no_open_position():
    """NoOpenPositionError semantics + has_open_position + re-trade close."""
    tok = "2" * 30
    now = "2026-07-06T00:00:00+00:00"
    _, restore = _swap_test_db()
    try:
        init_portfolio(100.0)

        # 1. Closing a token with no position raises the TYPED error and it
        #    is still a RuntimeError (resolution sweep compatibility).
        try:
            close_position(tok, side="YES", force_exit_price=0.5)
            raise AssertionError("expected NoOpenPositionError")
        except NoOpenPositionError as e:
            assert isinstance(e, RuntimeError)
        assert has_open_position(tok, "YES") is False
        print("Test 1 PASS: NoOpenPositionError raised, subclasses RuntimeError")

        # 2. Seed a position by SQL (place_order would hit the network),
        #    close it via force_exit_price (no orderbook fetch) — the happy
        #    path — then verify has_open_position flips true->false.
        conn = _get_db()
        conn.execute(
            "INSERT INTO positions (portfolio_id, token_id, side, shares, "
            "avg_entry, current_price, opened_at, updated_at, closed) "
            "VALUES (1, ?, 'YES', 10, 0.3, 0.3, ?, ?, 0)", (tok, now, now))
        conn.commit()
        conn.close()
        assert has_open_position(tok, "YES") is True
        assert has_open_position(tok) is True          # side omitted
        assert has_open_position(tok, "NO") is False   # other side
        r = close_position(tok, side="YES", force_exit_price=0.5)
        assert r["status"] == "closed" and r["shares_sold"] == 10, r
        assert has_open_position(tok, "YES") is False
        print("Test 2 PASS: has_open_position true->false around close")

        # 3. Re-trade regression (the fix-D scenario end-to-end): open the
        #    SAME token/side again after a full close, close again — with the
        #    old UNIQUE this raised IntegrityError; now it must succeed.
        conn = _get_db()
        conn.execute(
            "INSERT INTO positions (portfolio_id, token_id, side, shares, "
            "avg_entry, current_price, opened_at, updated_at, closed) "
            "VALUES (1, ?, 'YES', 4, 0.6, 0.6, ?, ?, 0)", (tok, now, now))
        conn.commit()
        conn.close()
        r = close_position(tok, side="YES", force_exit_price=1.0)
        assert r["status"] == "closed", r
        conn = _get_db()
        n_closed, = conn.execute(
            "SELECT COUNT(*) FROM positions WHERE token_id = ? AND closed = 1",
            (tok,)).fetchone()
        conn.close()
        assert n_closed == 2, n_closed
        print("Test 3 PASS: re-traded token closes cleanly (2 closed rows)")
        print("\nAll --test-no-open-position PASS")
    finally:
        restore()


def _test_external_venue():
    """Caminho de venue externa (ex. tickers Kalshi): place_order com
    market_question + price explícitos NÃO pode tocar rede (Gamma/CLOB);
    closes usam force_exit_price; set_position_price mantém current_price."""
    global _api_get
    tok = "KXHIGHNY-26JUL12-B85"
    _, restore = _swap_test_db()
    saved_api = _api_get

    def _boom(url, timeout=15):
        raise AssertionError(f"network call attempted: {url}")
    _api_get = _boom
    try:
        init_portfolio(1000.0)

        # 1. Ticker Kalshi SEM market_question → ValueError do regex CLOB
        #    (caminho Polymarket intocado/protegido).
        try:
            place_order(tok, "YES", 10.0, price=0.42)
            raise AssertionError("expected ValueError for Kalshi ticker "
                                 "without market_question")
        except ValueError:
            pass
        print("Test 1 PASS: ticker externo sem market_question -> ValueError "
              "(regex CLOB preservado)")

        # 2. place_order com question + price: zero rede (_api_get explode
        #    se chamado), posição aberta, question/fee corretos.
        r = place_order(tok, "YES", 10.0, price=0.40,
                        market_question="Highest temperature in NYC?",
                        fee_rate=0.042)  # kalshi_fee_rate(0.40)=0.07*0.6
        assert r["status"] == "filled", r
        assert abs(r["shares"] - 25.0) < 1e-6, r
        assert abs(r["fee"] - 0.42) < 1e-6, r
        assert has_open_position(tok, "YES") is True
        conn = _get_db()
        row = conn.execute(
            "SELECT market_question FROM positions WHERE token_id = ?",
            (tok,)).fetchone()
        conn.close()
        assert row["market_question"] == "Highest temperature in NYC?", row
        print("Test 2 PASS: place_order externo (question+price) sem rede; "
              "25 shares @0.40, fee $0.42, question persistida")

        # 3. Wrapper PaperEngine propaga price/market_question e normaliza
        #    o resultado para o shape do run_execute.
        eng = PaperEngine(portfolio="default")
        r2 = eng.open_position(token_id="KXLOWTNYC-26JUL12-B60", side="NO",
                               size_usd=5.0, price=0.50,
                               market_question="Lowest temperature in NYC?",
                               fee_rate=0.035)
        assert r2["status"] == "executed", r2
        assert abs(r2["avg_price"] - 0.50) < 1e-9, r2
        assert abs(r2["shares_filled"] - 10.0) < 1e-6, r2
        assert r2["cost_usd"] is not None, r2
        print("Test 3 PASS: PaperEngine.open_position propaga price/question "
              "(status executed, avg_price 0.50, 10 shares)")

        # 4. set_position_price atualiza current_price (rowcount) e valida
        #    range; token sem posição aberta -> 0.
        n = set_position_price(tok, 0.55, side="YES")
        assert n == 1, n
        conn = _get_db()
        cp = conn.execute(
            "SELECT current_price FROM positions WHERE token_id = ? "
            "AND closed = 0", (tok,)).fetchone()["current_price"]
        conn.close()
        assert abs(cp - 0.55) < 1e-9, cp
        assert set_position_price("KXNOPE-26JAN01", 0.5) == 0
        try:
            set_position_price(tok, 1.5)
            raise AssertionError("expected ValueError for price 1.5")
        except ValueError:
            pass
        print("Test 4 PASS: set_position_price (1 linha, 0.55; token sem "
              "posição -> 0; range validado)")

        # 5. Wrapper close_position propaga force_exit_price + fee_rate:
        #    sem rede, P&L líquido de fee.
        r3 = eng.close_position(token_id=tok, side="YES",
                                force_exit_price=0.60, fee_rate=0.028,
                                reasoning="cashout kalshi")
        assert r3["status"] == "closed", r3
        assert abs(r3["avg_sell_price"] - 0.60) < 1e-9, r3
        # proceeds 25*0.60=15.0; fee=15*0.028=0.42; pnl=(0.60-0.40)*25-0.42
        assert abs(r3["fee"] - 0.42) < 1e-6, r3
        assert abs(r3["realized_pnl"] - 4.58) < 1e-6, r3
        assert has_open_position(tok, "YES") is False
        print("Test 5 PASS: PaperEngine.close_position propaga "
              "force_exit_price+fee_rate (pnl 4.58 líquido de fee, sem rede)")

        # 6. Validação do ticker externo: lixo com espaço/curto demais ->
        #    ValueError mesmo com question fornecida.
        for bad in ("KX HIGH", "abc", "x" * 81):
            try:
                place_order(bad, "YES", 5.0, price=0.5,
                            market_question="q")
                raise AssertionError(f"expected ValueError for {bad!r}")
            except ValueError:
                pass
        print("Test 6 PASS: tickers externos inválidos rejeitados")

        print("\nAll --test-external-venue PASS (6/6)")
    finally:
        _api_get = saved_api
        restore()


if __name__ == "__main__":
    if "--test-migration-positions" in sys.argv:
        _test_migration_positions()
    elif "--test-no-open-position" in sys.argv:
        _test_no_open_position()
    elif "--test-external-venue" in sys.argv:
        _test_external_venue()
    else:
        main()
