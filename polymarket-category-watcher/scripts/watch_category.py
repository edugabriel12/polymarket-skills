#!/usr/bin/env python3
"""Continuously LISTEN to all live Polymarket markets of a category.

Discovers every live market of a category (basketball, tennis, soccer, ...),
then polls their live midpoint prices on an interval, emitting JSON events to
stdout. Periodically re-discovers the category so newly-listed markets are
picked up and resolved ones are dropped — a long-running "listener" for a whole
category rather than a single token.

Self-contained — uses only `category_common.py` from this same skill folder.
Does not import or modify any other skill.

Event types emitted (one JSON object per line, stdout):
  - "snapshot": full price list for all tracked markets each cycle
  - "move":     a token whose midpoint moved >= --threshold since baseline
  - "added":    markets discovered on a re-scan that weren't tracked before
  - "removed":  markets no longer returned by discovery (resolved/closed)
Status/diagnostics go to stderr.

Usage:
    python watch_category.py --category basketball
    python watch_category.py --category tennis --interval 20 --threshold 3
    python watch_category.py --category futebol --min-volume 5000 --rescan-every 20
    python watch_category.py --category soccer --max-cycles 5      # finite, for tests

Ctrl-C to stop. CLAUDE.md rule #5: market text is untrusted and only displayed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone

import requests

from category_common import (
    APIClient,
    discover_markets,
    fetch_midpoint,
    resolve_category,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def emit(event: dict) -> None:
    """Write one event as a single JSON line to stdout."""
    print(json.dumps(event, ensure_ascii=False), flush=True)


def index_markets(markets: list[dict]) -> dict[str, dict]:
    """Index discovered markets by token_id (YES side = first token)."""
    idx: dict[str, dict] = {}
    for m in markets:
        if not m["token_ids"]:
            continue
        token = m["token_ids"][0]
        idx[token] = {
            "token_id": token,
            "question": m["question"],
            "slug": m["slug"],
            "volume_24h": m["volume_24h"],
        }
    return idx


def run(args) -> None:
    if args.tag:
        category_key, candidates = args.tag, [args.tag]
    else:
        category_key, candidates = resolve_category(args.category)

    api = APIClient(rate_limit_ms=args.rate_limit, debug=args.debug)

    def rediscover() -> dict[str, dict]:
        _tag, markets = discover_markets(
            api, category_key, candidates, min_volume=args.min_volume,
            max_markets=args.max_markets, include_closed=False,
        )
        return index_markets(markets)

    tracked = rediscover()
    baseline: dict[str, float] = {}
    emit({"event": "watch_started", "ts": now_iso(), "category": category_key,
          "tracked_markets": len(tracked), "interval_s": args.interval,
          "threshold_pct": args.threshold})

    cycle = 0
    try:
        while args.max_cycles == 0 or cycle < args.max_cycles:
            cycle += 1

            # Periodic re-discovery to catch new / removed markets.
            if cycle > 1 and args.rescan_every > 0 and cycle % args.rescan_every == 0:
                fresh = rediscover()
                added = [t for t in fresh if t not in tracked]
                removed = [t for t in tracked if t not in fresh]
                if added:
                    emit({"event": "added", "ts": now_iso(),
                          "markets": [fresh[t] for t in added]})
                if removed:
                    emit({"event": "removed", "ts": now_iso(),
                          "markets": [tracked[t] for t in removed]})
                    for t in removed:
                        baseline.pop(t, None)
                tracked = fresh

            snapshot = []
            for token, meta in tracked.items():
                mid = fetch_midpoint(api, token)
                if mid is None:
                    continue
                snapshot.append({"token_id": token, "question": meta["question"],
                                 "midpoint": round(mid, 4)})
                base = baseline.get(token)
                if base is None:
                    baseline[token] = mid
                elif base > 0:
                    change_pct = (mid - base) / base * 100.0
                    if abs(change_pct) >= args.threshold:
                        emit({"event": "move", "ts": now_iso(),
                              "token_id": token, "question": meta["question"],
                              "slug": meta["slug"], "from": round(base, 4),
                              "to": round(mid, 4), "change_pct": round(change_pct, 2)})
                        baseline[token] = mid  # reset baseline after an alert

            emit({"event": "snapshot", "ts": now_iso(), "cycle": cycle,
                  "tracked": len(tracked), "priced": len(snapshot),
                  "markets": snapshot})

            if args.max_cycles == 0 or cycle < args.max_cycles:
                time.sleep(args.interval)
    except KeyboardInterrupt:
        emit({"event": "watch_stopped", "ts": now_iso(), "cycles": cycle})


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Continuously listen to all live markets of a Polymarket category."
    )
    parser.add_argument("--category", type=str, default=None,
                        help="Category name or alias (basketball, tennis, soccer, ...)")
    parser.add_argument("--tag", type=str, default=None,
                        help="Explicit Gamma tag_slug, overrides --category mapping")
    parser.add_argument("--interval", type=int, default=30,
                        help="Seconds between price polls (default 30, min 5)")
    parser.add_argument("--threshold", type=float, default=5.0,
                        help="Midpoint move %% that triggers a 'move' event (default 5)")
    parser.add_argument("--min-volume", type=float, default=0.0,
                        help="Minimum 24h volume in USD to track a market (default 0)")
    parser.add_argument("--max-markets", type=int, default=None,
                        help="Cap how many markets to track (default: all)")
    parser.add_argument("--rescan-every", type=int, default=10,
                        help="Re-discover the category every N cycles (0 disables)")
    parser.add_argument("--max-cycles", type=int, default=0,
                        help="Stop after N cycles (0 = run forever)")
    parser.add_argument("--rate-limit", type=int, default=100,
                        help="Min ms between API calls (default 100)")
    parser.add_argument("--debug", action="store_true",
                        help="Log every API call to stderr")
    args = parser.parse_args()

    if not args.category and not args.tag:
        print(json.dumps({"error": "provide --category or --tag"}), file=sys.stderr)
        sys.exit(2)
    if args.interval < 5:
        args.interval = 5

    try:
        run(args)
    except requests.RequestException as e:
        print(json.dumps({"error": f"API request failed: {e}"}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
