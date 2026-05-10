#!/usr/bin/env python3
"""Weather edge analyzer — counterfactual analysis + threshold suggestions.

Read-only. Run on-demand (not part of the daemon). Joins entries with
cashouts and resolutions, computes counterfactual deltas, aggregates by edge
bucket / TTR bucket / forecast prob bucket, and suggests parameter tweaks.

Usage:
  python weather_edge_analyzer.py [--since YYYY-MM-DD] [--output FORMAT]
                                   [--out PATH] [--recompute]
                                   [--replay-entry ID]
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "polymarket-analyzer" / "scripts"))

import weather_edge_db as db  # noqa: E402


# ---------------------------------------------------------------------------
# Counterfactual computation
# ---------------------------------------------------------------------------


def compute_counterfactuals(conn, recompute: bool = False) -> int:
    """For every entry with both a cashout and a resolution, compute delta
    and upsert into counterfactuals table. Returns count processed."""
    if recompute:
        conn.execute("DELETE FROM counterfactuals")
    rows = db.query_for_counterfactual(conn)
    count = 0
    for r in rows:
        size_shares = float(r["size_shares"] or 0)
        entry_price = float(r["entry_price"] or 0)
        exit_price = float(r["exit_price"] or 0)
        payout = float(r["payout_per_share"] or 0)

        realized_pnl = (exit_price - entry_price) * size_shares
        hypothetical_hold_pnl = (payout - entry_price) * size_shares
        delta = hypothetical_hold_pnl - realized_pnl

        notes = "should_have_held" if delta > 0 else \
                "cashout_correct" if delta < 0 else "neutral"

        db.upsert_counterfactual(
            conn,
            entry_id=r["entry_id"],
            cashout_id=r["cashout_id"],
            realized_pnl=realized_pnl,
            hypothetical_hold_pnl=hypothetical_hold_pnl,
            delta=delta,
            computed_at=datetime.now(timezone.utc).isoformat(),
            notes=notes,
        )
        count += 1
    return count


# ---------------------------------------------------------------------------
# Aggregation buckets
# ---------------------------------------------------------------------------


def _edge_bucket(edge_pp: float) -> str:
    if edge_pp < 10: return "<10pp"
    if edge_pp < 15: return "10-15pp"
    if edge_pp < 20: return "15-20pp"
    return "20pp+"


def _ttr_bucket(ttr_h: float) -> str:
    if ttr_h < 6: return "<6h"
    if ttr_h < 24: return "6-24h"
    return "24-48h"


def _prob_bucket(p: float) -> str:
    if p < 0.30: return "0-30%"
    if p < 0.50: return "30-50%"
    if p < 0.70: return "50-70%"
    return "70-100%"


def aggregate_by_bucket(conn, since_iso: str) -> dict:
    """Compute aggregations for the report."""
    # 1. Trade outcomes
    rows = conn.execute(
        "SELECT e.entry_id, e.edge_pp_at_entry, e.ttr_hours_at_entry, "
        "       e.forecast_prob_at_entry, e.side, e.entry_price, "
        "       cf.realized_pnl, cf.hypothetical_hold_pnl, cf.delta, "
        "       r.payout_per_share, r.final_outcome "
        "FROM entries e "
        "LEFT JOIN counterfactuals cf ON cf.entry_id = e.entry_id "
        "LEFT JOIN resolutions r ON r.entry_id = e.entry_id "
        "WHERE e.ts >= ? AND e.status IN ('EXECUTED','FAST_PATH')",
        (since_iso,),
    ).fetchall()

    by_edge: dict[str, list[dict]] = defaultdict(list)
    by_ttr: dict[str, list[dict]] = defaultdict(list)
    by_forecast_prob: dict[str, dict[str, int]] = defaultdict(lambda: {"yes": 0, "n": 0})

    for r in rows:
        edge_pp = r["edge_pp_at_entry"] or 0
        ttr_h = r["ttr_hours_at_entry"] or 0
        fp = r["forecast_prob_at_entry"] or 0

        by_edge[_edge_bucket(edge_pp)].append(dict(r))
        by_ttr[_ttr_bucket(ttr_h)].append(dict(r))

        # Calibration — only count resolved
        if r["final_outcome"] in ("YES", "NO"):
            won = (r["final_outcome"] == r["side"])
            b = _prob_bucket(fp)
            by_forecast_prob[b]["n"] += 1
            if won:
                by_forecast_prob[b]["yes"] += 1

    return {
        "by_edge": {k: _summarize(v) for k, v in by_edge.items()},
        "by_ttr": {k: _summarize(v) for k, v in by_ttr.items()},
        "calibration": {b: {**v, "win_rate": v["yes"] / v["n"] if v["n"] else None}
                        for b, v in by_forecast_prob.items()},
    }


def _summarize(rows: list[dict]) -> dict:
    n = len(rows)
    if n == 0:
        return {"n": 0}
    deltas = [r["delta"] for r in rows if r.get("delta") is not None]
    realized = [r["realized_pnl"] for r in rows if r.get("realized_pnl") is not None]
    held = [r["hypothetical_hold_pnl"] for r in rows
            if r.get("hypothetical_hold_pnl") is not None]
    cashout_subopt = sum(1 for d in deltas if d > 0)
    return {
        "n": n,
        "n_with_cashout": len(deltas),
        "pct_should_have_held": round(100 * cashout_subopt / len(deltas), 1) if deltas else None,
        "mean_delta_usd": round(sum(deltas) / len(deltas), 2) if deltas else None,
        "total_realized_pnl_usd": round(sum(realized), 2) if realized else 0,
        "total_held_pnl_usd": round(sum(held), 2) if held else 0,
    }


# ---------------------------------------------------------------------------
# Judge calibration
# ---------------------------------------------------------------------------


def aggregate_judge(conn, since_iso: str) -> dict:
    """Aggregate judge verdicts and reject-counterfactual."""
    rows = conn.execute(
        "SELECT j.verdict, j.judge_prob, j.bot_prob, j.cost_usd, "
        "       e.entry_id, e.status, e.entry_price, e.side, "
        "       r.final_outcome, r.payout_per_share "
        "FROM judge_reviews j "
        "JOIN entries e ON e.entry_id = j.entry_id "
        "LEFT JOIN resolutions r ON r.entry_id = e.entry_id "
        "WHERE j.ts >= ?",
        (since_iso,),
    ).fetchall()

    n_total = len(rows)
    by_verdict = defaultdict(int)
    total_cost = 0.0
    judge_calibration: dict[str, dict[str, int]] = defaultdict(lambda: {"yes": 0, "n": 0})
    reject_value: list[float] = []

    for r in rows:
        by_verdict[r["verdict"]] += 1
        total_cost += r["cost_usd"] or 0
        if r["final_outcome"] in ("YES", "NO") and r["judge_prob"] is not None:
            jp = float(r["judge_prob"])
            b = _prob_bucket(jp)
            judge_calibration[b]["n"] += 1
            if r["final_outcome"] == r["side"]:
                judge_calibration[b]["yes"] += 1
            # Reject counterfactual: if rejected and won, that was a missed trade
            if r["verdict"] == "REJECT" and r["payout_per_share"] is not None:
                hypothetical_pnl = (float(r["payout_per_share"]) -
                                    float(r["entry_price"] or 0)) * 100  # assume $100 size
                reject_value.append(hypothetical_pnl)

    return {
        "n_reviews": n_total,
        "verdict_distribution": dict(by_verdict),
        "total_cost_usd": round(total_cost, 2),
        "calibration": {b: {**v, "win_rate": v["yes"] / v["n"] if v["n"] else None}
                        for b, v in judge_calibration.items()},
        "reject_counterfactual": {
            "n_rejects_resolved": len(reject_value),
            "missed_pnl_estimate": round(sum(reject_value), 2) if reject_value else 0,
            "note": "PnL assumes hypothetical $100 size; relative comparisons valid.",
        },
    }


# ---------------------------------------------------------------------------
# Suggestions
# ---------------------------------------------------------------------------


def generate_suggestions(buckets: dict, judge: dict) -> list[str]:
    """Heuristic threshold suggestions based on aggregations."""
    s: list[str] = []

    # Edge bucket: if 10-15pp shows < 50% win rate, suggest raising
    by_edge = buckets["by_edge"]
    if "10-15pp" in by_edge and by_edge["10-15pp"]["n"] >= 5:
        held = by_edge["10-15pp"]["total_held_pnl_usd"]
        if held < 0:
            s.append(
                f"⚠️  Edge bucket 10-15pp ({by_edge['10-15pp']['n']} trades) total held "
                f"P&L = ${held:.2f}. Consider raising --min-edge-pp to 15."
            )

    # Calibration: forecast prob vs actual win rate
    cal = buckets["calibration"]
    for bucket, c in cal.items():
        if c["n"] >= 5 and c["win_rate"] is not None:
            mid = {"0-30%": 0.15, "30-50%": 0.40, "50-70%": 0.60, "70-100%": 0.85}[bucket]
            gap = c["win_rate"] - mid
            if abs(gap) > 0.10:
                direction = "over-calibrated (forecasts too confident)" if gap < 0 \
                    else "under-calibrated (forecasts too cautious)"
                s.append(
                    f"📊 Calibration gap in {bucket} bucket: forecasts ≈ {mid:.0%} but "
                    f"observed win rate = {c['win_rate']:.0%} ({c['n']} resolved). "
                    f"Forecast probabilities are {direction}. Consider increasing MAE_TEMP_F."
                )

    # TTR bucket: if cashouts in <6h were mostly suboptimal, suggest holding longer
    by_ttr = buckets["by_ttr"]
    for tt_bucket, v in by_ttr.items():
        if v.get("pct_should_have_held") is not None and v["pct_should_have_held"] > 60:
            s.append(
                f"🤔 In TTR bucket {tt_bucket}, {v['pct_should_have_held']:.0f}% of "
                f"cashouts would have been better held. Consider tightening cashout "
                f"trigger (require bid >= entry_price + small buffer) for this bucket."
            )

    # Judge: over-rejecting
    rej_value = judge.get("reject_counterfactual", {}).get("missed_pnl_estimate", 0)
    if rej_value > 50:
        s.append(
            f"🔍 Judge rejections forfeited an estimated ${rej_value:.2f} in hypothetical "
            f"P&L (assuming $100 size each). Review weather-judge-prompt.md for over-rejection."
        )

    if not s:
        s.append("✅ No anomalous patterns detected. Sample size may be small; re-run after more trades.")

    return s


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def replay_entry(conn, entry_id: int) -> str:
    """Return a markdown summary of one entry's snapshot + decision flow."""
    e = conn.execute("SELECT * FROM entries WHERE entry_id = ?",
                     (entry_id,)).fetchone()
    if not e:
        return f"Entry {entry_id} not found."

    out = [f"# Replay — Entry {entry_id}", ""]
    out.append(f"- Market: {e['market_question']}")
    out.append(f"- Slug: {e['market_slug']}")
    out.append(f"- City: {e['city_resolved']}")
    out.append(f"- Threshold: {e['threshold_value']} {e['threshold_unit']} ({e['comparison']})")
    out.append(f"- Side: {e['side']}, Entry price: {e['entry_price']}, "
               f"Edge: {e['edge_pp_at_entry']}pp")
    out.append(f"- Status: {e['status']}")
    out.append("")

    out.append("## OpenWeather forecast snapshot at entry")
    out.append("```json")
    out.append(json.dumps(json.loads(e["forecast_snapshot_json"] or "{}"),
                          indent=2, default=str))
    out.append("```")

    j = conn.execute("SELECT * FROM judge_reviews WHERE entry_id = ?",
                     (entry_id,)).fetchone()
    if j:
        out.append(f"\n## Judge verdict: {j['verdict']} (confidence {j['confidence']:.2f})")
        out.append(f"- judge_prob: {j['judge_prob']:.3f} (vs bot {j['bot_prob']:.3f}, "
                   f"delta {j['prob_delta']:.3f})")
        out.append(f"- Rationale: {j['rationale']}")
        out.append(f"- Cost: ${j['cost_usd']:.4f}")

    co = conn.execute("SELECT * FROM cashouts WHERE entry_id = ?",
                      (entry_id,)).fetchone()
    if co:
        out.append(f"\n## Cashout: {co['ts']}")
        out.append(f"- Exit price: {co['exit_price']}, shares: {co['exit_shares']}")
        out.append(f"- Realized PnL: ${co['realized_pnl_usd']:.2f}")
        out.append(f"- Reason: {co['reason']}")

    r = conn.execute("SELECT * FROM resolutions WHERE entry_id = ?",
                     (entry_id,)).fetchone()
    if r:
        out.append(f"\n## Resolution: {r['final_outcome']}")
        out.append(f"- Payout: {r['payout_per_share']}")

    cf = conn.execute("SELECT * FROM counterfactuals WHERE entry_id = ?",
                      (entry_id,)).fetchone()
    if cf:
        out.append(f"\n## Counterfactual")
        out.append(f"- Realized: ${cf['realized_pnl']:.2f}")
        out.append(f"- Held to resolution: ${cf['hypothetical_hold_pnl']:.2f}")
        out.append(f"- Delta: ${cf['delta']:.2f} ({cf['notes']})")

    return "\n".join(out)


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def format_report_md(buckets: dict, judge: dict, suggestions: list[str],
                     since_iso: str) -> str:
    out = [
        "# Weather Edge Bot — Analysis Report",
        "",
        f"_Generated {datetime.now(timezone.utc).isoformat()}_",
        f"_Window: since {since_iso}_",
        "",
        "## By Edge Bucket",
        "| Bucket | N | N w/cashout | % should have held | Mean delta $ | Total realized $ | Total held $ |",
        "|---|---|---|---|---|---|---|",
    ]
    for bucket, v in sorted(buckets["by_edge"].items()):
        out.append(f"| {bucket} | {v['n']} | {v.get('n_with_cashout', 0)} | "
                   f"{v.get('pct_should_have_held', '-')} | "
                   f"{v.get('mean_delta_usd', '-')} | "
                   f"{v.get('total_realized_pnl_usd', 0)} | "
                   f"{v.get('total_held_pnl_usd', 0)} |")

    out.append("\n## By TTR Bucket")
    out.append("| Bucket | N | % should have held | Mean delta $ |")
    out.append("|---|---|---|---|")
    for bucket, v in sorted(buckets["by_ttr"].items()):
        out.append(f"| {bucket} | {v['n']} | {v.get('pct_should_have_held', '-')} | "
                   f"{v.get('mean_delta_usd', '-')} |")

    out.append("\n## Forecast Probability Calibration")
    out.append("| Bucket | N resolved | Wins | Win rate | Expected if calibrated |")
    out.append("|---|---|---|---|---|")
    expected = {"0-30%": "<30%", "30-50%": "30-50%", "50-70%": "50-70%", "70-100%": ">70%"}
    for bucket, v in sorted(buckets["calibration"].items()):
        wr = v.get("win_rate")
        wr_str = f"{wr * 100:.0f}%" if wr is not None else "-"
        out.append(f"| {bucket} | {v['n']} | {v['yes']} | {wr_str} | "
                   f"{expected.get(bucket, '-')} |")

    out.append("\n## Judge Performance")
    out.append(f"- Total reviews: {judge['n_reviews']}")
    out.append(f"- Total cost: ${judge['total_cost_usd']:.2f}")
    out.append(f"- Verdict distribution: {judge['verdict_distribution']}")
    out.append(f"- Reject counterfactual: {judge['reject_counterfactual']}")

    out.append("\n## Suggestions")
    for s in suggestions:
        out.append(f"- {s}")

    return "\n".join(out)


