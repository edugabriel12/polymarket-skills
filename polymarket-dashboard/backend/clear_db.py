#!/usr/bin/env python3
"""Wipe the Polymarket **Sports** storefront database (entries.db).

The Sports backend keeps ALL its data in one SQLite file (default
``~/.polymarket-dashboard/entries.db``; override with ``SPORTS_ENTRIES_DB`` or ``--db``):

  - ``entries``  : every ingested bet/entry — OPEN cards + settled WON/LOST/VOID results.
  - ``settings`` : Telegram config (bot token + chat id).

Safe by design: it is a DRY-RUN by default (just prints the row counts and what it WOULD
do). Pass ``--apply`` to actually wipe, and a timestamped ``.bak`` copy of the DB is made
first so the change is reversible.

    python clear_db.py                            # dry-run: show counts, change nothing
    python clear_db.py --apply                    # COMPLETE wipe (entries + Telegram config) + backup
    python clear_db.py --apply --keep-telegram    # wipe bets/results, KEEP the Telegram config
    python clear_db.py --apply --db /path/to/entries.db

Restore: stop the backend and copy the ``.bak`` file back over ``entries.db``.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
from datetime import datetime, timezone

_TABLES = ("entries", "settings")


def _default_db() -> str:
    return os.environ.get(
        "SPORTS_ENTRIES_DB", os.path.expanduser("~/.polymarket-dashboard/entries.db"))


def _counts(con: sqlite3.Connection) -> dict:
    out = {}
    for t in _TABLES:
        try:
            out[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.OperationalError:
            out[t] = None          # table not present
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Completely wipe the Polymarket Sports DB (entries.db).")
    ap.add_argument("--db", default=_default_db(), help="path to entries.db")
    ap.add_argument("--apply", action="store_true", help="actually wipe (default: dry-run)")
    ap.add_argument("--keep-telegram", action="store_true",
                    help="preserve the Telegram config (the settings table)")
    args = ap.parse_args()

    db = args.db
    if not os.path.isfile(db):
        print(f"[clear_db] nothing to do — DB not found: {db}")
        return 0

    con = sqlite3.connect(db)
    try:
        before = _counts(con)
    finally:
        con.close()

    targets = ["entries"] if args.keep_telegram else list(_TABLES)
    scope = "bets/results only (keeping Telegram config)" if args.keep_telegram \
        else "COMPLETE — entries AND Telegram config"
    print(f"[clear_db] DB: {db}")
    print(f"[clear_db] current rows: " + "  ".join(f"{t}={before[t]}" for t in _TABLES))
    print(f"[clear_db] will wipe: {', '.join(targets)}  ({scope})")

    if not args.apply:
        print("[clear_db] DRY-RUN — nothing changed. Re-run with --apply to wipe.")
        return 0

    bak = f"{db}.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.bak"
    shutil.copy2(db, bak)
    print(f"[clear_db] backup -> {bak}")

    con = sqlite3.connect(db)
    try:
        with con:
            for t in targets:
                try:
                    con.execute(f"DELETE FROM {t}")
                except sqlite3.OperationalError:
                    pass
        con.execute("VACUUM")      # reclaim disk space after the deletes
        after = _counts(con)
    finally:
        con.close()

    print(f"[clear_db] done. rows now: " + "  ".join(f"{t}={after[t]}" for t in _TABLES))
    print("[clear_db] restart the Sports backend to serve the empty store.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
