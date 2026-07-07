#!/usr/bin/env python3
"""Clear non-temperature ("will it rain" / "will it snow" / precip) entries
left in weather_edge.db from BEFORE the v14 temperature-only policy (PR #155).

v14 blocks NEW rain/snow/precip markets at discovery + judge, but entries
created earlier may linger. This one-shot removes them from the active
pipeline safely:

  - PENDING (PROPOSED / APPROVED / ADJUSTED) — never became a position, so
    they are marked SKIPPED (or, with --delete, removed). This is the cleanup.
  - EXECUTED / FAST_PATH — LEFT AS-IS. These hold paper capital in
    portfolio.db; touching only weather_edge.db would desync the two. Since
    v14 blocks new rain markets, the existing rain positions drain naturally
    (the resolution sweep settles them at end_date). Close via the paper
    engine if you must, never by editing this DB.
  - Already RESOLVED / SKIPPED — history; left untouched.

Non-temperature signature: the slug/question mentions rain/snow, OR
threshold_unit is 'mm' (temperature markets resolve in F/C; precipitation is
mm). If you have a legitimate mm market to keep, pass --no-mm.

Default is DRY-RUN (report only). Run on the host with ~/.polymarket-paper/,
ideally with the daemons stopped:

    systemctl --user stop weather-edge-bot weather-edge-judge
    python clear_rain_entries.py                 # report, no writes
    python clear_rain_entries.py --apply          # mark pending SKIPPED
    python clear_rain_entries.py --apply --delete # delete pending rows
    systemctl --user start weather-edge-bot weather-edge-judge
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import weather_edge_db as db  # noqa: E402

PENDING = ("PROPOSED", "APPROVED", "ADJUSTED")
_PENDING_SQL = "('PROPOSED','APPROVED','ADJUSTED')"
_OPEN = ("EXECUTED", "FAST_PATH")

# Text signals for rain/snow markets. The mm clause is added unless --no-mm.
_TEXT_PREDICATE = (
    "(lower(market_slug)     LIKE 'will-it-rain%'"
    " OR lower(market_slug)     LIKE 'will-it-snow%'"
    " OR lower(market_question) LIKE '%will it rain%'"
    " OR lower(market_question) LIKE '%will it snow%'"
)


def _predicate(include_mm: bool) -> str:
    """Full WHERE predicate for non-temperature entries."""
    if include_mm:
        return _TEXT_PREDICATE + " OR threshold_unit = 'mm')"
    return _TEXT_PREDICATE + ")"


def _matching_rows(conn, predicate: str):
    return conn.execute(
        f"SELECT entry_id, status, market_slug, threshold_unit "
        f"FROM entries WHERE {predicate} ORDER BY status, entry_id"
    ).fetchall()


def run(db_path=None, apply: bool = False, delete: bool = False,
        include_mm: bool = True) -> dict:
    """Report (and, when apply=True, clean) non-temperature entries.

    Returns a summary dict: {matched, pending, executed, other, action,
    affected}. Pure count report when apply=False."""
    predicate = _predicate(include_mm)
    connect = (lambda: db.connect(db_path)) if db_path else db.connect

    with connect() as conn:
        rows = _matching_rows(conn, predicate)

    pending = [r for r in rows if r["status"] in PENDING]
    executed = [r for r in rows if r["status"] in _OPEN]
    other = [r for r in rows if r["status"] not in PENDING + _OPEN]

    action = "none"
    affected = 0
    if apply and pending:
        with connect() as conn:
            if delete:
                cur = conn.execute(
                    f"DELETE FROM entries WHERE {predicate} "
                    f"AND status IN {_PENDING_SQL}")
                action = "delete"
            else:
                cur = conn.execute(
                    f"UPDATE entries SET status='SKIPPED', "
                    f"skip_reason='non_temperature_cleanup_v14' "
                    f"WHERE {predicate} AND status IN {_PENDING_SQL}")
                action = "skip"
            affected = cur.rowcount
            conn.commit()

    return {"matched": len(rows), "pending": len(pending),
            "executed": len(executed), "other": len(other),
            "action": action, "affected": affected,
            "pending_rows": pending, "executed_rows": executed}


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Clear pre-v14 rain/snow/precip entries from "
                    "weather_edge.db (pending → SKIPPED; executed left as-is).")
    ap.add_argument("--apply", action="store_true",
                    help="write changes (default: dry-run report only)")
    ap.add_argument("--delete", action="store_true",
                    help="DELETE pending rows instead of marking SKIPPED "
                         "(loses history; only ever touches PENDING, never "
                         "EXECUTED — those have FKs + a portfolio.db position)")
    ap.add_argument("--no-mm", action="store_true",
                    help="don't treat threshold_unit='mm' as non-temp "
                         "(use only slug/question text) — for operators with "
                         "a legitimate mm market to keep")
    args = ap.parse_args()
    dry = not args.apply

    summary = run(apply=args.apply, delete=args.delete,
                  include_mm=not args.no_mm)

    print(f"{'DRY-RUN' if dry else 'APPLY'}: {summary['matched']} "
          f"non-temperature entries matched\n")
    if summary["pending_rows"]:
        verb = "would " + ("delete" if args.delete else "skip") if dry else \
               ("deleted" if args.delete else "skipped")
        print(f"  PENDING ({len(summary['pending_rows'])}) — {verb}:")
        for r in summary["pending_rows"]:
            print(f"    #{r['entry_id']:<5d} {r['status']:<9s} "
                  f"{r['market_slug'][:56]}")
    if summary["executed_rows"]:
        print(f"\n  EXECUTED ({len(summary['executed_rows'])}) — LEFT AS-IS "
              f"(open paper positions; drain at resolution):")
        for r in summary["executed_rows"]:
            print(f"    #{r['entry_id']:<5d} {r['status']:<9s} "
                  f"{r['market_slug'][:56]}")
    if summary["other"]:
        print(f"\n  {summary['other']} already resolved/skipped — untouched.")

    print(f"\nsummary: matched={summary['matched']} pending={summary['pending']} "
          f"executed={summary['executed']} other={summary['other']}")
    if dry and summary["pending"]:
        print("  → re-run with --apply to clean the pending ones "
              "(add --delete to remove instead of mark SKIPPED).")
    elif not dry:
        print(f"  → {summary['action']}ed {summary['affected']} pending "
              f"ent(y/ies).")


# ---------------------------------------------------------------------------
# Inline test (offline, synthetic DB)
# ---------------------------------------------------------------------------

def _test() -> None:
    import tempfile
    tmp = Path(tempfile.mkdtemp()) / "clear_rain_test.db"
    db.init_db(tmp)
    seed = [
        ("will-it-rain-in-dallas", "Will it rain in Dallas?", "mm", "PROPOSED"),
        ("will-it-rain-in-boston", "Will it rain in Boston?", "mm", "EXECUTED"),
        ("will-it-snow-in-nyc", "Will it snow in NYC?", "mm", "APPROVED"),
        ("london-5mm-rain", "Will London get more than 5mm of rain?", "mm", "PROPOSED"),
        ("highest-temp-paris", "Will the highest temperature in Paris be 14C?", "C", "PROPOSED"),
        ("old-rain-resolved", "Will it rain in Rio?", "mm", "SKIPPED"),
    ]
    with db.connect(tmp) as conn:
        for slug, q, unit, st in seed:
            conn.execute(
                "INSERT INTO entries (ts, market_slug, market_question, "
                "threshold_unit, comparison, status, strategy) VALUES "
                "('2026-06-29T00:00:00Z',?,?,?,'exceed',?,'weather_edge')",
                (slug, q, unit, st))
        conn.commit()

    # Dry-run: matches 5 (4 rain/snow/precip pending-or-executed + 1 already
    # SKIPPED); the Paris temp entry (C) must NOT match.
    rep = run(db_path=tmp, apply=False)
    assert rep["matched"] == 5, rep
    assert rep["pending"] == 3, rep      # dallas, nyc, london-5mm
    assert rep["executed"] == 1, rep     # boston (left as-is)
    assert rep["other"] == 1, rep        # rio already SKIPPED
    print(f"Test 1 PASS: dry-run matched=5 pending=3 executed=1 other=1 "
          f"(Paris temp excluded)")

    # Apply (skip): 3 pending → SKIPPED; executed Boston preserved; Paris temp
    # still PROPOSED.
    rep = run(db_path=tmp, apply=True)
    assert rep["action"] == "skip" and rep["affected"] == 3, rep
    with db.connect(tmp) as conn:
        by_status = dict(conn.execute(
            "SELECT status, COUNT(*) FROM entries GROUP BY status").fetchall())
        boston = conn.execute(
            "SELECT status FROM entries WHERE market_slug='will-it-rain-in-boston'"
        ).fetchone()[0]
        paris = conn.execute(
            "SELECT status FROM entries WHERE market_slug='highest-temp-paris'"
        ).fetchone()[0]
    assert boston == "EXECUTED", boston          # untouched open position
    assert paris == "PROPOSED", paris            # legit temp untouched
    assert by_status.get("EXECUTED") == 1, by_status
    print(f"Test 2 PASS: --apply skipped 3 pending; EXECUTED + temp preserved "
          f"({by_status})")

    # --no-mm: numeric-precip 'london-5mm' (no rain/snow text in slug? it has
    # 'rain' in question) — still matches via question text. Build a case that
    # ONLY matches via mm to prove --no-mm drops it.
    tmp2 = Path(tempfile.mkdtemp()) / "clear_rain_nomm.db"
    db.init_db(tmp2)
    with db.connect(tmp2) as conn:
        conn.execute(
            "INSERT INTO entries (ts, market_slug, market_question, "
            "threshold_unit, comparison, status, strategy) VALUES "
            "('2026-06-29T00:00:00Z','precip-total-berlin',"
            "'Total precipitation in Berlin','mm','above','PROPOSED','weather_edge')")
        conn.commit()
    assert run(db_path=tmp2, apply=False)["matched"] == 1        # mm matches
    assert run(db_path=tmp2, apply=False, include_mm=False)["matched"] == 0
    print("Test 3 PASS: --no-mm drops mm-only match (precip w/o rain/snow text)")

    print("\nAll clear_rain_entries tests PASS")


if __name__ == "__main__":
    if "--test" in sys.argv:
        _test()
    else:
        main()
