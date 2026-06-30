#!/usr/bin/env python3
"""Manually (re)settle a soccer game's predictions from its REGULATION (90') score.

Polymarket goals O/U + BTTS markets settle on 90 minutes. The auto-settler skips knockout games
decided in extra time / penalties (the results feed's ``fullTime`` includes ET goals), so those
stay PENDENTE — and games settled BEFORE that fix may carry a WRONG status (e.g. a 1-1 at 90'
that became 2-1 in ET wrongly lost an UNDER 2.5). This re-settles a single game using the 90'
score you provide.

    python resettle_game.py --match "Germany vs. Paraguay" --home 1 --away 1          # dry-run
    python resettle_game.py --match "Germany vs. Paraguay" --home 1 --away 1 --apply  # write

It recomputes EVERY row of the matched game (all TOTAL lines + BTTS) via compute_status:
  actual_total = home + away,  actual_btts = (home >= 1 and away >= 1).

Safe by design: DRY-RUN by default (prints old → new, changes nothing); ``--apply`` makes a
timestamped ``.bak`` of the DB first. ``--match`` must select exactly ONE game — if it matches
more, it lists them and aborts so you can narrow the text. DB: ``SOCCER_PREDICTIONS_DB`` or the
soccer predictions default (~/.polymarket-soccer/predictions.db).
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import soccer_predictions as spdb  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Re-liquidar um jogo de futebol pelo placar do tempo regulamentar (90').")
    ap.add_argument("--match", required=True,
                    help="trecho do market_question (ex.: 'Germany vs. Paraguay')")
    ap.add_argument("--home", type=int, required=True, help="gols do mandante no 90' (tempo normal)")
    ap.add_argument("--away", type=int, required=True, help="gols do visitante no 90' (tempo normal)")
    ap.add_argument("--db", default=os.environ.get("SOCCER_PREDICTIONS_DB", spdb.DEFAULT_DB),
                    help="caminho do predictions.db")
    ap.add_argument("--apply", action="store_true", help="efetivar (default: dry-run)")
    args = ap.parse_args()

    if not os.path.isfile(args.db):
        print(f"[resettle] BD não encontrado: {args.db}")
        return 0

    total = float(args.home + args.away)
    btts = 1 if (args.home >= 1 and args.away >= 1) else 0

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT id, game_slug, market, side, line, status FROM predictions "
            "WHERE lower(market_question) LIKE ?", (f"%{args.match.lower()}%",))]
    finally:
        con.close()

    if not rows:
        print(f"[resettle] nenhuma predição casa com: {args.match!r}")
        return 0
    # Each market is stored under its own slug (…-total-2pt5 / …-btts); group by the BASE game
    # slug so the several markets of ONE game don't look like several games.
    games = sorted({spdb.model_log_base(r["game_slug"]) for r in rows})
    if len(games) > 1:
        print(f"[resettle] '{args.match}' casou com {len(games)} jogos distintos — refine o --match:")
        for g in games:
            print(f"    {g}")
        return 2

    print(f"[resettle] DB: {args.db}")
    print(f"[resettle] jogo: {games[0]}  |  placar 90' = {args.home}-{args.away} "
          f"(total={total:g}, btts={'sim' if btts else 'não'})")
    plan = []
    for r in rows:
        new = spdb.compute_status(r["market"], r["side"], r["line"],
                                  actual_total=total, actual_btts=btts)
        plan.append((r, new))
        flag = "=" if new == r["status"] else "→"
        print(f"  #{r['id']:>4} {r['market']:<6} {str(r['side']):<5} line={str(r['line']):<5} "
              f"{r['status']:<8} {flag} {new}")

    if not args.apply:
        print("[resettle] DRY-RUN — nada alterado. Rode de novo com --apply.")
        return 0

    bak = f"{args.db}.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.bak"
    shutil.copy2(args.db, bak)
    print(f"[resettle] backup -> {bak}")
    con = sqlite3.connect(args.db)
    try:
        with con:
            for r, new in plan:
                con.execute(
                    "UPDATE predictions SET status=?, actual_total=?, actual_btts=?, "
                    "settled_at=?, updated_at=? WHERE id=?",
                    (new, total, btts, spdb._now(), spdb._now(), r["id"]))
    finally:
        con.close()
    print(f"[resettle] pronto — {len(plan)} linha(s) re-liquidadas pelo placar de 90'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
