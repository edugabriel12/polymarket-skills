#!/usr/bin/env python3
"""Tail-calibration gate for the cheap_convexity strategy.

The cheap_convexity strategy buys 1-20c temperature bins when the market
price sits below the model's fair value and exits on cashout at convergence.
Its entire edge rests on ONE precondition: the model's fair value must be
trustworthy in the 1-20% probability band. The deep-research pass found that
global calibration does NOT imply tail calibration — raw NWP ensembles are
underdispersive and even EMOS post-processing leaves tails "too light". So
before the strategy is allowed to open a single position, this gate checks,
on our OWN resolved history, whether the model's RAW fair in [0.01, 0.20] is
calibrated: across the times the model said "7%", did YES actually happen
~7% of the time?

Why RAW and not the stored forecast_prob_at_entry: the stored value is the
sized P(side) — clipped to [0.30, 0.70] for single-threshold (at_least /
at_most) bins and floored by the P(NO) cap for range NO bets. Both destroy
the tail. This script recomputes p_yes_raw = _forecast_probability_raw(spec,
snapshot, overrides) so the tail band is honest.

Writes a verdict artifact to ~/.polymarket-paper/cheap_convexity_gate.json.
run_discovery_cheap_convexity reads that file and refuses to propose while
tail_calibration_pass is false. Pure/offline: reads the DB, reuses the
calibration_core measurement functions, no API calls, no DB writes.

Usage:
    python cheap_convexity_calibration.py --since-days 120 [--write]
    python cheap_convexity_calibration.py --test
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "polymarket-analyzer" / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "polymarket-forecasting" / "scripts"))

import weather_edge_db as db  # noqa: E402
from weather_edge_helpers import (  # noqa: E402
    parse_market, load_cities, _forecast_probability_raw,
)
from calibration_core import (  # noqa: E402
    reliability_diagram, ece, brier_decomposition,
)

# Gate artifact read by run_discovery_cheap_convexity.
GATE_PATH = Path.home() / ".polymarket-paper" / "cheap_convexity_gate.json"

# Tail band the strategy trades in.
BAND_LO = 0.01
BAND_HI = 0.20

# Approval criteria (documented; tune with data).
N_MIN = 150            # minimum resolved pairs in the band
ECE_MAX = 0.05         # expected calibration error in the band < 5pp
RELIABILITY_MAX = 0.01  # Murphy reliability component in the band
# Direction: the model must NOT overstate tail probability. If empirical
# >= avg_pred the tail is at least as likely as the model says (reinforces
# convexity); if avg_pred >> empirical the model inflates the tail → fail.
DIRECTION_TOL = 0.02   # allow avg_pred to exceed empirical by at most 2pp


def recompute_raw_p_yes(entry: dict, cities: dict) -> Optional[float]:
    """Reconstruct the model's RAW P(YES) for a resolved entry from its
    forecast snapshot + discovery meta overrides. None if not reconstructible
    (missing snapshot, unparseable question, missing forecast for date)."""
    snap = entry.get("forecast_snapshot_json")
    if not snap:
        return None
    try:
        forecast = json.loads(snap) if isinstance(snap, str) else snap
    except (TypeError, ValueError):
        return None
    spec = parse_market(entry.get("market_question") or "",
                        entry.get("end_date"), cities)
    if spec is None:
        return None
    meta = {}
    dm = entry.get("discovery_meta_json")
    if dm:
        try:
            meta = json.loads(dm) if isinstance(dm, str) else dm
        except (TypeError, ValueError):
            meta = {}
    try:
        return _forecast_probability_raw(
            spec, forecast,
            mae_override=meta.get("mae_dynamic"),
            bias_override=meta.get("bias"),
            mu_override=meta.get("mu"))
    except Exception:
        return None


def build_pairs(conn, since_iso: str,
                band_lo: float = BAND_LO, band_hi: float = BAND_HI) -> dict:
    """Build (p_yes_raw, outcome_yes) pairs from resolved entries, filtered to
    the [band_lo, band_hi] tail band. Returns
      {"pairs": [...], "n_recomputed": int, "n_skipped": int,
       "n_in_band": int}.
    outcome_yes = 1 if the market resolved YES else 0; VOID excluded."""
    cities = load_cities()
    rows = conn.execute(
        "SELECT e.market_question, e.end_date, e.forecast_snapshot_json, "
        "       e.discovery_meta_json, r.final_outcome "
        "FROM entries e JOIN resolutions r ON r.entry_id = e.entry_id "
        "WHERE e.ts >= ? AND r.final_outcome IN ('YES','NO')",
        (since_iso,),
    ).fetchall()

    pairs: list[tuple[float, int]] = []
    n_recomputed = n_skipped = 0
    for r in rows:
        p_yes = recompute_raw_p_yes(dict(r), cities)
        if p_yes is None:
            n_skipped += 1
            continue
        n_recomputed += 1
        if band_lo <= p_yes <= band_hi:
            pairs.append((p_yes, 1 if r["final_outcome"] == "YES" else 0))
    return {"pairs": pairs, "n_recomputed": n_recomputed,
            "n_skipped": n_skipped, "n_in_band": len(pairs)}


def evaluate_gate(pairs: list[tuple[float, int]],
                  n_min: int = N_MIN, ece_max: float = ECE_MAX,
                  reliability_max: float = RELIABILITY_MAX,
                  direction_tol: float = DIRECTION_TOL) -> dict:
    """Decide tail_calibration_pass over the in-band pairs. Fine bins (0.02
    wide) so the narrow [0.01,0.20] band gets ~10 buckets."""
    n = len(pairs)
    # nbins over [0,1]; 50 → 0.02-wide buckets so the band has resolution.
    band_ece = ece(pairs, nbins=50)
    decomp = brier_decomposition(pairs, nbins=50)
    reliability = decomp["reliability"] if decomp else None
    avg_pred = (sum(p for p, _ in pairs) / n) if n else None
    empirical = (sum(o for _, o in pairs) / n) if n else None
    # Direction: fail only if the model overstates the tail beyond tolerance.
    overstates = (avg_pred is not None and empirical is not None
                  and avg_pred - empirical > direction_tol)

    reasons = []
    if n < n_min:
        reasons.append(f"insufficient_data(n={n}<{n_min})")
    if band_ece is None or band_ece >= ece_max:
        reasons.append(f"ece({band_ece})>= {ece_max}")
    if reliability is None or reliability >= reliability_max:
        reasons.append(f"reliability({reliability})>= {reliability_max}")
    if overstates:
        reasons.append(
            f"model_overstates_tail(avg_pred={avg_pred:.4f}>emp={empirical:.4f})")

    return {
        "tail_calibration_pass": len(reasons) == 0,
        "n": n,
        "ece": band_ece,
        "reliability": reliability,
        "avg_pred": avg_pred,
        "empirical": empirical,
        "fail_reasons": reasons,
        "reliability_diagram": reliability_diagram(pairs, nbins=50),
    }


def write_gate(verdict: dict, path: Path = GATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(verdict, indent=2, default=str))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--since-days", type=int, default=120,
                   help="Lookback window in days (default 120).")
    p.add_argument("--write", action="store_true",
                   help=f"Write the verdict to {GATE_PATH}.")
    p.add_argument("--output", choices=("text", "json"), default="text")
    args = p.parse_args()

    db.init_db()
    since = (datetime.now(timezone.utc)
             - timedelta(days=args.since_days)).isoformat()
    with db.connect() as conn:
        built = build_pairs(conn, since)
    verdict = evaluate_gate(built["pairs"])
    verdict["generated_at"] = datetime.now(timezone.utc).isoformat()
    verdict["since_iso"] = since
    verdict["n_recomputed"] = built["n_recomputed"]
    verdict["n_skipped"] = built["n_skipped"]

    if args.write:
        write_gate(verdict)

    if args.output == "json":
        print(json.dumps(verdict, indent=2, default=str))
    else:
        status = "PASS" if verdict["tail_calibration_pass"] else "FAIL"
        print(f"Tail-calibration gate [{BAND_LO}, {BAND_HI}]: {status}")
        print(f"  in-band pairs: {verdict['n']} "
              f"(recomputed {built['n_recomputed']}, skipped {built['n_skipped']})")
        print(f"  ECE: {verdict['ece']}  reliability: {verdict['reliability']}")
        print(f"  avg_pred: {verdict['avg_pred']}  empirical: {verdict['empirical']}")
        if verdict["fail_reasons"]:
            print(f"  fail reasons: {', '.join(verdict['fail_reasons'])}")
        if args.write:
            print(f"  wrote {GATE_PATH}")
    return 0


# ---------------------------------------------------------------------------
# Inline tests (offline, synthetic pairs — no DB, no API)
# ---------------------------------------------------------------------------


def _run_tests() -> int:
    # Test 1: well-calibrated tail — model says ~p, YES happens ~p. Build 300
    # pairs spread across [0.02, 0.18] with empirical frequency matching pred.
    pairs = []
    for k in range(300):
        pred = 0.02 + (k % 9) * 0.02       # 0.02..0.18
        # deterministic outcome pattern hitting ~pred frequency per pred level
        outcome = 1 if (k % 100) < round(pred * 100) else 0
        pairs.append((pred, outcome))
    v = evaluate_gate(pairs, n_min=150)
    assert v["n"] == 300, v
    assert v["tail_calibration_pass"] is True, v["fail_reasons"]
    print(f"Test 1 PASS: well-calibrated tail → PASS (ece={v['ece']:.4f})")

    # Test 2: model inflates the tail — says 0.15 but YES almost never happens.
    inflated = [(0.15, 1 if i < 6 else 0) for i in range(300)]  # emp ~0.02
    v = evaluate_gate(inflated, n_min=150)
    assert v["tail_calibration_pass"] is False, v
    assert any("overstates" in r or "ece" in r for r in v["fail_reasons"]), v
    print(f"Test 2 PASS: inflated tail → FAIL ({v['fail_reasons']})")

    # Test 3: insufficient data → FAIL regardless of calibration quality.
    few = [(0.10, 1 if i < 1 else 0) for i in range(10)]
    v = evaluate_gate(few, n_min=150)
    assert v["tail_calibration_pass"] is False, v
    assert any("insufficient_data" in r for r in v["fail_reasons"]), v
    print(f"Test 3 PASS: n=10 < 150 → FAIL ({v['fail_reasons']})")

    # Test 4: model UNDERstates the tail (emp > pred) — allowed (reinforces
    # convexity), not a fail reason on direction; passes if ece/reliability ok.
    under = []
    for k in range(300):
        pred = 0.05
        outcome = 1 if (k % 100) < 6 else 0   # emp ~0.06 > pred 0.05
        under.append((pred, outcome))
    v = evaluate_gate(under, n_min=150)
    assert not any("overstates" in r for r in v["fail_reasons"]), v
    print(f"Test 4 PASS: model understates tail → no 'overstates' fail "
          f"(pass={v['tail_calibration_pass']})")

    print("\nAll cheap_convexity calibration tests PASS")
    return 0


if __name__ == "__main__":
    if "--test" in sys.argv:
        sys.exit(_run_tests())
    sys.exit(main())
