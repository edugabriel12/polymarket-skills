"""Split the weather_edge.db snapshot into two smaller SQLite files so
the operator can download them separately. Each output file is a fully
valid SQLite database — open with any tool, query with `sqlite3`, etc.

  core.db     — trading data: entries, cashouts, resolutions,
                counterfactuals, judge_reviews, judge_prompts,
                monitor_checks, advisor_* tables.
  forecast.db — observability ballast: forecast_history,
                discovery_skips. Roughly 80% of the snapshot's size
                comes from these two tables.

Usage (PowerShell on Windows — replaces the legacy Copy-Item):
  python snapshot_split.py
  # outputs to %USERPROFILE%\\Downloads\\
  #   weather_edge_snapshot_core.db
  #   weather_edge_snapshot_forecast.db

Or pass explicit paths:
  python snapshot_split.py --src C:\\path\\to\\source.db ^
                            --out-dir C:\\path\\to\\outdir

Re-runnable: each invocation overwrites the previous output.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from pathlib import Path

# Which tables land in which output file. Anything in neither set is
# dropped from both — keep this exhaustive so we don't silently lose
# data when a new schema version adds a table.
CORE_TABLES = {
    "entries", "cashouts", "resolutions", "counterfactuals",
    "judge_reviews", "judge_prompts", "monitor_checks",
    "advisor_runs", "advisor_suggestion_applies", "advisor_jobs",
}
FORECAST_TABLES = {"forecast_history", "discovery_skips"}


def _default_src() -> Path:
    return Path.home() / ".polymarket-paper" / "weather_edge.db"


def _default_outdir() -> Path:
    home = Path.home()
    # Match the Windows path the operator uses; on other OSes fall
    # back to ~/Downloads if it exists, else cwd.
    downloads = home / "Downloads"
    return downloads if downloads.exists() else Path.cwd()


def _checkpoint_source(src: Path) -> None:
    """Best-effort: fold the WAL back into the main .db file so a plain
    filesystem copy captures the latest committed rows. Safe to fail —
    if the bot holds a write lock the checkpoint is skipped and the copy
    simply reflects the last automatic checkpoint, which is still a
    consistent snapshot."""
    try:
        conn = sqlite3.connect(str(src), timeout=2.0)
        try:
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        finally:
            conn.close()
    except Exception:
        pass


def _clone(src: Path, dst: Path) -> None:
    """Filesystem-level copy of the main DB file — mirrors the operator's
    original `Copy-Item`.

    We deliberately avoid sqlite3.backup(): on Windows it raises
    "disk I/O error" when the live DB is in WAL mode and its -wal/-shm
    sidecar files are mapped by the running bot. A byte copy of the main
    file sidesteps that machinery entirely. We checkpoint first so the
    copy is as fresh as possible, and we make sure no stale sidecar files
    are left next to the destination."""
    _checkpoint_source(src)
    dst.unlink(missing_ok=True)
    shutil.copy2(str(src), str(dst))
    # A previous run's WAL/SHM next to dst would shadow our fresh copy.
    for suffix in ("-wal", "-shm"):
        Path(str(dst) + suffix).unlink(missing_ok=True)


def _drop_tables(db_path: Path, keep: set[str]) -> None:
    """Drop every user table not in `keep`, then VACUUM to reclaim space.

    Indexes attached to dropped tables go away with them. user_version
    is preserved by VACUUM so the resulting file still identifies as
    the correct schema version.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()]
        for t in tables:
            if t not in keep:
                conn.execute(f'DROP TABLE IF EXISTS "{t}"')
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()


def split(src: Path, outdir: Path) -> tuple[Path, Path]:
    if not src.exists():
        raise SystemExit(f"source DB not found: {src}")
    outdir.mkdir(parents=True, exist_ok=True)
    core_path = outdir / "weather_edge_snapshot_core.db"
    fc_path = outdir / "weather_edge_snapshot_forecast.db"

    _clone(src, core_path)
    _drop_tables(core_path, keep=CORE_TABLES)

    _clone(src, fc_path)
    _drop_tables(fc_path, keep=FORECAST_TABLES)

    return core_path, fc_path


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--src", type=Path, default=_default_src(),
                   help=f"source DB (default {_default_src()})")
    p.add_argument("--out-dir", type=Path, default=_default_outdir(),
                   help=f"output directory (default {_default_outdir()})")
    args = p.parse_args()

    src_size = args.src.stat().st_size if args.src.exists() else 0
    core_path, fc_path = split(args.src, args.out_dir)
    core_size = core_path.stat().st_size
    fc_size = fc_path.stat().st_size

    def _kb(n: int) -> str:
        return f"{n / 1024:.1f} KB" if n < 1_048_576 else f"{n / 1_048_576:.2f} MB"

    print(f"source:   {args.src}  ({_kb(src_size)})")
    print(f"core:     {core_path}  ({_kb(core_size)})")
    print(f"forecast: {fc_path}  ({_kb(fc_size)})")
    saved = src_size - max(core_size, fc_size)
    if src_size:
        print(f"largest part is {max(core_size, fc_size) / src_size * 100:.0f}% "
              f"of the original.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
