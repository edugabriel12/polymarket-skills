#!/usr/bin/env python3
"""Export the prediction database to a portable JSON (or CSV) you can share.

Pure stdlib, no skill imports (works for the MLB and soccer stores). Dumps the
`predictions` and `model_log` tables to a single JSON file by default — the format
that preserves the nested stats/audit JSON and is easiest to analyze. Use --csv to
write one CSV per table instead, and --compact to drop the big stats_log/model_params
blobs for a lighter dump.

Examples:
  python export_db.py                                  # MLB store -> mlb_export.json
  python export_db.py --sport soccer --out soccer.json
  python export_db.py --compact --status ACERTO,ERRO   # only settled, no audit blobs
  python export_db.py --csv --out dump                 # -> dump_predictions.csv, dump_model_log.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys

DEFAULT_DBS = {
    "mlb": os.path.expanduser("~/.polymarket-mlb-totals/predictions.db"),
    "soccer": os.path.expanduser("~/.polymarket-soccer/predictions.db"),
}
TABLES = ("predictions", "model_log")
# Heavy JSON blobs dropped by --compact (kept by default).
_BLOB_COLS = {"stats_log", "model_params"}


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    return con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                       (table,)).fetchone() is not None


def dump_table(con: sqlite3.Connection, table: str, *, statuses=None, compact=False) -> list[dict]:
    if not _table_exists(con, table):
        return []
    rows = [dict(r) for r in con.execute(f"SELECT * FROM {table}")]
    if statuses:
        keep = {s.strip().upper() for s in statuses}
        rows = [r for r in rows if str(r.get("status", "")).upper() in keep]
    if compact:
        for r in rows:
            for c in _BLOB_COLS:
                r.pop(c, None)
    return rows


def export(db_path: str, *, statuses=None, compact=False) -> dict:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        return {t: dump_table(con, t, statuses=statuses, compact=compact) for t in TABLES}
    finally:
        con.close()


def write_json(data: dict, out: str) -> None:
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False, default=str)


def write_csv(data: dict, out_base: str) -> list[str]:
    written = []
    base, _ext = os.path.splitext(out_base)
    for table, rows in data.items():
        if not rows:
            continue
        path = f"{base}_{table}.csv"
        cols = list({k for r in rows for k in r})
        # Stable, readable column order: id/date-ish first, then the rest.
        preferred = [c for c in ("id", "game_slug", "game_date", "line", "side",
                                 "entry_price", "edge", "status") if c in cols]
        cols = preferred + [c for c in cols if c not in preferred]
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in cols})
        written.append(path)
    return written


def summarize(data: dict) -> str:
    lines = []
    for table, rows in data.items():
        by_status: dict[str, int] = {}
        for r in rows:
            s = str(r.get("status", "?"))
            by_status[s] = by_status.get(s, 0) + 1
        breakdown = ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())) or "(empty)"
        lines.append(f"  {table}: {len(rows)} row(s)  [{breakdown}]")
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description="Export the prediction DB to JSON/CSV to share.")
    p.add_argument("--sport", choices=("mlb", "soccer"), default="mlb")
    p.add_argument("--db", default=None, help="Override the DB path (else the sport default)")
    p.add_argument("--out", default=None, help="Output file (default <sport>_export.json)")
    p.add_argument("--csv", action="store_true", help="Write one CSV per table instead of JSON")
    p.add_argument("--compact", action="store_true", help="Drop stats_log/model_params blobs")
    p.add_argument("--status", default=None,
                   help="Comma-separated status filter (e.g. ACERTO,ERRO,PENDENTE)")
    a = p.parse_args()

    db_path = a.db or DEFAULT_DBS[a.sport]
    if not os.path.exists(db_path):
        print(f"DB not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    statuses = a.status.split(",") if a.status else None
    data = export(db_path, statuses=statuses, compact=a.compact)

    out = a.out or f"{a.sport}_export.{'csv' if a.csv else 'json'}"
    if a.csv:
        written = write_csv(data, out)
        print(f"Wrote {len(written)} CSV file(s):")
        for w in written:
            print(f"  {w}")
    else:
        write_json(data, out)
        print(f"Wrote {out}")
    print(f"Source DB: {db_path}")
    print(summarize(data))


if __name__ == "__main__":
    main()
