#!/usr/bin/env python3
"""Delete only the TENNIS rows from the dashboards' databases (the tennis model was removed,
so its leftover entries are stale).

Targets — each cleaned independently, skipped if the file doesn't exist:
  - Sports ``entries`` (default ``~/.polymarket-dashboard/entries.db``; env ``SPORTS_ENTRIES_DB``
    or ``--db``): the "Entradas"/"Resultados" rows with category Tennis.
  - (opt-in, ``--include-wallets``) Wallet-dashboard ``wallet_bets`` (default
    ``~/.polymarket-wallet-dashboard/wallets.db``; env ``DASHBOARD_WALLETS_DB`` or ``--wallets-db``):
    tennis bets a WATCHED WALLET made. These are real copy-trade data (not the model), so they are
    NOT touched unless you ask — use this only if you want zero tennis anywhere.

Only rows whose category is Tennis/Tênis are removed; everything else is untouched.

Safe by design: DRY-RUN by default (just prints how many tennis rows it WOULD delete). Pass
``--apply`` to delete; a timestamped ``.bak`` copy of each touched DB is made first, so it's
reversible (stop the backend and copy the ``.bak`` back).

    python clear_tennis.py                              # dry-run, Sports entries.db only
    python clear_tennis.py --apply                      # delete tennis from Sports + backup
    python clear_tennis.py --include-wallets            # dry-run, Sports + wallet_bets
    python clear_tennis.py --include-wallets --apply    # delete tennis from both + backups
    python clear_tennis.py --apply --db /path/entries.db

NB: the orphaned tennis predictions store (``~/.polymarket-tennis/predictions.db``) is no longer
read by anything — just delete that folder by hand if you want it gone.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
from datetime import datetime, timezone

# (label, db path, table, category column). Tennis is matched case-insensitively below.
_TENNIS_CATEGORIES = ("tennis", "tênis")


def _sports_db() -> str:
    return os.environ.get(
        "SPORTS_ENTRIES_DB", os.path.expanduser("~/.polymarket-dashboard/entries.db"))


def _wallets_db() -> str:
    return os.environ.get(
        "DASHBOARD_WALLETS_DB", os.path.expanduser("~/.polymarket-wallet-dashboard/wallets.db"))


_WHERE = "lower(category) IN (%s)" % ",".join("?" * len(_TENNIS_CATEGORIES))


def _count_tennis(con: sqlite3.Connection, table: str) -> int | None:
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table} WHERE {_WHERE}",
                           _TENNIS_CATEGORIES).fetchone()[0]
    except sqlite3.OperationalError:
        return None          # table not present


def _clean_one(label: str, db: str, table: str, apply: bool) -> None:
    if not os.path.isfile(db):
        print(f"[clear_tennis] {label}: DB not found, skipping ({db})")
        return
    con = sqlite3.connect(db)
    try:
        n = _count_tennis(con, table)
    finally:
        con.close()
    if n is None:
        print(f"[clear_tennis] {label}: table '{table}' not present, skipping ({db})")
        return
    print(f"[clear_tennis] {label}: {n} tennis row(s) in {table}  ({db})")
    if n == 0 or not apply:
        return
    bak = f"{db}.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.bak"
    shutil.copy2(db, bak)
    print(f"[clear_tennis] {label}: backup -> {bak}")
    con = sqlite3.connect(db)
    try:
        with con:
            con.execute(f"DELETE FROM {table} WHERE {_WHERE}", _TENNIS_CATEGORIES)
        con.execute("VACUUM")          # reclaim disk after the deletes
        left = _count_tennis(con, table)
    finally:
        con.close()
    print(f"[clear_tennis] {label}: deleted {n} row(s); tennis remaining = {left}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Delete only the tennis rows from the dashboard DBs.")
    ap.add_argument("--db", default=_sports_db(), help="Sports entries.db path")
    ap.add_argument("--include-wallets", action="store_true",
                    help="also delete tennis bets from the wallet-dashboard wallet_bets (real "
                         "copy-trade data — only if you want zero tennis anywhere)")
    ap.add_argument("--wallets-db", default=_wallets_db(), help="wallet-dashboard wallets.db path")
    ap.add_argument("--apply", action="store_true", help="actually delete (default: dry-run)")
    args = ap.parse_args()

    _clean_one("Sports", args.db, "entries", args.apply)
    if args.include_wallets:
        _clean_one("Wallets", args.wallets_db, "wallet_bets", args.apply)

    if not args.apply:
        print("[clear_tennis] DRY-RUN — nothing changed. Re-run with --apply to delete.")
    else:
        print("[clear_tennis] done. Restart the affected backend(s) to serve the cleaned data.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
