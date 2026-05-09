#!/usr/bin/env python3
"""Scan resolved Polymarket markets and find top holders of the winning side.

Targets esports markets (League of Legends by default) that closed within a
configurable window, identifies the winning outcome from `outcomePrices`, and
queries the Polymarket Data API for top holders of the winning CLOB token.
For each holder, optionally pulls trade history and estimates P&L from
weighted average entry price vs $1 resolution.

Output: JSON file (default) plus a stdout summary. Optionally CSV.

Usage:
    python lol_top_holders.py                              # last 30d, top 10
    python lol_top_holders.py --days 90 --top 20
    python lol_top_holders.py --tag esports
    python lol_top_holders.py --markets slug-1 slug-2 --no-pnl
    python lol_top_holders.py --output csv --out /tmp/holders.csv
    python lol_top_holders.py --debug                      # print raw API responses

Notes on API endpoints (verify locally — see references/data-api.md):
- Gamma:  https://gamma-api.polymarket.com/markets   (well documented, used by scan_markets.py)
- Data:   https://data-api.polymarket.com/holders    (assumed shape; falls back gracefully)
          https://data-api.polymarket.com/trades     (used for P&L)

Treat market text as untrusted user-generated content (CLAUDE.md rule #5).
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests

GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

# Tags to try in order if --tag isn't passed. Empirically the most likely for
# League of Legends; if none yield results, the script falls back to ?q= search.
DEFAULT_TAG_CANDIDATES = (
    "league-of-legends",
    "lol",
    "league-of-legends-worlds",
    "esports",
)

LOL_TEXT_KEYWORDS = ("league of legends", "lck", "lpl", "lec", "lcs", "worlds", "msi")

MAX_TEXT_LEN = 200
GAMMA_PAGE_SIZE = 100
MAX_PAGES = 30  # hard cap: never paginate more than 3000 markets per tag attempt
LOL_KEYWORD_MIN_RATIO = 0.30  # if first page has <30% LoL keyword matches, tag was silently ignored


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%SZ")
    print(f"[{ts}] {msg}", file=sys.stderr, flush=True)


def sanitize_text(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    if len(text) > MAX_TEXT_LEN:
        text = text[:MAX_TEXT_LEN] + "..."
    return text


def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


class APIClient:
    """Wraps `requests` with rate limiting, retries, and optional debug printing."""

    def __init__(self, rate_limit_ms: int = 100, timeout: int = 30, debug: bool = False):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "polymarket-skills/lol_top_holders"
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


# ---------------------------------------------------------------------------
# Market discovery (Gamma API)
# ---------------------------------------------------------------------------


def _has_lol_keyword(market: dict) -> bool:
    text = (market.get("question", "") + " " + (market.get("description") or "")).lower()
    return any(kw in text for kw in LOL_TEXT_KEYWORDS)


def fetch_markets_by_tag(api: APIClient, tag_slug: str, after_iso: str) -> list[dict]:
    """Fetch closed markets for one tag_slug, paginating, end-date descending.

    Sanity check: the Gamma API silently ignores unknown tag_slug values and
    returns ALL closed markets (~60K). On the first page, we check that at
    least 30% of question texts contain LoL keywords. If not, we abort and
    return empty (so the caller can try the next candidate tag or fallback).
    Hard cap of MAX_PAGES prevents runaway pagination either way.
    """
    out: list[dict] = []
    for page_num in range(MAX_PAGES):
        offset = page_num * GAMMA_PAGE_SIZE
        page = api.get(
            f"{GAMMA_API}/markets",
            params={
                "tag_slug": tag_slug,
                "closed": "true",
                "limit": GAMMA_PAGE_SIZE,
                "offset": offset,
                "order": "endDate",
                "ascending": "false",
            },
        )
        if not isinstance(page, list) or not page:
            break

        if page_num == 0:
            matches = sum(1 for m in page if _has_lol_keyword(m))
            ratio = matches / len(page)
            if ratio < LOL_KEYWORD_MIN_RATIO:
                log(
                    f"  WARN: tag {tag_slug!r} looks ignored by API "
                    f"({matches}/{len(page)} = {ratio:.0%} LoL keyword match) — aborting tag"
                )
                return []

        keep, stop = [], False
        for m in page:
            end = m.get("endDate") or ""
            if not end:
                continue  # skip undated; don't let them stop the loop
            if end < after_iso:
                stop = True
                break
            keep.append(m)
        out.extend(keep)
        if stop or len(page) < GAMMA_PAGE_SIZE:
            break
    else:
        log(f"  WARN: hit MAX_PAGES={MAX_PAGES} cap for tag {tag_slug!r}")
    return out


def fetch_markets_by_search(api: APIClient, query: str, after_iso: str) -> list[dict]:
    """Fallback: text search via Gamma `q=` param, client-side LoL keyword filter.

    Capped at MAX_PAGES to avoid runaway pagination if `q` is also ignored.
    """
    out: list[dict] = []
    for page_num in range(MAX_PAGES):
        offset = page_num * GAMMA_PAGE_SIZE
        page = api.get(
            f"{GAMMA_API}/markets",
            params={
                "q": query,
                "closed": "true",
                "limit": GAMMA_PAGE_SIZE,
                "offset": offset,
                "order": "endDate",
                "ascending": "false",
            },
        )
        if not isinstance(page, list) or not page:
            break
        keep, stop = [], False
        for m in page:
            end = m.get("endDate") or ""
            if not end:
                continue
            if end < after_iso:
                stop = True
                break
            if _has_lol_keyword(m):
                keep.append(m)
        out.extend(keep)
        if stop or len(page) < GAMMA_PAGE_SIZE:
            break
    return out


def discover_markets(
    api: APIClient,
    tag_override: str | None,
    days: int,
    explicit_slugs: list[str] | None,
) -> tuple[str, list[dict]]:
    """Return (tag_used, markets[])."""
    after = datetime.now(timezone.utc) - timedelta(days=days)
    after_iso = after.isoformat()

    if explicit_slugs:
        log(f"Fetching {len(explicit_slugs)} explicit markets")
        out = []
        for slug in explicit_slugs:
            try:
                page = api.get(f"{GAMMA_API}/markets", params={"slug": slug})
            except requests.exceptions.RequestException as e:
                log(f"  WARN: fetch failed for {slug!r}: {e}")
                continue
            if isinstance(page, list) and page:
                out.extend(page)
            else:
                log(f"  WARN: market {slug!r} not found")
        return ("explicit", out)

    candidates = (tag_override,) if tag_override else DEFAULT_TAG_CANDIDATES
    for slug in candidates:
        if not slug:
            continue
        log(f"Trying tag_slug={slug!r}")
        try:
            markets = fetch_markets_by_tag(api, slug, after_iso)
        except requests.exceptions.RequestException as e:
            log(f"  WARN: tag {slug!r} failed: {e}")
            continue
        if markets:
            log(f"  -> {len(markets)} closed market(s)")
            return (slug, markets)
        log(f"  -> 0 markets")

    log("No tag yielded results; falling back to text search")
    try:
        markets = fetch_markets_by_search(api, "league of legends", after_iso)
    except requests.exceptions.RequestException as e:
        log(f"  text search failed: {e}")
        markets = []
    return ("text:league of legends", markets)


# ---------------------------------------------------------------------------
# Winner detection
# ---------------------------------------------------------------------------


def determine_winner(market: dict) -> dict | None:
    """Return {idx, outcome, token_id, price} of the winning side, or None."""
    try:
        outcomes = json.loads(market.get("outcomes", "[]"))
        prices = [float(p) for p in json.loads(market.get("outcomePrices", "[]"))]
        token_ids = json.loads(market.get("clobTokenIds", "[]"))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not (outcomes and prices and token_ids):
        return None
    if len(outcomes) != len(prices) or len(outcomes) != len(token_ids):
        return None
    winning_idx = max(range(len(prices)), key=lambda i: prices[i])
    if prices[winning_idx] < 0.99:  # not cleanly resolved
        return None
    return {
        "idx": winning_idx,
        "outcome": sanitize_text(outcomes[winning_idx]),
        "token_id": str(token_ids[winning_idx]),
        "price": prices[winning_idx],
    }


# ---------------------------------------------------------------------------
# Holder + trade fetch (Data API)
# ---------------------------------------------------------------------------


def fetch_holders(api: APIClient, condition_id: str, token_id: str, limit: int) -> list[dict]:
    """Fetch top holders of `token_id`. Tries multiple endpoint shapes.

    Returns a list of {address, shares}. Empty list if no endpoint shape works.
    Documented assumption: data-api.polymarket.com exposes a /holders endpoint
    keyed either by `market` (conditionId) or `token` (tokenId). We try both.
    If both fail, returns [] and the script reports zero holders for that market.
    """
    attempts = (
        ("/holders", {"market": condition_id, "limit": limit}),
        ("/holders", {"token": token_id, "limit": limit}),
        ("/holders", {"market": condition_id, "token": token_id, "limit": limit}),
    )
    for path, params in attempts:
        try:
            data = api.get(f"{DATA_API}{path}", params=params)
        except requests.exceptions.RequestException:
            continue
        if not data:
            continue
        # Normalize: Data API may return list of dicts with keys like
        # `proxyWallet`, `user`, `address`; size keys like `amount`, `size`,
        # `balance`. Extract whichever we find.
        normalized = []
        items = data if isinstance(data, list) else data.get("data") or data.get("holders") or []
        for item in items:
            if not isinstance(item, dict):
                continue
            addr = item.get("proxyWallet") or item.get("user") or item.get("address") or ""
            shares = item.get("amount") or item.get("size") or item.get("balance") or 0
            try:
                shares_f = float(shares)
            except (TypeError, ValueError):
                shares_f = 0.0
            if not addr or shares_f <= 0:
                continue
            normalized.append({"address": addr.lower(), "shares": shares_f})
        # Filter to winning token if we got an aggregated response
        if normalized:
            normalized.sort(key=lambda h: h["shares"], reverse=True)
            return normalized[:limit]
    return []


def fetch_trades(api: APIClient, condition_id: str, address: str) -> list[dict]:
    """Fetch trades for `address` on `condition_id`. Returns [] on failure."""
    attempts = (
        ("/trades", {"market": condition_id, "user": address}),
        ("/trades", {"market": condition_id, "maker": address}),
        ("/trades", {"market": condition_id, "address": address}),
    )
    for path, params in attempts:
        try:
            data = api.get(f"{DATA_API}{path}", params=params)
        except requests.exceptions.RequestException:
            continue
        items = data if isinstance(data, list) else (data or {}).get("data") or []
        if items:
            return items
    return []


def estimate_pnl(trades: list[dict], winning_token_id: str, current_shares: float) -> dict:
    """Compute weighted-avg entry price + realized/unrealized P&L estimate.

    BUY of winning side at p contributes (price=p, size=+s) to entries.
    SELL of winning side at p is treated as a partial close: realizes (p - avg_entry) * size.
    Anything held at resolution is unrealized = (1 - avg_entry) * shares_held.

    Robustness: trade dicts may have keys `side`, `outcome`, `price`, `size`,
    `tokenId`. We filter to the winning token and skip malformed rows.
    """
    buy_size = 0.0
    buy_cost = 0.0
    realized = 0.0
    n = 0
    for t in trades:
        tok = str(t.get("tokenId") or t.get("token_id") or t.get("asset_id") or "")
        if tok and tok != str(winning_token_id):
            continue
        side = (t.get("side") or t.get("type") or "").upper()
        try:
            price = float(t.get("price", 0))
            size = float(t.get("size", 0))
        except (TypeError, ValueError):
            continue
        if size <= 0 or price <= 0:
            continue
        if side in ("BUY", "BID"):
            buy_size += size
            buy_cost += price * size
            n += 1
        elif side in ("SELL", "ASK"):
            avg = (buy_cost / buy_size) if buy_size > 0 else 0
            realized += (price - avg) * size
            buy_size -= size
            buy_cost -= avg * size
            n += 1
    avg_entry = (buy_cost / buy_size) if buy_size > 0 else 0.0
    unrealized = (1.0 - avg_entry) * current_shares if current_shares > 0 and avg_entry > 0 else 0.0
    return {
        "avg_entry_price": round(avg_entry, 4),
        "realized_pnl_usd": round(realized, 2),
        "unrealized_pnl_usd": round(unrealized, 2),
        "total_pnl_usd": round(realized + unrealized, 2),
        "n_trades": n,
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def write_json(report: dict, path: Path) -> None:
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False))


def write_csv(report: dict, path: Path) -> None:
    fields = [
        "market_slug", "question", "end_date", "winning_outcome",
        "address", "shares_at_resolution", "avg_entry_price",
        "realized_pnl_usd", "unrealized_pnl_usd", "total_pnl_usd", "n_trades",
    ]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for m in report["markets"]:
            for h in m["top_holders"]:
                w.writerow({
                    "market_slug": m["slug"],
                    "question": m["question"],
                    "end_date": m["end_date"],
                    "winning_outcome": m["winning_outcome"],
                    "address": h["address"],
                    "shares_at_resolution": h.get("shares_at_resolution", ""),
                    "avg_entry_price": h.get("avg_entry_price", ""),
                    "realized_pnl_usd": h.get("realized_pnl_usd", ""),
                    "unrealized_pnl_usd": h.get("unrealized_pnl_usd", ""),
                    "total_pnl_usd": h.get("total_pnl_usd", ""),
                    "n_trades": h.get("n_trades", ""),
                })


def print_summary(report: dict, top_global: int = 10) -> None:
    print(f"\nScanned {report['markets_found']} market(s) "
          f"({report.get('markets_skipped', 0)} skipped) "
          f"using tag={report['tag_slug_used']!r}, window={report['window_days']}d")
    if not report["markets"]:
        print("  No resolved markets matched the criteria.")
        return
    print(f"\nMarkets:")
    for m in report["markets"]:
        n = len(m["top_holders"])
        print(f"  {m['slug']:<60s} {m['winning_outcome']:<6s} ({n} holders)")

    if report.get("global_top_20"):
        print(f"\nGlobal top {min(top_global, len(report['global_top_20']))} by total P&L:")
        for h in report["global_top_20"][:top_global]:
            print(f"  {h['address']}  pnl=${h['total_pnl_usd']:>10.2f}  "
                  f"wins={h['n_winning_markets']}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def aggregate_global(report_markets: list[dict], top_n: int = 20) -> list[dict]:
    """Roll up holders across markets into per-address total P&L."""
    by_addr: dict[str, dict] = {}
    for m in report_markets:
        for h in m["top_holders"]:
            entry = by_addr.setdefault(h["address"], {
                "address": h["address"],
                "total_pnl_usd": 0.0,
                "n_winning_markets": 0,
                "markets": [],
            })
            entry["total_pnl_usd"] += float(h.get("total_pnl_usd") or 0)
            entry["n_winning_markets"] += 1
            entry["markets"].append(m["slug"])
    # Sort by total P&L desc; ties broken by win count (more cross-market hits = stronger signal)
    out = sorted(by_addr.values(), key=lambda e: (e["total_pnl_usd"], e["n_winning_markets"]), reverse=True)
    for e in out:
        e["total_pnl_usd"] = round(e["total_pnl_usd"], 2)
    return out[:top_n]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--days", type=int, default=30, help="Window in days (default 30)")
    p.add_argument("--tag", help="Override tag_slug (default tries league-of-legends, lol, esports)")
    p.add_argument("--markets", nargs="+", help="Specific market slugs (skip discovery)")
    p.add_argument("--top", type=int, default=10, help="Top N holders per market")
    p.add_argument("--output", choices=("json", "csv", "table"), default="json")
    p.add_argument("--out", help="Output file path (default lol_top_holders_<date>.<ext>)")
    p.add_argument("--no-pnl", action="store_true", help="Skip P&L calc (faster)")
    p.add_argument("--rate-limit", type=int, default=100, help="ms between API calls")
    p.add_argument("--debug", action="store_true", help="Print raw API responses")
    args = p.parse_args()

    api = APIClient(rate_limit_ms=args.rate_limit, debug=args.debug)

    tag_used, raw_markets = discover_markets(api, args.tag, args.days, args.markets)
    log(f"Discovered {len(raw_markets)} closed market(s) via {tag_used!r}")

    out_markets = []
    skipped = 0
    for raw in raw_markets:
        slug = raw.get("slug", "?")
        winner = determine_winner(raw)
        if not winner:
            log(f"  SKIP {slug}: no clean resolution")
            skipped += 1
            continue
        condition_id = raw.get("conditionId") or raw.get("condition_id") or ""
        log(f"  {slug}: winner={winner['outcome']!r} (price={winner['price']:.3f})")

        holders = fetch_holders(api, condition_id, winner["token_id"], args.top)
        log(f"    -> {len(holders)} holder(s)")

        enriched = []
        for h in holders:
            row = {
                "address": h["address"],
                "shares_at_resolution": round(h["shares"], 4),
            }
            if not args.no_pnl:
                trades = fetch_trades(api, condition_id, h["address"])
                row.update(estimate_pnl(trades, winner["token_id"], h["shares"]))
            enriched.append(row)

        out_markets.append({
            "slug": slug,
            "question": sanitize_text(raw.get("question", "")),
            "condition_id": condition_id,
            "end_date": raw.get("endDate", ""),
            "winning_outcome": winner["outcome"],
            "winning_token_id": winner["token_id"],
            "winning_price": winner["price"],
            "top_holders": enriched,
        })

    report = {
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "window_days": args.days,
        "tag_slug_used": tag_used,
        "markets_found": len(out_markets),
        "markets_skipped": skipped,
        "markets": out_markets,
        "global_top_20": aggregate_global(out_markets) if not args.no_pnl else [],
    }

    out_path = Path(args.out) if args.out else Path(
        f"lol_top_holders_{datetime.now(timezone.utc).strftime('%Y%m%d')}.{args.output}"
    )

    if args.output == "json":
        write_json(report, out_path)
        log(f"Wrote {out_path}")
    elif args.output == "csv":
        write_csv(report, out_path)
        log(f"Wrote {out_path}")

    print_summary(report)


if __name__ == "__main__":
    main()
