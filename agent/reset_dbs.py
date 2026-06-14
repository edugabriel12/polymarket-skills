"""Reset the local Polymarket weather bot DBs to a clean state.

Backs up everything first so you can roll back if you delete by mistake.

Usage:
    # Default: $1000 starting balance, keep log file
    python agent/reset_dbs.py --confirm

    # Custom balance
    python agent/reset_dbs.py --confirm --balance 500

    # Also wipe the JSONL event log (fresh start, no history)
    python agent/reset_dbs.py --confirm --reset-log

    # Preview what would be deleted (no changes)
    python agent/reset_dbs.py --dry-run
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path.home() / ".polymarket-paper"

WEATHER_DB_FILES = [
    DATA_DIR / "weather_edge.db",
    DATA_DIR / "weather_edge.db-wal",
    DATA_DIR / "weather_edge.db-shm",
]
PORTFOLIO_DB_FILES = [
    DATA_DIR / "portfolio.db",
    DATA_DIR / "portfolio.db-wal",
    DATA_DIR / "portfolio.db-shm",
]
JSONL_FILE = DATA_DIR / "weather_edge.jsonl"
ADVISOR_REPORTS_DIR = DATA_DIR / "advisor_reports"


def _try_remove_or_report(f: Path) -> bool:
    """Try to unlink a file. Returns True if removed, False if locked
    (with an informative message about which processes might hold it)."""
    try:
        f.unlink()
        return True
    except PermissionError as e:
        print(f"\n✗ FAILED to remove {f.name}: {e}")
        print(f"  Something still has this file open. On Windows, run:")
        print(f"    Get-Process | Where-Object {{ $_.Path -like '*python*' }}")
        print(f"  Then Stop-Process -Id <PID> for each one. Common holders:")
        print(f"    - weather_edge_bot.py (terminal running the bot)")
        print(f"    - weather_edge_judge.py (terminal running the judge)")
        print(f"    - uvicorn dashboard.main:app (dashboard server)")
        print(f"    - DBeaver / any SQLite browser GUI")
        print(f"    - Background Python from a previous --daemon you forgot to stop")
        return False


def make_backup() -> Path | None:
    """Move all existing DB files + log into a timestamped backup folder.
    Returns the backup path, or None if any move failed due to locked files."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = DATA_DIR / f"backup-{stamp}"
    backup.mkdir(parents=True, exist_ok=True)
    moved: list[Path] = []
    locked: list[Path] = []
    for f in WEATHER_DB_FILES + PORTFOLIO_DB_FILES + [JSONL_FILE]:
        if not f.exists():
            continue
        dst = backup / f.name
        try:
            shutil.move(str(f), str(dst))
            moved.append(dst)
        except PermissionError:
            locked.append(f)

    if locked:
        # Roll back partial move so user can retry cleanly
        for m in moved:
            try:
                shutil.move(str(m), str(DATA_DIR / m.name))
            except Exception:
                pass
        try:
            backup.rmdir()
        except Exception:
            pass
        print(f"\n✗ {len(locked)} file(s) are locked by another process:")
        for f in locked:
            print(f"  - {f.name}")
        print()
        print("Stop ALL of the following before running reset:")
        print("  1. weather_edge_bot.py    (Ctrl+C in its terminal)")
        print("  2. weather_edge_judge.py  (Ctrl+C in its terminal)")
        print("  3. uvicorn dashboard...   (Ctrl+C in its terminal)")
        print("  4. DBeaver / SQLite Browser GUI (close the connection)")
        print()
        print("On PowerShell, to find leftover python processes:")
        print("  Get-Process python | Format-Table Id, ProcessName, StartTime")
        print("Kill a specific one with: Stop-Process -Id <PID>")
        return None

    if ADVISOR_REPORTS_DIR.exists() and any(ADVISOR_REPORTS_DIR.iterdir()):
        dst = backup / "advisor_reports"
        shutil.move(str(ADVISOR_REPORTS_DIR), str(dst))
        moved.append(dst)
        ADVISOR_REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    print(f"\n✓ Backed up {len(moved)} items to:")
    print(f"  {backup}\n")
    for m in moved:
        print(f"  - {m.name}")
    return backup


def init_weather_db() -> None:
    """Re-create weather_edge.db schema (v3) by importing the module."""
    sys.path.insert(0, str(REPO_ROOT / "polymarket-analyzer" / "scripts"))
    import weather_edge_db as db  # type: ignore
    db.init_db()
    print(f"✓ Re-created weather_edge.db (schema v{db.SCHEMA_VERSION})")


