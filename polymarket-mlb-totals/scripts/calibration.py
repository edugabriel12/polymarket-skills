#!/usr/bin/env python3
"""Calibration report (Brier / log-loss / reliability) over the model_log shadow log.

The shadow log stores, for EVERY modeled market (bet or not), the model's probability
for a reference side (OVER for totals, YES for BTTS) and the eventual reference price.
This settles those rows and scores the model's calibration — the validation the deep
research (§5) calls for, free of the selection bias you'd get from only bet games.

Settlement is offline: a game's actual outcome (known from ANY settled prediction for
that game) is propagated to ALL of the game's shadow rows, so non-bet lines of a bet
game get scored too. Games where nothing was bet need the results feed (a documented
follow-up), as does CLV (needs a closing-price snapshot).

Pure stdlib; works for both the MLB and soccer stores (`--sport`).
"""

from __future__ import annotations

import argparse
import math
import os
import re
import sqlite3
import sys

DEFAULT_DBS = {
    "mlb": os.path.expanduser("~/.polymarket-mlb-totals/predictions.db"),
    "soccer": os.path.expanduser("~/.polymarket-soccer/predictions.db"),
}

_SUFFIX_RE = re.compile(r"-(?:total-\d{1,2}(?:pt5)?|btts|both-teams-to-score|gg)$")
_SETTLED = ("ACERTO", "ERRO", "ANULADO")


def base_slug(slug: str) -> str:
    return _SUFFIX_RE.sub("", slug or "")


def _clip(p: float, eps: float = 1e-6) -> float:
    return min(1.0 - eps, max(eps, p))


def ref_outcome(market: str, line, actual_total, actual_btts) -> int | None:
    """1 if the reference side (OVER/YES) won, 0 if lost, None if push/unknown."""
    if (market or "").upper() == "BTTS":
        return None if actual_btts is None else (1 if actual_btts else 0)
    if actual_total is None or line is None:
        return None
    if abs(float(actual_total) - float(line)) < 1e-9:
        return None  # push (integer line)
    return 1 if float(actual_total) > float(line) else 0


# ---------------------------------------------------------------------------
# Pure metrics
# ---------------------------------------------------------------------------


def _pairs(rows):
    return [(float(r["ref_prob"]), int(r["ref_outcome"])) for r in rows
            if r.get("ref_prob") is not None and r.get("ref_outcome") is not None]


def brier(rows) -> float | None:
    xs = _pairs(rows)
    return sum((p - o) ** 2 for p, o in xs) / len(xs) if xs else None


def log_loss(rows) -> float | None:
    xs = _pairs(rows)
    if not xs:
        return None
    return -sum(o * math.log(_clip(p)) + (1 - o) * math.log(1 - _clip(p))
                for p, o in xs) / len(xs)


def reliability(rows, nbins: int = 10) -> list[dict]:
    """Binned calibration table: predicted vs empirical frequency."""
    bins: list[list] = [[] for _ in range(nbins)]
    for p, o in _pairs(rows):
        bins[min(nbins - 1, int(p * nbins))].append((p, o))
    out = []
    for i, b in enumerate(bins):
        if b:
            out.append({"bucket": f"{i/nbins:.1f}-{(i+1)/nbins:.1f}", "n": len(b),
                        "avg_pred": sum(p for p, _ in b) / len(b),
                        "empirical": sum(o for _, o in b) / len(b)})
    return out


# ---------------------------------------------------------------------------
# Settlement (offline: propagate each game's outcome to all its shadow rows)
# ---------------------------------------------------------------------------


