#!/usr/bin/env python3
"""Reset the prediction database(s) — clear all recorded predictions (and the
analysis cache), so you can start fresh after a model change.

Pure stdlib, no skill imports (works for both the MLB and soccer stores). By
default it DELETEs the rows (keeps the file/schema); --delete-file removes the
file entirely (it's recreated empty on the next run). Destructive: asks for
confirmation unless --yes.

Examples:
  python reset_db.py                      # reset BOTH stores (asks to confirm)
  python reset_db.py --sport mlb --yes    # reset only MLB, no prompt
  python reset_db.py --delete-file --yes  # remove the .db files entirely
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

DEFAULT_DBS = {
    "mlb": os.path.expanduser("~/.polymarket-mlb-totals/predictions.db"),
    "soccer": os.path.expanduser("~/.polymarket-soccer/predictions.db"),
}


def _count(con: sqlite3.Connection, table: str) -> int:
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.Error:
        return 0


def reset(db_path: str, *, delete_file: bool = False) -> dict:
    """Reset one DB. Returns {existed, deleted_file, predictions, model_log, cache}."""
    if not os.path.exists(db_path):
        return {"existed": False, "deleted_file": False, "predictions": 0,
                "model_log": 0, "cache": 0}
    if delete_file:
        os.remove(db_path)
        return {"existed": True, "deleted_file": True, "predictions": 0,
                "model_log": 0, "cache": 0}
    con = sqlite3.connect(db_path)
    try:
        preds = _count(con, "predictions")
        mlog = _count(con, "model_log")        # shadow calibration log
        cache = _count(con, "analysis_cache")  # only present in the MLB store
        with con:
            con.execute("DELETE FROM predictions")
            if mlog:
                con.execute("DELETE FROM model_log")
            if cache:
                con.execute("DELETE FROM analysis_cache")
        con.execute("VACUUM")
        return {"existed": True, "deleted_file": False, "predictions": preds,
                "model_log": mlog, "cache": cache}
    finally:
        con.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Reset the prediction database(s).")
    p.add_argument("--sport", choices=("mlb", "soccer", "all"), default="all")
    p.add_argument("--db", default=None, help="Override a single DB path (implies one target)")
    p.add_argument("--delete-file", action="store_true",
                   help="Remove the .db file entirely (default: just clear the rows)")
    p.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    a = p.parse_args()

    if a.db:
        targets = [("custom", a.db)]
    else:
        sports = ("mlb", "soccer") if a.sport == "all" else (a.sport,)
        targets = [(s, DEFAULT_DBS[s]) for s in sports]

    print("About to reset:")
    for name, path in targets:
        print(f"  [{name}] {path}" + ("  (delete file)" if a.delete_file else "  (clear rows)"))
    if not a.yes:
        if input("Type 'yes' to confirm: ").strip().lower() != "yes":
            print("Aborted.")
            sys.exit(1)

    for name, path in targets:
        r = reset(path, delete_file=a.delete_file)
        if not r["existed"]:
            print(f"  [{name}] not found (nothing to do): {path}")
        elif r["deleted_file"]:
            print(f"  [{name}] file deleted: {path}")
        else:
            print(f"  [{name}] cleared {r['predictions']} prediction(s)"
                  f" + {r['model_log']} model-log + {r['cache']} cache row(s): {path}")
    print("Done.")


if __name__ == "__main__":
    main()
