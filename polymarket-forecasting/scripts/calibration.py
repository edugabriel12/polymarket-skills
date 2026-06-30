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

Pure stdlib; works for the soccer store (`--sport`).
"""

from __future__ import annotations

import argparse
import math
import os
import re
import sqlite3
import sys

import calibration_core as cc

DEFAULT_DBS = {
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


def clv_stats(db_path: str) -> dict:
    """Closing-line value over rows that have a captured close price."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT ref_price, close_price FROM model_log "
            "WHERE close_price IS NOT NULL AND ref_price IS NOT NULL")]
    except sqlite3.OperationalError:
        rows = []
    finally:
        con.close()
    if not rows:
        return {"n": 0}
    clvs = [float(r["close_price"]) - float(r["ref_price"]) for r in rows]
    return {"n": len(rows),
            "avg_ref_price": sum(float(r["ref_price"]) for r in rows) / len(rows),
            "avg_close_price": sum(float(r["close_price"]) for r in rows) / len(rows),
            "avg_clv": sum(clvs) / len(clvs),
            "beat_close_pct": sum(1 for c in clvs if c > 0) / len(clvs)}


def interval_coverage(db_path: str) -> dict:
    """Empirical coverage of the 50%/80% prediction intervals over SETTLED totals games.

    The thermometer for whether the Negative-Binomial distribution is well calibrated on
    REAL outcomes (and thus whether distribution-free conformal intervals would add
    anything). For each settled game it reconstructs the pmf from the logged (mu,
    variance), takes the central 50%/80% interval, and checks whether the actual total
    landed inside. Deduped to one forecast per GAME (the interval is a per-game property,
    not per-line). Also reports mean CRPS (run units) and the mean 80% interval width.

    Reads the unbiased `model_log` (every modeled game, bet or not). Best-effort: rows
    whose model_params lack mu/variance (e.g. soccer Dixon-Coles, BTTS) are skipped, so
    this is a no-op (n=0) where it does not apply.
    """
    import json as _json
    import run_distribution as rd
    import forecast as fc
    import scoring

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT game_slug, market, model_params, actual_total FROM model_log "
            "WHERE actual_total IS NOT NULL AND model_params IS NOT NULL")]
    except sqlite3.OperationalError:
        rows = []
    finally:
        con.close()

    seen: set[str] = set()
    recs = []
    for r in rows:
        if (r.get("market") or "TOTAL").upper() != "TOTAL":
            continue
        key = base_slug(r.get("game_slug", ""))
        if key in seen:
            continue
        mp = r.get("model_params")
        try:
            mp = _json.loads(mp) if isinstance(mp, str) else (mp or {})
            mu = float(mp["mu"])
            var = float(mp["variance"])
            actual = float(r["actual_total"])
        except (KeyError, ValueError, TypeError):
            continue
        if var <= mu:               # NegBin needs overdispersion; skip degenerate rows
            continue
        seen.add(key)
        pmf = rd.negbin_total_runs_pmf(mu, var)
        lo50, hi50 = fc.prediction_interval(pmf, 0.50)
        lo80, hi80 = fc.prediction_interval(pmf, 0.80)
        recs.append({"in50": 1 if lo50 <= actual <= hi50 else 0,
                     "in80": 1 if lo80 <= actual <= hi80 else 0,
                     "crps": scoring.crps_pmf(pmf, actual),
                     "width80": hi80 - lo80})
    n = len(recs)
    if not n:
        return {"n": 0}
    return {
        "n": n,
        "coverage50": sum(x["in50"] for x in recs) / n,   # target 0.50
        "coverage80": sum(x["in80"] for x in recs) / n,   # target 0.80
        "mean_crps": sum(x["crps"] for x in recs) / n,
        "mean_width80": sum(x["width80"] for x in recs) / n,
    }


