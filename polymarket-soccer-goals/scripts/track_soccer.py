#!/usr/bin/env python3
"""Review and settle recorded soccer predictions (total-goals + BTTS).

Auto-settle pulls final scores from football-data.org (free; set FOOTBALL_DATA_TOKEN
or pass --token). Manual settle takes a game's final total goals (and whether both
teams scored). Status: PENDENTE -> ACERTO / ERRO (ANULADO on a total-goals push).

Usage:
    python track_soccer.py --summary
    python track_soccer.py --list --status PENDENTE
    python track_soccer.py --auto-settle
    python track_soccer.py --settle-game fifwc-nld-jpn-2026-06-14 --total 3 --btts yes
"""

from __future__ import annotations

import argparse
import json
import sys

import _bootstrap  # noqa: F401

import soccer_predictions as spdb
import soccer_results


def render_list(rows: list[dict]) -> str:
    lines = [f"{'id':>4} {'status':<9} {'mkt':<5} {'side':<5} {'line':>5} "
             f"{'price':>6} {'edge':>7}  game", "-" * 78]
    for r in rows:
        edge = f"{r['edge']*100:+.1f}%" if r.get("edge") is not None else "  n/a"
        price = f"{r['entry_price']:.3f}" if r.get("entry_price") is not None else "  -"
        line = r["line"] if (r.get("line") and r["line"] > 0) else "—"
        lines.append(f"{r['id']:>4} {r['status']:<9} {r['market']:<5} {r['side']:<5} "
                     f"{str(line):>5} {price:>6} {edge:>7}  {r['game_slug']}")
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description="Review/settle soccer predictions.")
    p.add_argument("--predictions-db", default=spdb.DEFAULT_DB)
    p.add_argument("--list", action="store_true")
    p.add_argument("--status", default=None)
    p.add_argument("--date", default=None)
    p.add_argument("--summary", action="store_true")
    p.add_argument("--auto-settle", action="store_true", help="Settle from football-data.org finals")
    p.add_argument("--token", default=None, help="football-data.org API token (or FOOTBALL_DATA_TOKEN)")
    p.add_argument("--settle-game", default=None, help="Game slug to settle manually")
    p.add_argument("--total", type=float, default=None, help="Final total goals (with --settle-game)")
    p.add_argument("--btts", choices=["yes", "no"], default=None, help="Both teams scored? (with --settle-game)")
    p.add_argument("--output", choices=["json", "text"], default="json")
    args = p.parse_args()

    db = args.predictions_db
    out: dict = {}

    if args.settle_game is not None:
        if args.total is None and args.btts is None:
            print(json.dumps({"error": "pass --total and/or --btts"}), file=sys.stderr); sys.exit(2)
        out["settled"] = spdb.settle_game(args.settle_game, db, actual_total=args.total,
                                          actual_btts=None if args.btts is None else args.btts == "yes")
    elif args.auto_settle:
        out["settled"] = soccer_results.settle_pending(db, token=args.token)

    if args.summary or not (args.list or "settled" in out):
        out["summary"] = spdb.summary(db)
    if args.list:
        out["predictions"] = spdb.get_predictions(db, status=args.status, game_date=args.date)

    if args.output == "text":
        if "settled" in out:
            print(f"Settled: {json.dumps(out['settled'])}")
        if "summary" in out:
            s = out["summary"]
            wr = "n/a" if s["win_rate"] is None else f"{s['win_rate']*100:.1f}%"
            print(f"Predictions: {s['total']} | PENDENTE {s['pendente']} | ACERTO {s['acerto']} | "
                  f"ERRO {s['erro']} | ANULADO {s['anulado']} | win rate {wr}")
        if "predictions" in out:
            print(render_list(out["predictions"]))
    else:
        print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
