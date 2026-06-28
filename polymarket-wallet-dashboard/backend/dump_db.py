#!/usr/bin/env python3
"""Full dump (snapshot) of the Polymarket **Dashboard** (wallet-dashboard) databases.

By default it dumps every SQLite DB the dashboard uses (skipping any that don't exist):
  - ``wallets``            — DASHBOARD_WALLETS_DB   (~/.polymarket-wallet-dashboard/wallets.db):
                              watched wallets, tracking, baseline, per-wallet bets, tag cache.
  - ``soccer-predictions`` — SOCCER_PREDICTIONS_DB  (~/.polymarket-soccer/predictions.db)
  - ``tennis-predictions`` — TENNIS_PREDICTIONS_DB  (~/.polymarket-tennis/predictions.db)

For each DB it writes, into the output dir (default ``./dumps``), timestamped:
  - ``<label>.<UTCts>.db``  — a consistent BINARY snapshot (SQLite online backup API)
  - ``<label>.<UTCts>.sql`` — a full SQL dump (schema + data via iterdump): portable/restorable

Read-only & live-safe: each source is opened read-only and never modified. The ``.db`` snapshot
uses SQLite's backup API (consistent even if the backend is writing); the ``.sql`` is generated
from that snapshot.

    python dump_db.py                      # dump all 3 DBs -> ./dumps (.db + .sql each)
    python dump_db.py --out C:\\backups    # custom output dir
    python dump_db.py --sql-only           # only the .sql
    python dump_db.py --db-only            # only the .db snapshot
    python dump_db.py --gzip               # gzip the .sql (.sql.gz)
    python dump_db.py --db /path/wallets.db [--db /other.db]   # dump specific files instead

Restore:  sqlite3 restored.db < <label>.<ts>.sql      (or just copy the .db snapshot back)
"""

from __future__ import annotations

import argparse
import gzip
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def _defaults() -> list[tuple[str, str]]:
    return [
        ("wallets", os.environ.get(
            "DASHBOARD_WALLETS_DB",
            os.path.expanduser("~/.polymarket-wallet-dashboard/wallets.db"))),
        ("soccer-predictions", os.environ.get(
            "SOCCER_PREDICTIONS_DB", os.path.expanduser("~/.polymarket-soccer/predictions.db"))),
        ("tennis-predictions", os.environ.get(
            "TENNIS_PREDICTIONS_DB", os.path.expanduser("~/.polymarket-tennis/predictions.db"))),
    ]


def _table_counts(con: sqlite3.Connection) -> dict:
    out = {}
    for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                               "AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        out[name] = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
    return out


def dump_one(label: str, src: str, out_dir: str, ts: str, *,
             keep_db: bool = True, keep_sql: bool = True, gzip_sql: bool = False):
    """Snapshot one DB. Returns (table_counts, [files_written]). Source opened read-only."""
    snap = os.path.join(out_dir, f"{label}.{ts}.db")
    ro = sqlite3.connect(f"{Path(src).resolve().as_uri()}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(snap)
        try:
            ro.backup(dst)                     # consistent online snapshot (safe while live)
        finally:
            dst.close()
    finally:
        ro.close()

    made = []
    snapcon = sqlite3.connect(snap)
    try:
        counts = _table_counts(snapcon)
        if keep_sql:
            sql_path = os.path.join(out_dir, f"{label}.{ts}.sql" + (".gz" if gzip_sql else ""))
            opener = gzip.open if gzip_sql else open
            with opener(sql_path, "wt", encoding="utf-8") as fh:
                for line in snapcon.iterdump():
                    fh.write(line + "\n")
            made.append(sql_path)
    finally:
        snapcon.close()

    if keep_db:
        made.append(snap)
    else:
        os.remove(snap)
    return counts, made


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Dump completo dos BDs do Dashboard (wallets + previsões soccer/tennis).")
    ap.add_argument("--out", default="dumps", help="diretório de saída (default: ./dumps)")
    ap.add_argument("--db", action="append",
                    help="dump deste arquivo .db (repetível; default: os 3 bancos do dashboard)")
    ap.add_argument("--gzip", action="store_true", help="comprimir o .sql (.sql.gz)")
    fmt = ap.add_mutually_exclusive_group()
    fmt.add_argument("--sql-only", action="store_true", help="gerar só o .sql")
    fmt.add_argument("--db-only", action="store_true", help="gerar só o snapshot .db")
    args = ap.parse_args()

    targets = ([(os.path.splitext(os.path.basename(p))[0], p) for p in args.db]
               if args.db else _defaults())
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    os.makedirs(args.out, exist_ok=True)

    print(f"[dump_db] saída: {os.path.abspath(args.out)}  ({ts})")
    any_done = False
    for label, path in targets:
        if not os.path.isfile(path):
            print(f"[dump_db] pulado — BD não encontrado: {path}")
            continue
        counts, made = dump_one(label, path, args.out, ts, keep_db=not args.sql_only,
                                keep_sql=not args.db_only, gzip_sql=args.gzip)
        print(f"[dump_db] {label}: {path}")
        print("           tabelas: " + ("  ".join(f"{k}={v}" for k, v in counts.items()) or "(vazio)"))
        for f in made:
            print(f"           -> {f}  ({os.path.getsize(f):,} bytes)")
        any_done = True
    if not any_done:
        print("[dump_db] nada para dumpar (nenhum BD encontrado).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
