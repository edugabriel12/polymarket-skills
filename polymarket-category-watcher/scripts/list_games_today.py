#!/usr/bin/env python3
"""List all baseball/MLB (or any sport) GAMES happening on a given day.

Polymarket lists sports games with a date-stamped slug, e.g.
`mlb-hou-kc-2026-06-13` (HOU @ KC on 2026-06-13), shown at
`https://polymarket.com/sports/mlb/<slug>`. This script discovers a sport's
markets via the Gamma API, keeps only those whose game date matches the target
day, and groups them into one row per game.

Self-contained — uses only `category_common.py` from this same skill folder.

Usage:
    python list_games_today.py                              # MLB games today (UTC)
    python list_games_today.py --date 2026-06-13            # a specific day
    python list_games_today.py --output text
    python list_games_today.py --upcoming-only              # drop games already started
    python list_games_today.py --category soccer --sport-path epl   # other sports
    python list_games_today.py --debug

Date handling: the game date is taken from the date embedded in the game slug
(authoritative), falling back to `gameStartTime`/`startDate` in UTC. The
default --date is today in UTC; pass --date to target another day or to avoid
UTC/local-day drift for late US games.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

import requests

from category_common import (
    APIClient,
    discover_markets,
    game_date,
    iso_date,
    resolve_category,
)


def is_moneyline(market: dict) -> bool:
    """Two distinct team outcomes => the head-to-head moneyline market."""
    outs = market.get("outcomes") or []
    return len(outs) == 2


def group_games(markets: list[dict], sport_path: str) -> list[dict]:
    """Collapse markets into one entry per game, keyed by event/game slug."""
    games: dict[str, dict] = {}
    for m in markets:
        key = m.get("event_slug") or m.get("slug") or m.get("condition_id")
        if not key:
            continue
        g = games.get(key)
        if g is None:
            game_slug = m.get("event_slug") or m.get("slug") or ""
            g = games[key] = {
                "game_slug": game_slug,
                "game_date": game_date(m),
                "game_start_time": m.get("game_start_time", ""),
                "url": f"https://polymarket.com/sports/{sport_path}/{game_slug}",
                "matchup": None,
                "teams": [],
                "moneyline_prices": [],
                "markets": [],
                "volume_24h": 0.0,
            }
        g["markets"].append({
            "question": m["question"],
            "slug": m["slug"],
            "outcomes": m["outcomes"],
            "outcome_prices": m["outcome_prices"],
            "token_ids": m["token_ids"],
            "volume_24h": m["volume_24h"],
        })
        g["volume_24h"] += m["volume_24h"]
        # Use the moneyline market as the game's headline.
        if g["matchup"] is None and is_moneyline(m):
            g["matchup"] = m["question"]
            g["teams"] = m["outcomes"]
            g["moneyline_prices"] = m["outcome_prices"]
        if not g["game_start_time"] and m.get("game_start_time"):
            g["game_start_time"] = m["game_start_time"]
    out = list(games.values())
    # Fall back to first market's question if no moneyline was found.
    for g in out:
        if g["matchup"] is None and g["markets"]:
            g["matchup"] = g["markets"][0]["question"]
    out.sort(key=lambda g: (g.get("game_start_time") or "", -g["volume_24h"]))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="List all games of a sport happening on a given day."
    )
    parser.add_argument("--date", type=str, default=None,
                        help="Target day YYYY-MM-DD (default: today in UTC)")
    parser.add_argument("--category", type=str, default="baseball",
                        help="Category/alias to discover (default: baseball)")
    parser.add_argument("--tag", type=str, default=None,
                        help="Explicit Gamma tag_slug, overrides --category")
    parser.add_argument("--sport-path", type=str, default="mlb",
                        help="URL path segment under /sports/ (default: mlb)")
    parser.add_argument("--min-volume", type=float, default=0.0,
                        help="Minimum 24h volume in USD per market (default 0)")
    parser.add_argument("--include-closed", action="store_true",
                        help="Also include closed/resolved markets")
    parser.add_argument("--upcoming-only", action="store_true",
                        help="Drop games whose start time is already in the past")
    parser.add_argument("--output", choices=["json", "text"], default="json",
                        help="Output format (default json)")
    parser.add_argument("--rate-limit", type=int, default=100,
                        help="Min ms between API calls (default 100)")
    parser.add_argument("--debug", action="store_true",
                        help="Log every API call to stderr")
    args = parser.parse_args()

    target = args.date or datetime.now(timezone.utc).date().isoformat()

    if args.tag:
        category_key, candidates = args.tag, [args.tag]
    else:
        category_key, candidates = resolve_category(args.category)

    api = APIClient(rate_limit_ms=args.rate_limit, debug=args.debug)
    try:
        tag_used, markets = discover_markets(
            api, category_key, candidates,
            min_volume=args.min_volume,
            include_closed=args.include_closed,
        )
    except requests.RequestException as e:
        print(json.dumps({"error": f"API request failed: {e}"}), file=sys.stderr)
        sys.exit(1)

    on_day = [m for m in markets if game_date(m) == target]
    games = group_games(on_day, args.sport_path)

    if args.upcoming_only:
        now_iso = datetime.now(timezone.utc).isoformat()
        games = [g for g in games
                 if not g["game_start_time"] or g["game_start_time"] >= now_iso]

    if args.output == "text":
        print(_render_text(target, category_key, tag_used, games))
    else:
        print(json.dumps({
            "date": target,
            "category": category_key,
            "tag_used": tag_used,
            "count": len(games),
            "games": games,
        }, indent=2, ensure_ascii=False))


def _render_text(target: str, category_key: str, tag_used: str,
                 games: list[dict]) -> str:
    lines = [
        f"{category_key} games on {target}  (tag: {tag_used})",
        f"Games: {len(games)}",
        "-" * 72,
    ]
    for g in games:
        when = iso_date(g["game_start_time"]) and g["game_start_time"][11:16] or "--:--"
        prices = g["moneyline_prices"]
        if len(prices) == 2 and len(g["teams"]) == 2:
            odds = f"  [{g['teams'][0]} {prices[0]:.2f} / {g['teams'][1]} {prices[1]:.2f}]"
        else:
            odds = ""
        lines.append(f"{when} UTC  {g['matchup']}{odds}")
        lines.append(f"          {g['url']}")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
