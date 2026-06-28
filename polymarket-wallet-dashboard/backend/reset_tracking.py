#!/usr/bin/env python3
"""Reset the LIVE tracking of watched wallets in the Wallet-Dashboard DB (wallets.db).

Why this exists
---------------
When a wallet is first added, the watcher now snapshots everything it ALREADY holds as a
*baseline* and ignores it — so only bets the wallet opens AFTER you add it show up in
Resultados and get pushed to Sports. Wallets that were added BEFORE that fix already have
their pre-add bets (often already settled) stored in ``wallet_bets`` plus dedup state. This
script clears that live tracking so the next poll re-baselines each wallet from "now".

It KEEPS the wallets themselves — name, address, CSV analysis, thresholds and forwarding
filters all survive. Only the live tracking is wiped, per wallet:

  - ``wallet_bets``      : the wallet's own Resultados rows (OPEN + settled)
  - ``seen_alerts``      : entry dedup state
  - ``settled_markets``  : settlement dedup state
  - ``baseline_markets`` : the ignored pre-existing markets
  - ``wallets.baseline_at`` -> NULL  (so the next poll re-snapshots the baseline)

Default DB: ``~/.polymarket-wallet-dashboard/wallets.db`` (override with ``DASHBOARD_WALLETS_DB``
or ``--db``).

Safe by design: DRY-RUN by default (prints what it WOULD clear, changes nothing). Pass
``--apply`` to actually reset, and a timestamped ``.bak`` copy of the DB is made first so the
change is reversible.

    python reset_tracking.py                       # dry-run, all wallets
    python reset_tracking.py --apply               # reset ALL wallets' tracking + backup
    python reset_tracking.py --apply --wallet-id 3 # reset only wallet #3
    python reset_tracking.py --apply --db /path/to/wallets.db

Restart the Wallet-Dashboard backend afterwards; each wallet re-baselines on its next poll, so
from then on ONLY the bets it opens while watched appear. Restore: stop the backend and copy
the ``.bak`` file back over ``wallets.db``.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wallets_store as ws  # noqa: E402

_TRACKING = ("wallet_bets", "seen_alerts", "settled_markets", "baseline_markets")


def _counts(con, wallet_id: int) -> dict:
    return {t: con.execute(f"SELECT COUNT(*) FROM {t} WHERE wallet_id=?",
                           (wallet_id,)).fetchone()[0] for t in _TRACKING}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Reset live tracking for watched wallets (keeps the wallets themselves).")
    ap.add_argument("--db", default=ws.DEFAULT_DB, help="path to wallets.db")
    ap.add_argument("--apply", action="store_true", help="actually reset (default: dry-run)")
    ap.add_argument("--wallet-id", type=int, default=None,
                    help="reset only this wallet id (default: every wallet)")
    args = ap.parse_args()

    db = args.db
    if not os.path.isfile(db):
        print(f"[reset_tracking] nothing to do — DB not found: {db}")
        return 0

    wallets = ws.list_wallets(db)
    if args.wallet_id is not None:
        wallets = [w for w in wallets if w["id"] == args.wallet_id]
        if not wallets:
            print(f"[reset_tracking] no wallet with id={args.wallet_id} in {db}")
            return 0

    print(f"[reset_tracking] DB: {db}")
    con = ws.connect(db)
    try:
        total = {t: 0 for t in _TRACKING}
        for w in wallets:
            c = _counts(con, w["id"])
            for t in _TRACKING:
                total[t] += c[t]
            print(f"  #{w['id']:<3} {w['name'][:28]:28s} " +
                  "  ".join(f"{t.split('_')[0]}={c[t]}" for t in _TRACKING))
    finally:
        con.close()
    print(f"[reset_tracking] {len(wallets)} wallet(s); totals: " +
          "  ".join(f"{t}={total[t]}" for t in _TRACKING))

    if not args.apply:
        print("[reset_tracking] DRY-RUN — nothing changed. Re-run with --apply to reset.")
        return 0

    bak = f"{db}.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.bak"
    shutil.copy2(db, bak)
    print(f"[reset_tracking] backup -> {bak}")

    for w in wallets:
        removed = ws.reset_tracking(w["id"], db)
        print(f"[reset_tracking] reset #{w['id']} {w['name'][:28]!r}: "
              + "  ".join(f"{t}={removed[t]}" for t in _TRACKING))
    print("[reset_tracking] done. Restart the Wallet-Dashboard backend; each wallet "
          "re-baselines on its next poll.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
