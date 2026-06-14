#!/usr/bin/env python3
"""List ALL live Polymarket markets for a given category (basketball, tennis,
soccer, ...), paginating through the Gamma API so the result is not capped at
one page.

Self-contained — uses only `category_common.py` from this same skill folder.
Does not import or modify any other skill.

Usage:
    python list_category_markets.py --category basketball
    python list_category_markets.py --category tennis --min-volume 10000
    python list_category_markets.py --category futebol --output text
    python list_category_markets.py --tag nba --max-markets 50
    python list_category_markets.py --category soccer --output text --debug

Output: JSON (default) or a human-readable table (--output text). The JSON
includes per-market token_ids, which feed directly into watch_category.py.
"""

from __future__ import annotations

import argparse
import json
import sys

import requests

from category_common import APIClient, discover_markets, resolve_category


def main() -> None:
    parser = argparse.ArgumentParser(
        description="List all live Polymarket markets for a category."
    )
    parser.add_argument("--category", type=str, default=None,
                        help="Category name or alias (basketball, tennis, soccer, "
                             "futebol, basquete, nba, lol, ...)")
    parser.add_argument("--tag", type=str, default=None,
                        help="Explicit Gamma tag_slug, overrides --category mapping")
    parser.add_argument("--min-volume", type=float, default=0.0,
                        help="Minimum 24h volume in USD (default 0)")
    parser.add_argument("--max-markets", type=int, default=None,
                        help="Cap the number of markets returned (default: all)")
    parser.add_argument("--include-closed", action="store_true",
                        help="Also include closed/resolved markets")
    parser.add_argument("--output", choices=["json", "text"], default="json",
                        help="Output format (default json)")
    parser.add_argument("--rate-limit", type=int, default=100,
                        help="Min ms between API calls (default 100)")
    parser.add_argument("--debug", action="store_true",
                        help="Log every API call to stderr")
    args = parser.parse_args()

    if not args.category and not args.tag:
        print(json.dumps({"error": "provide --category or --tag"}), file=sys.stderr)
        sys.exit(2)

    if args.tag:
        category_key, candidates = args.tag, [args.tag]
    else:
        category_key, candidates = resolve_category(args.category)

    api = APIClient(rate_limit_ms=args.rate_limit, debug=args.debug)
    try:
        tag_used, markets = discover_markets(
            api, category_key, candidates,
            min_volume=args.min_volume,
            max_markets=args.max_markets,
            include_closed=args.include_closed,
        )
    except requests.RequestException as e:
        print(json.dumps({"error": f"API request failed: {e}"}), file=sys.stderr)
        sys.exit(1)

    if args.output == "text":
        print(_render_text(category_key, tag_used, markets))
    else:
        print(json.dumps({
            "category": category_key,
            "tag_used": tag_used,
            "count": len(markets),
            "markets": markets,
        }, indent=2, ensure_ascii=False))


def _render_text(category_key: str, tag_used: str, markets: list[dict]) -> str:
    lines = [
        f"Category: {category_key}  (tag: {tag_used})",
        f"Live markets: {len(markets)}",
        "-" * 72,
    ]
    for m in markets:
        vol = m["volume_24h"]
        price = m["outcome_prices"][0] if m["outcome_prices"] else None
        price_s = f"{price:.2f}" if price is not None else " n/a"
        lines.append(f"[{price_s}] ${vol:>12,.0f}/24h  {m['question']}")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