def init_portfolio(balance: float, name: str = "default") -> None:
    """Initialize a fresh portfolio with the given starting balance."""
    sys.path.insert(0, str(REPO_ROOT / "polymarket-paper-trader" / "scripts"))
    import paper_engine  # type: ignore
    result = paper_engine.init_portfolio(starting_balance=balance, name=name)
    print(f"✓ Created portfolio '{result['name']}' "
          f"with ${result['starting_balance']:.2f} starting balance")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--confirm", action="store_true",
                   help="Required to actually wipe. Without it, prints a preview.")
    p.add_argument("--dry-run", action="store_true",
                   help="Just show what would be deleted, no action.")
    p.add_argument("--balance", type=float, default=1000.0,
                   help="Starting balance for the new default portfolio (default $1000)")
    p.add_argument("--portfolio-name", default="default",
                   help="Portfolio name (default 'default')")
    p.add_argument("--reset-log", action="store_true",
                   help="Also wipe weather_edge.jsonl event log (otherwise kept)")
    p.add_argument("--no-backup", action="store_true",
                   help="Skip backup (dangerous, not recommended)")
    args = p.parse_args()

    if not args.confirm and not args.dry_run:
        print("Refusing to wipe without --confirm.")
        print("Run with --dry-run to preview, or --confirm to proceed.")
        return 1

    print("=" * 60)
    print("Polymarket weather bot — DB reset")
    print("=" * 60)
    print(f"Data dir: {DATA_DIR}")
    print()
    print("Files that will be removed:")
    found_any = False
    for f in WEATHER_DB_FILES + PORTFOLIO_DB_FILES:
        if f.exists():
            print(f"  - {f.name:35s} ({f.stat().st_size:,} bytes)")
            found_any = True
    if args.reset_log and JSONL_FILE.exists():
        print(f"  - {JSONL_FILE.name:35s} ({JSONL_FILE.stat().st_size:,} bytes)")
        found_any = True
    if not found_any:
        print("  (no DB files exist yet — first-time setup)")

    if args.dry_run:
        print("\n[DRY RUN] Nothing was changed.")
        print(f"\nAfter reset, a fresh portfolio would be created with "
              f"${args.balance:.2f} starting balance.")
        return 0

    # Final confirmation prompt
    print()
    print("Bot and judge processes MUST be stopped (Ctrl+C in their terminals)")
    print("before running this — otherwise WAL files will be re-created.")
    print()
    if not args.no_backup:
        print("A timestamped backup will be made under ~/.polymarket-paper/backup-*")
    else:
        print("WARNING: --no-backup is set; data will be permanently deleted.")
    print()
    print("Proceeding in 3 seconds (Ctrl+C to abort)...")
    try:
        time.sleep(3)
    except KeyboardInterrupt:
        print("\nAborted.")
        return 130

    # 1) Backup
    if not args.no_backup:
        if make_backup() is None:
            print("\nAborted — files still locked. Stop the holders and re-run.")
            return 2
    else:
        # Just delete
        deleted = 0
        for f in WEATHER_DB_FILES + PORTFOLIO_DB_FILES:
            if f.exists():
                f.unlink()
                deleted += 1
        if args.reset_log and JSONL_FILE.exists():
            JSONL_FILE.unlink()
            deleted += 1
        print(f"✓ Deleted {deleted} files (no backup)")

    # 2) If we didn't reset the log but it WAS moved to backup, restore
    if not args.no_backup and not args.reset_log:
        latest_backup = max(DATA_DIR.glob("backup-*"), key=lambda p: p.stat().st_mtime)
        log_backup = latest_backup / JSONL_FILE.name
        if log_backup.exists():
            shutil.move(str(log_backup), str(JSONL_FILE))
            print(f"✓ Restored {JSONL_FILE.name} (use --reset-log to wipe it too)")

    # 3) Re-init schemas
    print()
    init_weather_db()
    init_portfolio(args.balance, name=args.portfolio_name)

    print()
    print("=" * 60)
    print("Reset complete. Next steps:")
    print("=" * 60)
    print()
    print(f"  Verify portfolio:")
    print(f"  python polymarket-paper-trader\\scripts\\paper_engine.py --action portfolio")
    print()
    print(f"  Restart the bot:")
    print(f"  python polymarket-analyzer\\scripts\\weather_edge_bot.py --daemon "
          f"--min-edge-pp 25 --log-file bot.jsonl")
    print()
    print(f"  Restart the judge:")
    print(f"  python polymarket-analyzer\\scripts\\weather_edge_judge.py")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
