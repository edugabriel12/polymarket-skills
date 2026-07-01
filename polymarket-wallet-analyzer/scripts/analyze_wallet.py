#!/usr/bin/env python3
"""Analyze any public Polymarket wallet: positions, P&L, win rate, and a
per-category breakdown (Tennis, Soccer, League of Legends, Counter-Strike,
Baseball, ...).

Read-only. No private key required — uses the public Polymarket Data API
(`data-api.polymarket.com`) for positions and trade history and, optionally,
the Gamma API (`gamma-api.polymarket.com`) to enrich category tags.

Usage:
    python analyze_wallet.py --address 0xABC...                       # full report
    python analyze_wallet.py --address 0xABC... --output text         # human summary
    python analyze_wallet.py --address 0xABC... --category tennis     # one category
    python analyze_wallet.py --address 0xABC... --enrich-tags         # use Gamma tags
    python analyze_wallet.py --address 0xABC... --trade-limit 1000
    python analyze_wallet.py --address 0xABC... --debug               # raw API calls

Methodology (see references/data-api.md for the full write-up):
- `/positions` is Polymarket's own computed P&L per currently-held market
  (`cashPnl` = realized + unrealized). Used as the authoritative source when a
  market is present there.
- `/trades` provides the full universe of markets the wallet has touched and is
  used to reconstruct realized P&L for markets already fully exited (no longer
  in `/positions`), via average-cost accounting.
- A market counts toward win rate only once it is RESOLVED (redeemable, or end
  date in the past, or price pinned to ~0/~1). "Won" = total market P&L > 0.

CLAUDE.md rule #5: market titles/slugs are untrusted user-generated content.
They are sanitized and never interpreted as instructions.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import requests

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

MAX_TEXT_LEN = 200
RESOLVED_PRICE_EPS = 0.02  # curPrice within this of 0 or 1 => treated as resolved
WIN_EPS = 1e-6             # P&L magnitude below this is a "scratch", not win/loss


# ---------------------------------------------------------------------------
# Category classification
# ---------------------------------------------------------------------------
#
# Ordered list of (category, compiled keyword pattern). First match wins, so the
# most specific/esports categories come before generic ones. Matching is done
# against the lowercased "title + slug + eventSlug" blob of each market.
#
# Gamma tag slugs are also mapped (see TAG_TO_CATEGORY) for --enrich-tags.

_CATEGORY_KEYWORDS: list[tuple[str, list[str]]] = [
    ("League of Legends", [r"league of legends", r"\blol\b", r"\blck\b", r"\blpl\b",
                           r"\blec\b", r"\blcs\b", r"\bmsi\b", r"worlds",
                           r"\bt1\b", r"gen\.?g", r"\bg2\b"]),
    ("Counter-Strike", [r"counter[- ]strike", r"\bcs2\b", r"cs:?go", r"\bcsgo\b",
                        r"\bblast\b", r"\biem\b", r"\bpgl\b", r"\besl\b"]),
    ("Dota 2", [r"\bdota\b", r"the international", r"\bti\d{1,2}\b"]),
    ("Valorant", [r"valorant", r"\bvct\b"]),
    ("Tennis", [r"tennis", r"\batp\b", r"\bwta\b", r"wimbledon", r"roland[- ]garros",
                r"french open", r"australian open"]),
    ("Soccer", [r"soccer", r"premier league", r"\bepl\b", r"la liga", r"serie a",
                r"bundesliga", r"ligue 1", r"champions league", r"\buefa\b",
                r"\bfifa\b", r"world cup", r"\bmls\b", r"\bfc\b", r"united vs",
                r"\bafc\b", r"copa"]),
    ("Baseball", [r"baseball", r"\bmlb\b", r"world series", r"yankees", r"dodgers"]),
    ("Basketball", [r"basketball", r"\bnba\b", r"\bncaab\b", r"euroleague",
                    r"finals mvp", r"lakers", r"celtics"]),
    ("American Football", [r"\bnfl\b", r"super ?bowl", r"college football",
                           r"\bcfb\b", r"\bncaaf\b"]),
    ("Hockey", [r"hockey", r"\bnhl\b", r"stanley cup"]),
    ("Cricket", [r"cricket", r"\bipl\b", r"\bt20\b", r"test match"]),
    ("Combat Sports", [r"\bufc\b", r"\bmma\b", r"boxing", r"\bvs\.? .* by (ko|submission)"]),
    ("Golf", [r"\bgolf\b", r"\bpga\b", r"the masters", r"ryder cup"]),
    ("Crypto", [r"bitcoin", r"\bbtc\b", r"ethereum", r"\beth\b", r"\bcrypto\b",
                r"solana", r"\bsol\b", r"dogecoin", r"\bxrp\b"]),
    ("Politics", [r"election", r"president", r"\bsenate\b", r"congress", r"\btrump\b",
                  r"\bbiden\b", r"governor", r"parliament", r"prime minister",
                  r"nominee", r"\bgop\b", r"democrat", r"republican"]),
    ("Economy", [r"\bfed\b", r"\bgdp\b", r"inflation", r"rate (hike|cut)", r"\bcpi\b",
                 r"interest rate", r"recession", r"jobs report"]),
]

_COMPILED_CATEGORIES = [
    (cat, re.compile("|".join(pats))) for cat, pats in _CATEGORY_KEYWORDS
]

# Gamma tag slug -> category, for --enrich-tags. Keys are tag *slugs* (lowercase).
TAG_TO_CATEGORY = {
    "league-of-legends": "League of Legends", "lol": "League of Legends",
    "counter-strike": "Counter-Strike", "cs2": "Counter-Strike", "csgo": "Counter-Strike",
    "dota": "Dota 2", "dota-2": "Dota 2",
    "valorant": "Valorant",
    "tennis": "Tennis",
    "soccer": "Soccer", "football": "Soccer", "epl": "Soccer", "champions-league": "Soccer",
    "baseball": "Baseball", "mlb": "Baseball",
    "basketball": "Basketball", "nba": "Basketball",
    "nfl": "American Football", "american-football": "American Football",
    "hockey": "Hockey", "nhl": "Hockey",
    "cricket": "Cricket",
    "ufc": "Combat Sports", "mma": "Combat Sports", "boxing": "Combat Sports",
    "golf": "Golf", "pga": "Golf",
    "crypto": "Crypto", "bitcoin": "Crypto", "ethereum": "Crypto",
    "politics": "Politics", "elections": "Politics", "us-election": "Politics",
    "economics": "Economy", "economy": "Economy", "fed": "Economy",
    "esports": "Esports (other)", "sports": "Sports (other)",
}

OTHER_CATEGORY = "Other"


def classify_category(text: str) -> str:
    """Keyword-classify a market into a category from its title/slug blob."""
    blob = (text or "").lower()
    for category, pattern in _COMPILED_CATEGORIES:
        if pattern.search(blob):
            return category
    return OTHER_CATEGORY


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%SZ")
    print(f"[{ts}] {msg}", file=sys.stderr, flush=True)


def sanitize_text(text: str | None) -> str:
    """Strip control chars and cap length. Titles are untrusted (CLAUDE.md #5)."""
    if not text:
        return ""
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    if len(text) > MAX_TEXT_LEN:
        text = text[:MAX_TEXT_LEN] + "..."
    return text


def is_address(addr: str) -> bool:
    return bool(re.fullmatch(r"0x[0-9a-fA-F]{40}", addr or ""))


def to_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


class APIClient:
    """`requests` wrapper with rate limiting, retries, and optional debug."""

    def __init__(self, rate_limit_ms: int = 100, timeout: int = 30, debug: bool = False):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "polymarket-skills/analyze_wallet"
        self.rate_limit = rate_limit_ms / 1000.0
        self.timeout = timeout
        self.debug = debug
        self._last_call = 0.0

    def get(self, url: str, params: dict | None = None) -> Any:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.rate_limit:
            time.sleep(self.rate_limit - elapsed)
        for attempt in range(3):
            try:
                resp = self.session.get(url, params=params or {}, timeout=self.timeout)
                self._last_call = time.monotonic()
                if self.debug:
                    log(f"GET {resp.url} -> {resp.status_code}")
                if resp.status_code == 429:
                    sleep_for = 2 ** attempt
                    log(f"429 — sleeping {sleep_for}s")
                    time.sleep(sleep_for)
                    continue
                resp.raise_for_status()
                if self.debug and len(resp.text) < 800:
                    log(f"  body: {resp.text}")
                return resp.json()
            except requests.exceptions.RequestException as e:
                if attempt == 2:
                    raise
                log(f"  attempt {attempt + 1} failed: {e}; retrying")
                time.sleep(2 ** attempt)
        return None


def _unwrap(data: Any) -> list:
    """Data API may return a bare list or wrap it in {data|positions|trades:[...]}."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "positions", "trades", "results"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


# ---------------------------------------------------------------------------
# Data API fetch (multi-shape, graceful — mirrors lol_top_holders.py)
# ---------------------------------------------------------------------------


def fetch_positions(api: APIClient, address: str) -> list[dict]:
    """Current positions for a wallet. Tries known query-param shapes."""
    out: list[dict] = []
    offset = 0
    page_size = 500
    while True:
        page = None
        for params in (
            {"user": address, "limit": page_size, "offset": offset, "sizeThreshold": 0},
            {"user": address, "limit": page_size, "offset": offset},
            {"address": address, "limit": page_size, "offset": offset},
        ):
            try:
                data = api.get(f"{DATA_API}/positions", params=params)
            except requests.exceptions.RequestException:
                continue
            items = _unwrap(data)
            if items:
                page = items
                break
        if not page:
            break
        out.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    return out


# Candidate keys carrying the trading wallet's address on a /trades record.
# `proxyWallet` is Polymarket's canonical field (confirmed by /holders shape);
# the rest are tolerant fallbacks for shape drift.
_TRADE_OWNER_KEYS = (
    "proxyWallet", "proxy_wallet", "user", "wallet", "owner", "account",
    "address", "maker",
)


def trade_owner(trade: dict) -> str | None:
    """Lowercased owner address of a /trades record, or None if unrecognizable."""
    for key in _TRADE_OWNER_KEYS:
        v = trade.get(key)
        if isinstance(v, str) and v.lower().startswith("0x") and len(v) >= 10:
            return v.lower()
    return None


def owned_trades(trades: list[dict], address: str) -> list[dict]:
    """Keep only trades that provably belong to `address`.

    The Data API `/trades` endpoint has been observed returning trades that are
    NOT the requested wallet's (an unfiltered / counterparty feed): the query
    param filtering was inferred from the public frontend, never verified
    (see references/data-api.md). So we attribute each record to its owner
    address client-side and drop anything we cannot tie to `address` — a wallet
    must never be credited with, or have copied, another wallet's trades.

    If NOT A SINGLE record carries a recognizable owner field, the response
    shape is entirely unknown; rather than silently return nothing we fall back
    to the raw feed (old behavior) and leave a warning to stderr.
    """
    addr = address.lower()
    owned = [t for t in trades if trade_owner(t) == addr]
    if trades and not owned and all(trade_owner(t) is None for t in trades):
        log(f"WARNING: /trades records for {address} carry no recognizable owner "
            f"field; cannot verify ownership — returning unfiltered feed.")
        return trades
    return owned


def fetch_trades(api: APIClient, address: str, max_trades: int) -> list[dict]:
    """Full trade history for a wallet, paginated up to max_trades.

    Only trades that provably belong to `address` are returned (see
    `owned_trades`) — the endpoint's wallet filter is not trusted blindly.
    """
    out: list[dict] = []
    offset = 0
    page_size = 500
    while len(out) < max_trades:
        page = None
        for params in (
            {"user": address, "limit": page_size, "offset": offset},
            {"address": address, "limit": page_size, "offset": offset},
            {"maker": address, "limit": page_size, "offset": offset},
        ):
            try:
                data = api.get(f"{DATA_API}/trades", params=params)
            except requests.exceptions.RequestException:
                continue
            items = _unwrap(data)
            if items:
                page = items
                break
        if not page:
            break
        out.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    return owned_trades(out, address)[:max_trades]


def fetch_event_tags(api: APIClient, event_slug: str) -> list[str]:
    """Fetch tag slugs for an event from Gamma. Best-effort; [] on failure."""
    if not event_slug:
        return []
    try:
        data = api.get(f"{GAMMA_API}/events", params={"slug": event_slug})
    except requests.exceptions.RequestException:
        return []
    events = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    tags: list[str] = []
    for ev in events:
        for tag in ev.get("tags", []) or []:
            slug = (tag.get("slug") or tag.get("label") or "").lower()
            if slug:
                tags.append(slug)
    return tags


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def market_text(rec: dict) -> str:
    """Build the classification blob from a position/trade record."""
    return " ".join(str(rec.get(k, "")) for k in ("title", "slug", "eventSlug"))


def reconstruct_trade_pnl(trades: list[dict]) -> dict[str, dict]:
    """Average-cost realized P&L per (conditionId, asset/token), summed per market.

    For each token: BUYs accumulate cost; SELLs realize (price - avg_entry)*size.
    Returns {conditionId: {realized, n_trades, shares_open, open_cost, title, slug,
    eventSlug, last_price}}. Used for markets no longer present in /positions.
    """
    # Per-token running state, keyed by (conditionId, token).
    tok_state: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"size": 0.0, "cost": 0.0, "realized": 0.0, "n": 0, "last_price": 0.0}
    )
    meta: dict[str, dict] = {}
    for t in trades:
        cond = str(t.get("conditionId") or t.get("market") or "")
        token = str(t.get("asset") or t.get("tokenId") or t.get("token_id") or "")
        if not cond:
            continue
        meta.setdefault(cond, {
            "title": sanitize_text(t.get("title")),
            "slug": t.get("slug", ""),
            "eventSlug": t.get("eventSlug", ""),
        })
        price = to_float(t.get("price"))
        size = to_float(t.get("size"))
        if size <= 0 or price <= 0:
            continue
        side = (t.get("side") or t.get("type") or "").upper()
        st = tok_state[(cond, token)]
        st["last_price"] = price
        st["n"] += 1
        if side in ("BUY", "BID"):
            st["size"] += size
            st["cost"] += price * size
        elif side in ("SELL", "ASK"):
            avg = (st["cost"] / st["size"]) if st["size"] > 0 else 0.0
            st["realized"] += (price - avg) * size
            st["size"] -= size
            st["cost"] -= avg * size

    per_market: dict[str, dict] = {}
    for (cond, _token), st in tok_state.items():
        m = per_market.setdefault(cond, {
            "realized": 0.0, "n_trades": 0, "open_shares": 0.0,
            "open_cost": 0.0, "last_price": 0.0,
            **meta.get(cond, {"title": "", "slug": "", "eventSlug": ""}),
        })
        m["realized"] += st["realized"]
        m["n_trades"] += st["n"]
        m["open_shares"] += max(st["size"], 0.0)
        m["open_cost"] += max(st["cost"], 0.0)
        m["last_price"] = st["last_price"]
    return per_market


def build_market_records(
    positions: list[dict],
    trade_pnl: dict[str, dict],
    enrich: APIClient | None,
) -> list[dict]:
    """Merge /positions (authoritative) with trade-reconstructed closed markets.

    One record per conditionId with: title, category, realized/unrealized/total
    P&L, invested, resolved flag, won flag.
    """
    records: dict[str, dict] = {}

    # 1) Authoritative current/redeemable positions.
    for p in positions:
        cond = str(p.get("conditionId") or p.get("market") or "")
        if not cond:
            continue
        cur_price = to_float(p.get("curPrice"))
        redeemable = bool(p.get("redeemable"))
        end_passed = _end_in_past(p.get("endDate"))
        resolved = (
            redeemable
            or end_passed
            or cur_price <= RESOLVED_PRICE_EPS
            or cur_price >= 1 - RESOLVED_PRICE_EPS
        )
        cash_pnl = to_float(p.get("cashPnl"))
        realized = to_float(p.get("realizedPnl"))
        rec = records.setdefault(cond, _blank_record(cond, p))
        rec["realized_pnl"] += realized
        rec["unrealized_pnl"] += cash_pnl - realized
        rec["total_pnl"] += cash_pnl
        rec["invested"] += to_float(p.get("initialValue")) or to_float(p.get("totalBought"))
        rec["current_value"] += to_float(p.get("currentValue"))
        rec["resolved"] = rec["resolved"] or resolved
        rec["_has_position"] = True

    # 2) Closed markets from trades that aren't in positions anymore.
    for cond, m in trade_pnl.items():
        rec = records.get(cond)
        if rec is None:
            # Not in /positions => wallet has fully exited this market. Use the
            # trade-reconstructed P&L. Any residual open shares are marked at the
            # last trade price; with none left, the market is treated as closed.
            rec = records.setdefault(cond, _blank_record(cond, m))
            still_open = m["open_shares"] > WIN_EPS
            mark = m["last_price"] if still_open else 0.0
            rec["realized_pnl"] = m["realized"]
            rec["unrealized_pnl"] = (mark * m["open_shares"]) - m["open_cost"]
            rec["total_pnl"] = rec["realized_pnl"] + rec["unrealized_pnl"]
            rec["invested"] = m["open_cost"]
            rec["resolved"] = not still_open
        # Present in /positions already => P&L is authoritative there; only the
        # trade count is additive.
        rec["n_trades"] = rec.get("n_trades", 0) + m["n_trades"]

    # 3) Categorize + finalize.
    out = []
    for cond, rec in records.items():
        blob = rec["_text"]
        category = OTHER_CATEGORY
        if enrich is not None:
            tags = fetch_event_tags(enrich, rec["eventSlug"])
            for tg in tags:
                if tg in TAG_TO_CATEGORY:
                    category = TAG_TO_CATEGORY[tg]
                    break
        if category == OTHER_CATEGORY:
            category = classify_category(blob)
        rec["category"] = category
        rec["total_pnl"] = round(rec["total_pnl"], 2)
        rec["realized_pnl"] = round(rec["realized_pnl"], 2)
        rec["unrealized_pnl"] = round(rec["unrealized_pnl"], 2)
        rec["invested"] = round(rec["invested"], 2)
        rec["current_value"] = round(rec.get("current_value", 0.0), 2)
        if rec["resolved"] and abs(rec["total_pnl"]) > WIN_EPS:
            rec["won"] = rec["total_pnl"] > 0
        else:
            rec["won"] = None  # open or scratch => excluded from win rate
        for k in ("_text", "_has_position"):
            rec.pop(k, None)
        out.append(rec)
    out.sort(key=lambda r: r["total_pnl"], reverse=True)
    return out


def _blank_record(cond: str, src: dict) -> dict:
    title = sanitize_text(src.get("title"))
    slug = src.get("slug", "")
    event_slug = src.get("eventSlug", "")
    return {
        "condition_id": cond,
        "title": title,
        "slug": slug,
        "eventSlug": event_slug,
        "category": OTHER_CATEGORY,
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "total_pnl": 0.0,
        "invested": 0.0,
        "current_value": 0.0,
        "resolved": False,
        "won": None,
        "n_trades": 0,
        "_text": " ".join(str(src.get(k, "")) for k in ("title", "slug", "eventSlug")),
        "_has_position": False,
    }


def _end_in_past(end: str | None) -> bool:
    if not end:
        return False
    try:
        dt = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except ValueError:
        return False
    # Polymarket's endDate may arrive WITHOUT a timezone (offset-naive). Comparing a naive
    # datetime to an aware now() raises "can't compare offset-naive and offset-aware
    # datetimes" — assume UTC for naive timestamps so the comparison is always valid.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt < datetime.now(timezone.utc)


def summarize(records: list[dict]) -> dict:
    """Build overall + per-category rollups from per-market records."""
    cats: dict[str, dict] = defaultdict(
        lambda: {"markets": 0, "resolved": 0, "wins": 0, "losses": 0,
                 "total_pnl": 0.0, "realized_pnl": 0.0, "unrealized_pnl": 0.0,
                 "invested": 0.0}
    )
    overall = {"markets": 0, "resolved": 0, "wins": 0, "losses": 0,
               "total_pnl": 0.0, "realized_pnl": 0.0, "unrealized_pnl": 0.0,
               "invested": 0.0, "current_value": 0.0}

    for r in records:
        c = cats[r["category"]]
        for bucket in (c, overall):
            bucket["markets"] += 1
            bucket["total_pnl"] += r["total_pnl"]
            bucket["realized_pnl"] += r["realized_pnl"]
            bucket["unrealized_pnl"] += r["unrealized_pnl"]
            bucket["invested"] += r["invested"]
        overall["current_value"] += r["current_value"]
        if r["won"] is not None:
            for bucket in (c, overall):
                bucket["resolved"] += 1
                if r["won"]:
                    bucket["wins"] += 1
                else:
                    bucket["losses"] += 1

    def finalize(b: dict) -> dict:
        resolved = b["resolved"]
        b["win_rate"] = round(b["wins"] / resolved, 4) if resolved else None
        b["roi"] = round(b["total_pnl"] / b["invested"], 4) if b["invested"] > 0 else None
        for k in ("total_pnl", "realized_pnl", "unrealized_pnl", "invested"):
            b[k] = round(b[k], 2)
        if "current_value" in b:
            b["current_value"] = round(b["current_value"], 2)
        return b

    categories = {name: finalize(b) for name, b in cats.items()}
    # Sort categories by total P&L desc for stable, useful output.
    categories = dict(sorted(categories.items(),
                             key=lambda kv: kv[1]["total_pnl"], reverse=True))
    return {"overall": finalize(overall), "by_category": categories}


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def render_text(report: dict) -> str:
    o = report["summary"]["overall"]
    addr = report["address"]
    lines = []
    lines.append(f"Polymarket wallet analysis — {addr}")
    lines.append("=" * 60)
    wr = "n/a" if o["win_rate"] is None else f"{o['win_rate']*100:.1f}%"
    roi = "n/a" if o["roi"] is None else f"{o['roi']*100:+.1f}%"
    lines.append(f"Markets traded : {o['markets']}  (resolved: {o['resolved']})")
    lines.append(f"Win rate       : {wr}  ({o['wins']}W / {o['losses']}L)")
    lines.append(f"Total P&L      : ${o['total_pnl']:+,.2f}  (ROI {roi})")
    lines.append(f"  realized     : ${o['realized_pnl']:+,.2f}")
    lines.append(f"  unrealized   : ${o['unrealized_pnl']:+,.2f}")
    lines.append(f"Invested       : ${o['invested']:,.2f}")
    lines.append(f"Current value  : ${o['current_value']:,.2f}")
    lines.append("")
    lines.append(f"{'Category':<20}{'Mkts':>5}{'W-L':>8}{'WinRate':>9}{'P&L':>14}")
    lines.append("-" * 60)
    for name, c in report["summary"]["by_category"].items():
        wr = "  n/a" if c["win_rate"] is None else f"{c['win_rate']*100:5.1f}%"
        wl = f"{c['wins']}-{c['losses']}"
        lines.append(f"{name:<20}{c['markets']:>5}{wl:>8}{wr:>9}{c['total_pnl']:>+14,.2f}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze a public Polymarket wallet: P&L and win rate by category."
    )
    parser.add_argument("--address", required=True,
                        help="Wallet address (0x-prefixed, 40 hex chars)")
    parser.add_argument("--trade-limit", type=int, default=2000,
                        help="Max trades to pull for history reconstruction (default 2000)")
    parser.add_argument("--category", type=str, default=None,
                        help="Filter the market list to one category (case-insensitive)")
    parser.add_argument("--enrich-tags", action="store_true",
                        help="Use Gamma event tags for more accurate categories (slower)")
    parser.add_argument("--top-markets", type=int, default=20,
                        help="How many per-market rows to include in JSON (default 20)")
    parser.add_argument("--output", choices=["json", "text"], default="json",
                        help="Output format (default json)")
    parser.add_argument("--rate-limit", type=int, default=100,
                        help="Min ms between API calls (default 100)")
    parser.add_argument("--debug", action="store_true",
                        help="Log every API call and short bodies to stderr")
    args = parser.parse_args()

    address = args.address.strip().lower()
    if not is_address(address):
        print(json.dumps({"error": f"invalid address: {args.address!r}"}), file=sys.stderr)
        sys.exit(2)

    api = APIClient(rate_limit_ms=args.rate_limit, debug=args.debug)
    enrich = api if args.enrich_tags else None

    try:
        log("Fetching positions...")
        positions = fetch_positions(api, address)
        log(f"  {len(positions)} position(s)")
        log("Fetching trade history...")
        trades = fetch_trades(api, address, args.trade_limit)
        log(f"  {len(trades)} trade(s)")
    except requests.RequestException as e:
        print(json.dumps({"error": f"API request failed: {e}"}), file=sys.stderr)
        sys.exit(1)

    if not positions and not trades:
        empty = {
            "address": address,
            "note": "No positions or trades found. Wallet may be empty, new, or "
                    "the Data API shape may differ — rerun with --debug.",
            "summary": {"overall": summarize([])["overall"], "by_category": {}},
            "markets": [],
        }
        _emit(empty, args)
        return

    trade_pnl = reconstruct_trade_pnl(trades)
    records = build_market_records(positions, trade_pnl, enrich)
    summary = summarize(records)

    if args.category:
        want = args.category.strip().lower()
        records = [r for r in records if r["category"].lower() == want]

    report = {
        "address": address,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "counts": {"positions": len(positions), "trades": len(trades),
                   "markets": summary["overall"]["markets"]},
        "summary": summary,
        "markets": records[: args.top_markets],
        "disclaimer": "Read-only public-data analysis. Not financial advice. "
                      "Win rate and realized P&L for fully-closed markets are "
                      "reconstructed from trade history and are estimates.",
    }
    _emit(report, args)


def _emit(report: dict, args) -> None:
    if args.output == "text":
        print(render_text(report))
    else:
        print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