def settle_from_predictions(db_path: str) -> int:
    """Fill model_log.ref_outcome from settled predictions. Returns rows updated."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        finals_total, finals_btts = {}, {}
        for r in con.execute(f"SELECT * FROM predictions WHERE status IN {_SETTLED}"):
            d = dict(r)
            key = base_slug(d.get("game_slug", ""))
            if d.get("actual_total") is not None:
                finals_total[key] = d["actual_total"]
            if d.get("actual_btts") is not None:
                finals_btts[key] = d["actual_btts"]

        updated = 0
        with con:
            for r in con.execute("SELECT * FROM model_log WHERE ref_outcome IS NULL"):
                d = dict(r)
                key = base_slug(d.get("game_slug", ""))
                at = finals_total.get(key)
                ab = finals_btts.get(key)
                if at is None and ab is None:
                    continue
                out = ref_outcome(d.get("market"), d.get("line"), at, ab)
                if out is None:
                    continue
                con.execute(
                    "UPDATE model_log SET ref_outcome=?, actual_total=?, "
                    "actual_btts=?, status='SETTLED' WHERE id=?",
                    (out, at, (1 if ab else 0) if ab is not None else None, d["id"]))
                updated += 1
        return updated
    finally:
        con.close()


def _settled_rows(db_path: str, bet: int | None = None) -> list[dict]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        q = "SELECT * FROM model_log WHERE ref_outcome IS NOT NULL"
        args = []
        if bet is not None:
            q += " AND bet=?"
            args.append(bet)
        return [dict(r) for r in con.execute(q, args)]
    finally:
        con.close()


def report(db_path: str) -> dict:
    """Calibration metrics over the settled shadow log (all games + bet-only)."""
    con = sqlite3.connect(db_path)
    try:
        total = con.execute("SELECT COUNT(*) FROM model_log").fetchone()[0]
    finally:
        con.close()
    allr = _settled_rows(db_path)
    betr = [r for r in allr if r.get("bet") == 1]
    return {
        "logged": total, "settled": len(allr), "settled_bet": len(betr),
        "all": {"n": len(allr), "brier": brier(allr), "log_loss": log_loss(allr),
                "reliability": reliability(allr)},
        "bet": {"n": len(betr), "brier": brier(betr), "log_loss": log_loss(betr)},
        "note": "CLV needs a closing-price snapshot (not captured yet); games with no "
                "bet line are settled only via the results feed (follow-up).",
    }


def format_report(rep: dict) -> str:
    def fmt(x):
        return "n/a" if x is None else f"{x:.4f}"
    a = rep["all"]
    lines = [
        "Calibration report (model_log shadow log)", "=" * 48,
        f"Logged: {rep['logged']}   Settled: {rep['settled']} "
        f"(bet: {rep['settled_bet']})", "",
        f"ALL modeled markets (n={a['n']}):  Brier={fmt(a['brier'])}  "
        f"LogLoss={fmt(a['log_loss'])}",
        f"BET only          (n={rep['bet']['n']}):  Brier={fmt(rep['bet']['brier'])}  "
        f"LogLoss={fmt(rep['bet']['log_loss'])}",
        "", "Reliability (predicted P(ref) vs empirical):",
        f"  {'bucket':<10} {'n':>5} {'avg_pred':>9} {'empirical':>10}",
    ]
    for b in a["reliability"]:
        lines.append(f"  {b['bucket']:<10} {b['n']:>5} {b['avg_pred']:>9.3f} "
                     f"{b['empirical']:>10.3f}")
    lines += ["", "Lower Brier/LogLoss = better. A well-calibrated model tracks the",
              "diagonal (avg_pred ≈ empirical). " + rep["note"]]
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description="Calibration report over the model_log shadow log.")
    p.add_argument("--sport", choices=("mlb", "soccer"), default="mlb")
    p.add_argument("--db", default=None, help="Override DB path (default per --sport)")
    p.add_argument("--settle", action="store_true",
                   help="First settle model_log from settled predictions")
    p.add_argument("--json", action="store_true")
    a = p.parse_args()
    db = a.db or DEFAULT_DBS[a.sport]
    if not os.path.exists(db):
        print(f"DB not found: {db}", file=sys.stderr)
        sys.exit(1)
    if a.settle:
        n = settle_from_predictions(db)
        print(f"Settled {n} shadow row(s) from settled predictions.\n", file=sys.stderr)
    rep = report(db)
    if a.json:
        import json
        print(json.dumps(rep, indent=2, default=str))
    else:
        print(format_report(rep))


if __name__ == "__main__":
    main()
