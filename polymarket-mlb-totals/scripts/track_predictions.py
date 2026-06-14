#!/usr/bin/env python3
"""Review and settle recorded MLB total-runs predictions.

The predictions store (predictions_db.py) holds every prediction the model made,
its statistical/mathematical audit log, and a status: PENDENTE until the game
settles, then ACERTO / ERRO (or ANULADO on a push). This CLI lists them,
summarizes win rate, and settles them — manually by final total, or best-effort
auto-settle from the MLB Stats API final scores.

Usage:
    python track_predictions.py --summary
    python track_predictions.py --list --status PENDENTE
    python track_predictions.py --settle-game mlb-hou-kc-2026-06-13 --actual-total 9
    python track_predictions.py --settle-id 12 --actual-total 7
    python track_predictions.py --auto-settle --date 2026-06-13
"""

from __future__ import annotations

import argparse
import json
import sys

import _bootstrap  # noqa: F401

import predictions_db as pdb
import park_factors as pf
import ballparks
from category_common import APIClient

STATSAPI = "https://statsapi.mlb.com/api/v1"


def fetch_final_totals(api, date: str) -> dict[tuple[str, str], float]:
    """Map (away_abbr, home_abbr) -> final total runs for finished games on a date.

    Best-effort via the MLB Stats API; returns {} on any failure (e.g. sandbox).
    """
    try:
        data = api.get(f"{STATSAPI}/schedule",
                       params={"sportId": 1, "date": date,
                               "hydrate": "linescore,team"})
    except Exception:  # noqa: BLE001
        return {}
    out: dict[tuple[str, str], float] = {}
    for d in (data or {}).get("dates", []):
        for g in d.get("games", []):
            if (g.get("status", {}).get("abstractGameState") or "") != "Final":
                continue
            teams = g.get("teams", {})
            away = _abbr(teams.get("away", {}))
            home = _abbr(teams.get("home", {}))
            ls = g.get("linescore", {}).get("teams", {})
            try:
                total = float(ls["away"]["runs"]) + float(ls["home"]["runs"])
            except (KeyError, TypeError, ValueError):
                continue
            if away and home:
                out[(away, home)] = total
    return out


def _abbr(team_side: dict) -> str | None:
    t = team_side.get("team", {})
    a = t.get("abbreviation") or t.get("teamCode") or ""
    a = a.lower()
    return ballparks.ALIASES.get(a, a) or None


def auto_settle(api, date: str, db_path: str) -> list[dict]:
    """Settle pending predictions whose game finished, using API final totals."""
    finals = fetch_final_totals(api, date)
    results = []
    for row in pdb.get_predictions(db_path, status="PENDENTE", game_date=date):
        away, home = pf.parse_slug_teams(row["game_slug"])
        total = finals.get((away, home))
        if total is None:
            continue
        out = pdb.settle_game(row["game_slug"], total, db_path)
        results.extend(out)
    return results


def render_list(rows: list[dict]) -> str:
    lines = [f"{'id':>4} {'status':<9} {'side':<5} {'line':>5} {'price':>6} "
             f"{'edge':>7} {'actual':>7}  game", "-" * 78]
    for r in rows:
        edge = f"{r['edge']*100:+.1f}%" if r.get("edge") is not None else "  n/a"
        actual = f"{r['actual_total']:.0f}" if r.get("actual_total") is not None else "  -"
        price = f"{r['entry_price']:.3f}" if r.get("entry_price") is not None else "  -"
        lines.append(f"{r['id']:>4} {r['status']:<9} {r['side']:<5} {r['line']:>5} "
                     f"{price:>6} {edge:>7} {actual:>7}  {r['game_slug']}")
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description="Review/settle MLB total-runs predictions.")
    p.add_argument("--predictions-db", default=pdb.DEFAULT_DB, help="Predictions DB path")
    p.add_argument("--list", action="store_true", help="List predictions")
    p.add_argument("--status", default=None, help="Filter by status (PENDENTE/ACERTO/ERRO/ANULADO)")
    p.add_argument("--date", default=None, help="Filter/auto-settle by game date YYYY-MM-DD")
    p.add_argument("--summary", action="store_true", help="Show counts + win rate")
    p.add_argument("--settle-game", default=None, help="Settle all pending predictions for a game slug")
    p.add_argument("--settle-id", type=int, default=None, help="Settle one prediction by id")
    p.add_argument("--actual-total", type=float, default=None, help="Final total runs (with --settle-*)")
    p.add_argument("--auto-settle", action="store_true", help="Auto-settle from MLB Stats API finals")
    p.add_argument("--output", choices=["json", "text"], default="json")
    p.add_argument("--rate-limit", type=int, default=100)
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()

    db = args.predictions_db
    out: dict = {}

    if args.settle_game is not None:
        if args.actual_total is None:
            print(json.dumps({"error": "--actual-total required with --settle-game"}), file=sys.stderr)
            sys.exit(2)
        out["settled"] = pdb.settle_game(args.settle_game, args.actual_total, db)
    elif args.settle_id is not None:
        if args.actual_total is None:
            print(json.dumps({"error": "--actual-total required with --settle-id"}), file=sys.stderr)
            sys.exit(2)
        out["settled"] = [{"id": args.settle_id,
                           "status": pdb.settle_prediction(args.settle_id, args.actual_total, db)}]
    elif args.auto_settle:
        if not args.date:
            print(json.dumps({"error": "--date required with --auto-settle"}), file=sys.stderr)
            sys.exit(2)
        api = APIClient(rate_limit_ms=args.rate_limit, debug=args.debug)
        out["settled"] = auto_settle(api, args.date, db)

    if args.summary or not (args.list or "settled" in out):
        out["summary"] = pdb.summary(db)
    if args.list:
        out["predictions"] = pdb.get_predictions(db, status=args.status, game_date=args.date)

    if args.output == "text":
        if "settled" in out:
            print(f"Settled: {json.dumps(out['settled'])}")
        if "summary" in out:
            s = out["summary"]
            wr = "n/a" if s["win_rate"] is None else f"{s['win_rate']*100:.1f}%"
            print(f"Predictions: {s['total']} | PENDENTE {s['pendente']} | "
                  f"ACERTO {s['acerto']} | ERRO {s['erro']} | ANULADO {s['anulado']} | "
                  f"win rate {wr}")
        if "predictions" in out:
            print(render_list(out["predictions"]))
    else:
        print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
