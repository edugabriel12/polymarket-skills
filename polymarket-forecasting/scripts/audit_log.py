#!/usr/bin/env python3
"""Dump the full math audit (stats_log JSON) of every prediction, readably.

Reads the soccer predictions SQLite DB directly. Pure stdlib, no skill imports.

Examples:
  python audit_log.py                         # all soccer predictions, full audit
  python audit_log.py --status PENDENTE       # only pending
  python audit_log.py --date 2026-06-16       # one day
  python audit_log.py --id 7                   # a single prediction
  python audit_log.py --json > audit.json      # machine-readable dump
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

DEFAULT_DBS = {
    "soccer": os.path.expanduser("~/.polymarket-soccer/predictions.db"),
}

# Header fields shown above each audit (only those present in the row are printed).
HEADER_FIELDS = (
    "id", "game_slug", "league", "market", "side", "line", "entry_price",
    "decimal_odds", "model_prob", "edge", "size_usd", "size_pct", "confidence",
    "status", "game_date", "market_url",
)


def _header(row: sqlite3.Row) -> str:
    keys = row.keys()
    return "  ".join(f"{k}={row[k]}" for k in HEADER_FIELDS
                     if k in keys and row[k] not in (None, ""))


def dump(db: str, *, status=None, date=None, pid=None, limit=None, as_json=False) -> int:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    query, args, conds = "SELECT * FROM predictions", [], []
    if status:
        conds.append("UPPER(status)=?"); args.append(status.upper())
    if date:
        conds.append("game_date=?"); args.append(date)
    if pid is not None:
        conds.append("id=?"); args.append(pid)
    if conds:
        query += " WHERE " + " AND ".join(conds)
    query += " ORDER BY id"
    if limit:
        query += f" LIMIT {int(limit)}"
    rows = con.execute(query, args).fetchall()

    if as_json:
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["stats_log"] = json.loads(d.get("stats_log") or "null")
            except (TypeError, ValueError):
                pass
            out.append(d)
        print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return len(rows)

    for r in rows:
        print("\n" + "=" * 88)
        print(_header(r))
        try:
            audit = json.loads(r["stats_log"]) if r["stats_log"] else {}
        except (TypeError, ValueError):
            audit = {"_raw_unparsable": r["stats_log"]}
        print(json.dumps(audit, indent=2, ensure_ascii=False, default=str))
    print(f"\n{len(rows)} prediction(s) in {db}")
    return len(rows)


def main() -> None:
    p = argparse.ArgumentParser(description="Dump the full math audit (stats_log) of all predictions.")
    p.add_argument("--sport", choices=("soccer",), default="soccer",
                   help="Which store to read (default soccer)")
    p.add_argument("--db", default=None, help="Override the DB path (default: per --sport)")
    p.add_argument("--status", default=None, help="Filter by status (PENDENTE/ACERTO/ERRO/ANULADO)")
    p.add_argument("--date", default=None, help="Filter by game_date YYYY-MM-DD")
    p.add_argument("--id", type=int, default=None, help="A single prediction id")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--json", action="store_true", help="Emit a JSON array (machine-readable)")
    a = p.parse_args()

    db = a.db or DEFAULT_DBS[a.sport]
    if not os.path.exists(db):
        print(f"DB not found: {db}", file=sys.stderr)
        sys.exit(1)
    dump(db, status=a.status, date=a.date, pid=a.id, limit=a.limit, as_json=a.json)


if __name__ == "__main__":
    main()