def format_csv(buckets: dict, path: Path) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["category", "bucket", "n", "metric", "value"])
        for bucket, v in buckets["by_edge"].items():
            for k, val in v.items():
                w.writerow(["by_edge", bucket, v["n"], k, val])
        for bucket, v in buckets["by_ttr"].items():
            for k, val in v.items():
                w.writerow(["by_ttr", bucket, v["n"], k, val])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--since", default=None,
                   help="YYYY-MM-DD (default: 30 days ago)")
    p.add_argument("--output", choices=("report-md", "json", "csv"),
                   default="report-md")
    p.add_argument("--out", default=None, help="Output file path")
    p.add_argument("--recompute", action="store_true",
                   help="Re-run counterfactual computation")
    p.add_argument("--replay-entry", type=int,
                   help="Print replay for one entry_id and exit")
    args = p.parse_args()

    since = args.since or (datetime.now(timezone.utc) -
                           timedelta(days=30)).date().isoformat()
    since_iso = f"{since}T00:00:00+00:00"

    with db.connect() as conn:
        if args.replay_entry is not None:
            print(replay_entry(conn, args.replay_entry))
            return

        n_cf = compute_counterfactuals(conn, recompute=args.recompute)
        print(f"Counterfactuals computed/refreshed: {n_cf}", file=sys.stderr)

        buckets = aggregate_by_bucket(conn, since_iso)
        judge = aggregate_judge(conn, since_iso)
        suggestions = generate_suggestions(buckets, judge)

    if args.output == "json":
        report = {"since": since, "buckets": buckets, "judge": judge,
                  "suggestions": suggestions}
        text = json.dumps(report, indent=2, default=str)
    elif args.output == "csv":
        out_path = Path(args.out or "weather_edge_report.csv")
        format_csv(buckets, out_path)
        print(f"CSV written to {out_path}", file=sys.stderr)
        return
    else:
        text = format_report_md(buckets, judge, suggestions, since_iso)

    if args.out:
        Path(args.out).write_text(text)
        print(f"Wrote {args.out}", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()