def report(db_path: str, fit_method: str | None = None) -> dict:
    """Calibration metrics over the settled shadow log (all games + bet-only).

    Layer 2 metrics (ECE/MCE + Murphy Brier decomposition) come from `calibration_core`.
    If `fit_method` is given, a post-hoc calibrator is FIT on the settled pairs and the
    before/after ECE is reported — a SUGGESTION-ONLY diagnostic (it does not alter the
    live model; the operator decides whether to apply it). With little data a held-out
    split is unreliable, so the fit is in-sample and flagged as such.
    """
    con = sqlite3.connect(db_path)
    try:
        total = con.execute("SELECT COUNT(*) FROM model_log").fetchone()[0]
    finally:
        con.close()
    allr = _settled_rows(db_path)
    betr = [r for r in allr if r.get("bet") == 1]
    pairs = _pairs(allr)
    rep = {
        "logged": total, "settled": len(allr), "settled_bet": len(betr),
        "all": {"n": len(allr), "brier": brier(allr), "log_loss": log_loss(allr),
                "reliability": reliability(allr),
                "ece": cc.ece(pairs), "mce": cc.mce(pairs),
                "brier_decomposition": cc.brier_decomposition(pairs)},
        "bet": {"n": len(betr), "brier": brier(betr), "log_loss": log_loss(betr)},
        "clv": clv_stats(db_path),
        "interval_coverage": interval_coverage(db_path),
        "note": "CLV uses captured closing prices (run --capture-close near game time). "
                "Reference side = OVER (totals) / YES (BTTS).",
    }
    if fit_method and pairs:
        cal = cc.fit_calibrator(fit_method, pairs)
        after = [(cal.predict(p), o) for p, o in pairs]
        params = ({"temperature": round(cal.temperature, 4)}
                  if fit_method == "temperature"
                  else {"a": round(cal.a, 4), "b": round(cal.b, 4)}
                  if fit_method == "platt" else {"knots": len(cal._x)})
        rep["calibrator"] = {
            "method": fit_method, "params": params, "n": len(pairs),
            "ece_before": cc.ece(pairs), "ece_after": cc.ece(after),
            "in_sample": True,
        }
    return rep


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
        f"  Calibration: ECE={fmt(a.get('ece'))}  MCE={fmt(a.get('mce'))}",
        f"BET only          (n={rep['bet']['n']}):  Brier={fmt(rep['bet']['brier'])}  "
        f"LogLoss={fmt(rep['bet']['log_loss'])}",
    ]
    d = a.get("brier_decomposition")
    if d:
        lines.append(
            f"  Brier = reliability {fmt(d['reliability'])} (↓) − resolution "
            f"{fmt(d['resolution'])} (↑) + uncertainty {fmt(d['uncertainty'])}")
    cal = rep.get("calibrator")
    if cal:
        lines += [
            "", f"Post-hoc calibrator [{cal['method']}] (suggestion-only, in-sample):",
            f"  params={cal['params']}   ECE {fmt(cal['ece_before'])} -> "
            f"{fmt(cal['ece_after'])}  (apply manually if it improves on held-out data)",
        ]
    lines += [
        "", "Reliability (predicted P(ref) vs empirical):",
        f"  {'bucket':<10} {'n':>5} {'avg_pred':>9} {'empirical':>10}",
    ]
    for b in a["reliability"]:
        lines.append(f"  {b['bucket']:<10} {b['n']:>5} {b['avg_pred']:>9.3f} "
                     f"{b['empirical']:>10.3f}")
    ic = rep.get("interval_coverage", {})
    if ic.get("n", 0) > 0:
        lines += [
            "", f"Interval coverage (n={ic['n']} settled games) — should track nominal:",
            f"  50% interval -> {ic['coverage50']:.1%} (target 50%)   "
            f"80% interval -> {ic['coverage80']:.1%} (target 80%)",
            f"  mean CRPS {ic['mean_crps']:.3f} runs   mean 80% width "
            f"{ic['mean_width80']:.1f} runs",
            "  (coverage ≈ nominal => the NegBin intervals are honest; conformal would add "
            "little)",
        ]
    elif rep.get("settled", 0) > 0:
        lines += ["", "Interval coverage: no settled totals with a logged (mu,var) yet."]
    clv = rep.get("clv", {})
    if clv.get("n", 0) > 0:
        lines += [
            "", "Closing-line value (CLV):",
            f"  n={clv['n']}   avg_ref_price={clv['avg_ref_price']:.4f}"
            f"   avg_close={clv['avg_close_price']:.4f}",
            f"  avg_CLV={clv['avg_clv']:+.4f}   beat_close={clv['beat_close_pct']:.1%}",
        ]
    else:
        lines += ["", "CLV: no closing prices captured yet "
                  "(run --capture-close near game time)."]
    lines += ["", "Lower Brier/LogLoss = better. A well-calibrated model tracks the",
              "diagonal (avg_pred ≈ empirical). " + rep["note"]]
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description="Calibration report over the model_log shadow log.")
    p.add_argument("--sport", choices=("soccer",), default="soccer")
    p.add_argument("--db", default=None, help="Override DB path (default per --sport)")
    p.add_argument("--settle", action="store_true",
                   help="Settle model_log from settled predictions (offline cross-propagation)")
    p.add_argument("--settle-feed", action="store_true",
                   help="Settle model_log via results feed (covers non-bet games)")
    p.add_argument("--capture-close", action="store_true",
                   help="Snapshot closing CLOB prices for CLV (run near game time)")
    p.add_argument("--fit-calibrator", choices=("temperature", "platt", "isotonic"),
                   default=None,
                   help="Fit a post-hoc calibrator on the settled pairs and report "
                        "before/after ECE (suggestion-only; does not change the model)")
    p.add_argument("--json", action="store_true")
    a = p.parse_args()
    db = a.db or DEFAULT_DBS[a.sport]
    if not os.path.exists(db):
        print(f"DB not found: {db}", file=sys.stderr)
        sys.exit(1)
    if a.settle:
        n = settle_from_predictions(db)
        print(f"Settled {n} shadow row(s) from settled predictions.\n", file=sys.stderr)
    if a.settle_feed:
        n = _settle_feed(a.sport, db)
        print(f"Feed-settled {n} shadow row(s).\n", file=sys.stderr)
    if a.capture_close:
        n = _capture_close(a.sport, db)
        print(f"Captured {n} closing price(s).\n", file=sys.stderr)
    rep = report(db, fit_method=a.fit_calibrator)
    if a.json:
        import json
        print(json.dumps(rep, indent=2, default=str))
    else:
        print(format_report(rep))


def _settle_feed(sport: str, db: str) -> int:
    """Dispatch to sport-specific feed settlement."""
    try:
        import soccer_results
        return soccer_results.settle_model_log_from_feed(db)
    except Exception as e:
        print(f"{sport} feed settlement error: {e}", file=sys.stderr)
        return 0


def _capture_close(sport: str, db: str) -> int:
    """Dispatch to sport-specific close-price capture."""
    try:
        import soccer_results
        return soccer_results.capture_close_prices(db)
    except Exception as e:
        print(f"{sport} close capture error: {e}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    main()
