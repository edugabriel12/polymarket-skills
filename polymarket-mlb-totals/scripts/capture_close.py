#!/usr/bin/env python3
"""Snapshot the SHARP closing line for a day's MLB totals to a CSV (for CLV scoring).

CLV vs the sharp close (clv_vs_sharp.py) is the only metric that validates a real edge in
~50 bets — but it needs the sharp's CLOSING line, the price near first pitch. The Odds API
only serves the CURRENT line, so run this shortly before the games start (e.g. a cron a few
minutes before first pitch) to capture the close. It fetches the current Pinnacle/consensus
totals, devigs them to a fair Over/Under probability, and MERGES rows into a CSV with
close_over_odds/close_under_odds columns that clv_vs_sharp.py (via load_sharp_csv) reads.

Run it day after day against the SAME --out file to accumulate a season of closes in one
CSV, then score all recorded entries at once.

Pure CSV merge/round-trip is offline-testable; the fetch is best-effort (no key -> no-op).
The API key is never logged (sharp_odds redacts it).
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import datetime, timezone

import _bootstrap  # noqa: F401  (wires sys.path)

import sharp_odds
from category_common import log

# load_sharp_csv understands these columns; the fair probs (0<p<1) are written directly as
# the "odds" — american_to_implied accepts an implied prob as-is.
FIELDS = ["date", "away", "home", "total_line", "close_over_odds", "close_under_odds"]


def lookup_to_rows(lookup: dict, date: str) -> list[dict]:
    """Sharp lookup (from fetch_sharp) -> close CSV rows for `date`.

    Team order is alphabetical — matching is order-free (load_sharp_csv keys by the team
    SET) — and the devigged fair probs are written straight into the close odds columns.
    """
    rows = []
    for key, rec in lookup.items():
        d, teams = key
        if d != date:
            continue
        over, under, line = rec.get("over_fair"), rec.get("under_fair"), rec.get("line")
        if over is None or under is None or line is None:
            continue
        a, b = sorted(teams)
        rows.append({"date": d, "away": a, "home": b, "total_line": line,
                     "close_over_odds": round(float(over), 6),
                     "close_under_odds": round(float(under), 6)})
    return rows


def _row_key(row: dict) -> tuple:
    return (row.get("date", ""),
            frozenset((sharp_odds.normalize_team(row.get("away", "")),
                       sharp_odds.normalize_team(row.get("home", "")))))


def merge_rows(existing: list[dict], new: list[dict]) -> list[dict]:
    """Upsert `new` rows into `existing` keyed by (date, team-set); a fresh capture wins."""
    by_key = {_row_key(r): r for r in existing}
    for r in new:
        by_key[_row_key(r)] = r
    return list(by_key.values())


def read_csv(path: str) -> list[dict]:
    if not path or not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return [dict(r) for r in csv.DictReader(fh)]


def write_csv(path: str, rows: list[dict]) -> int:
    rows = sorted(rows, key=lambda r: (str(r.get("date")), str(r.get("away")), str(r.get("home"))))
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})
    return len(rows)


def capture(api_key, date: str, out_csv: str, book: str = "pinnacle", vlog=log) -> tuple[list, int]:
    """Fetch the current sharp totals, convert to close rows, merge into out_csv."""
    lookup = sharp_odds.fetch_sharp(api_key, date, book=book, vlog=vlog)
    new = lookup_to_rows(lookup, date)
    vlog(f"  captured {len(new)} sharp close row(s) for {date}")
    merged = merge_rows(read_csv(out_csv), new)
    n = write_csv(out_csv, merged)
    vlog(f"  wrote {n} total row(s) to {out_csv}")
    return new, n


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Snapshot the sharp CLOSING line to a CSV (run near first pitch) for CLV.")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (default today UTC)")
    ap.add_argument("--out", required=True, help="Close CSV to create/merge (accumulate a season here)")
    ap.add_argument("--odds-api-key", default=None, help="The Odds API key (or $ODDS_API_KEY)")
    ap.add_argument("--book", default="pinnacle", help="Sharp book key (default pinnacle)")
    a = ap.parse_args()
    date = a.date or datetime.now(timezone.utc).date().isoformat()
    new, _ = capture(a.odds_api_key, date, a.out)
    if not new:
        print("No sharp close captured (no key, no games, or API error) — see logs above.",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
