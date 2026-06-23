#!/usr/bin/env python3
"""Closing-Line Value vs the SHARP close — the gold-standard edge metric for MLB.

The deep research (references/edge-pathways-deep-research.md): beating the sharp
(Pinnacle, devigged) closing line is the only validated proxy for a real edge, and it
confirms in ~50 bets vs thousands needed to prove profit from results. This scores each
recorded Polymarket entry against the sharp CLOSE:

    CLV(side) = sharp_close_fair_prob(side) − entry_price(side)

CLV > 0 means you bought the side cheaper than the sharp market valued it at close — the
signature of +EV. Pure scoring is offline-testable; the sharp close comes from a CSV
(close_over_odds/close_under_odds) or The Odds API near game time.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import predictions_db as pdb
import park_factors as pf
import run_distribution as rd
import sharp_odds

DEFAULT_DISPERSION = 2.0


def clv_for(side: str, entry_price: float, sharp_close_over: float | None) -> float | None:
    """CLV for one bet: sharp close fair prob of the bet side minus the entry price."""
    if sharp_close_over is None or entry_price is None:
        return None
    side_close = sharp_close_over if (side or "").upper() == "OVER" else (1.0 - sharp_close_over)
    return side_close - float(entry_price)


def sharp_close_over_at(sharp_lookup: dict, date: str, away, home, bet_line,
                        dispersion: float = DEFAULT_DISPERSION) -> float | None:
    """Sharp CLOSE fair P(over) AT THE BET'S line, or None.

    The bet may have been placed at an alternate line that differs from the sharp's
    closing line. Rather than drop it (the old 0.51-tolerance gate did), invert the
    sharp close to an expected total (mu) at ITS line and evaluate P(over bet_line) off
    that mu — the same line-drift-robust anchoring the model uses. When the close line
    equals the bet line this returns the sharp close prob directly.
    """
    ref = sharp_odds.sharp_ref(sharp_lookup, date, away, home, use_close=True)
    if ref is None or bet_line is None:
        return None
    close_line, close_over = ref
    if abs(close_line - float(bet_line)) < 1e-9:
        return close_over
    mu = rd.market_implied_mu(close_line, close_over, dispersion)
    var = rd.variance_from_mu(mu, dispersion)
    pmf = rd.negbin_total_runs_pmf(mu, var)
    return rd.prob_over(float(bet_line), pmf)["p_over_eff"]


def score(predictions: list[dict], sharp_lookup: dict,
          dispersion: float = DEFAULT_DISPERSION) -> list[dict]:
    """Attach CLV (vs sharp close) to each prediction that resolves a sharp close. Pure."""
    out = []
    for p in predictions:
        away, home = pf.parse_slug_teams(p.get("game_slug", ""))
        sc = sharp_close_over_at(sharp_lookup, p.get("game_date", ""), away, home,
                                 p.get("line"), dispersion)
        clv = clv_for(p.get("side"), p.get("entry_price"), sc)
        if clv is not None:
            out.append({"side": (p.get("side") or "").upper(), "entry_price": p["entry_price"],
                        "sharp_close_over": sc, "clv": clv, "status": p.get("status")})
    return out


def report(scored: list[dict]) -> dict:
    def block(rows):
        if not rows:
            return {"n": 0}
        clvs = [r["clv"] for r in rows]
        return {"n": len(rows), "avg_clv": sum(clvs) / len(clvs),
                "beat_close_pct": sum(1 for c in clvs if c > 0) / len(clvs),
                "avg_entry": sum(r["entry_price"] for r in rows) / len(rows)}
    return {"all": block(scored),
            "over": block([r for r in scored if r["side"] == "OVER"]),
            "under": block([r for r in scored if r["side"] == "UNDER"])}


def format_report(rep: dict) -> str:
    def line(name, b):
        if not b.get("n"):
            return f"  {name:<6} n=0"
        return (f"  {name:<6} n={b['n']:<4} avg_CLV={b['avg_clv']:+.4f}  "
                f"beat_close={b['beat_close_pct']:.1%}  avg_entry={b['avg_entry']:.3f}")
    return "\n".join([
        "CLV vs the SHARP close (the validated edge metric)", "=" * 50,
        line("ALL", rep["all"]), line("OVER", rep["over"]), line("UNDER", rep["under"]),
        "",
        "avg_CLV > 0 and beat_close > 50% = real edge (you beat the sharp close).",
        "Needs ~50+ settled bets to be meaningful (CLV variance << P&L variance).",
    ])


def main() -> None:
    ap = argparse.ArgumentParser(description="CLV of recorded entries vs the sharp closing line.")
    ap.add_argument("--db", default=pdb.DEFAULT_DB, help="Predictions DB (default MLB store)")
    ap.add_argument("--sharp-odds-csv", required=True,
                    help="Sharp odds CSV with close_over_odds/close_under_odds per game")
    ap.add_argument("--status", default=None, help="Filter predictions by status")
    ap.add_argument("--dispersion", type=float, default=DEFAULT_DISPERSION,
                    help="variance = dispersion*mean, for pricing the bet line off a drifted "
                         "sharp close (default 2.0; must match the model's)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if not os.path.exists(a.db):
        print(f"DB not found: {a.db}", file=sys.stderr); sys.exit(1)
    sharp = sharp_odds.load_sharp_csv(a.sharp_odds_csv)
    preds = pdb.get_predictions(a.db, status=a.status)
    scored = score(preds, sharp, a.dispersion)
    rep = report(scored)
    if a.json:
        print(json.dumps(rep, indent=2, default=str))
    else:
        print(format_report(rep))
    if not scored:
        print("\n(no predictions matched the sharp close — check team/date alignment)",
              file=sys.stderr)


if __name__ == "__main__":
    main()
