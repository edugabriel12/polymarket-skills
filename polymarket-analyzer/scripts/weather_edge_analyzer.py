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
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

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


def aggregate_by_bucket(conn, since_iso: str,
                        strategy: Optional[str] = None) -> dict:
    """Compute aggregations for the report.

    v11: `strategy` is an OPTIONAL filter. Default None aggregates ALL
    strategies (unchanged behavior). Pass 'weather_edge' to exclude the
    cheap_convexity strategy from the tuned bot's KPIs, or 'cheap_convexity'
    to isolate it for separate analysis. NULL strategy counts as
    'weather_edge'."""
    # 1. Trade outcomes
    q = ("SELECT e.entry_id, e.edge_pp_at_entry, e.ttr_hours_at_entry, "
         "       e.forecast_prob_at_entry, e.side, e.entry_price, "
         "       cf.realized_pnl, cf.hypothetical_hold_pnl, cf.delta, "
         "       r.payout_per_share, r.final_outcome "
         "FROM entries e "
         "LEFT JOIN counterfactuals cf ON cf.entry_id = e.entry_id "
         "LEFT JOIN resolutions r ON r.entry_id = e.entry_id "
         "WHERE e.ts >= ? AND e.status IN ('EXECUTED','FAST_PATH')")
    params: tuple = (since_iso,)
    if strategy is not None:
        q += " AND COALESCE(e.strategy, 'weather_edge') = ?"
        params = (since_iso, strategy)
    rows = conn.execute(q, params).fetchall()

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
# Cashout trigger aggregation
# ---------------------------------------------------------------------------


def aggregate_cashout_triggers(conn, since_iso: str,
                               strategy: Optional[str] = None) -> dict:
    """Group cashouts by which trigger fired (parsed from decision_reason of
    the monitor_check that immediately preceded the cashout). Returns per-
    trigger counts + mean realized PnL.

    Trigger name is the prefix of monitor_check.decision_reason before ':'
    (set by the bot as f'{trigger}: {reason}').

    v11: optional `strategy` filter (default None = all). cashouts has no
    strategy column, so it JOINs entries to filter (NULL strategy counts as
    'weather_edge')."""
    q = ("SELECT c.cashout_id, c.realized_pnl_usd, c.ts, c.entry_id, "
         "       (SELECT decision_reason FROM monitor_checks m "
         "        WHERE m.entry_id = c.entry_id AND m.decision = 'CASHOUT' "
         "        ORDER BY m.ts DESC LIMIT 1) AS reason "
         "FROM cashouts c WHERE c.ts >= ?")
    params: tuple = (since_iso,)
    if strategy is not None:
        q += (" AND EXISTS (SELECT 1 FROM entries e WHERE e.entry_id = "
              "c.entry_id AND COALESCE(e.strategy,'weather_edge') = ?)")
        params = (since_iso, strategy)
    rows = conn.execute(q, params).fetchall()

    by_trigger: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        reason = (r["reason"] or "").strip()
        trigger = reason.split(":", 1)[0].strip() if ":" in reason else "unknown"
        if not trigger:
            trigger = "unknown"
        pnl = r["realized_pnl_usd"]
        if pnl is not None:
            by_trigger[trigger].append(float(pnl))

    out = {}
    for trigger, pnls in by_trigger.items():
        out[trigger] = {
            "n": len(pnls),
            "total_pnl_usd": round(sum(pnls), 2),
            "mean_pnl_usd": round(sum(pnls) / len(pnls), 2) if pnls else 0,
            "min_pnl_usd": round(min(pnls), 2) if pnls else 0,
            "max_pnl_usd": round(max(pnls), 2) if pnls else 0,
        }
    return out


# ---------------------------------------------------------------------------
# Judge calibration
# ---------------------------------------------------------------------------


def aggregate_judge(conn, since_iso: str,
                    strategy: Optional[str] = None) -> dict:
    """Aggregate judge verdicts and reject-counterfactual.

    v11: optional `strategy` filter (default None = all). entries is already
    in the JOIN, so it just adds a predicate (NULL counts as 'weather_edge')."""
    q = ("SELECT j.verdict, j.judge_prob, j.bot_prob, j.cost_usd, "
         "       e.entry_id, e.status, e.entry_price, e.side, "
         "       r.final_outcome, r.payout_per_share "
         "FROM judge_reviews j "
         "JOIN entries e ON e.entry_id = j.entry_id "
         "LEFT JOIN resolutions r ON r.entry_id = e.entry_id "
         "WHERE j.ts >= ?")
    params: tuple = (since_iso,)
    if strategy is not None:
        q += " AND COALESCE(e.strategy,'weather_edge') = ?"
        params = (since_iso, strategy)
    rows = conn.execute(q, params).fetchall()

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


def compute_discovery_meta_breakdown(conn, since_iso: str) -> dict:
    """v8: cohort win-rate breakdown by discovery metadata (mae_dynamic,
    bias, station, multi_source, open_meteo, om_spread_penalty).

    Lets the advisor answer questions like:
      - Did v7 dynamic MAE help? Compare win_rate of trades where
        mae_dynamic > base × 1.5 vs trades where mae_dynamic == base.
      - Did v8 station coords help? Compare win_rate per station (KLGA
        vs auto-resolved vs legacy-geocoded).
      - Is the Open-Meteo disagreement penalty too aggressive? Compare
        win_rate of om_spread_penalty=True vs False.
      - Is the min-TTR filter actually saving us from losses? Compare
        recent discovery_skips with reason='ttr_below_min' against
        accepted trades in similar TTR ranges.

    Returns a nested dict:
      {
        "n_total_resolved": int,
        "by_station": {"KLGA": {"n": 12, "wins": 8, "win_rate": 0.67}, ...},
        "by_mae_bucket": {"base": {...}, "1.5x": {...}, "2x+": {...}},
        "by_multi_source": {"true": {...}, "false": {...}},
        "by_om_penalty": {"true": {...}, "false": {...}},
        "by_bias_applied": {"true": {...}, "false": {...}},
        "auto_station_resolves": {"n": int, "wins": int, "win_rate": float},
        "skips_breakdown": {
            "ttr_below_min": {"n": int, "avg_ttr_h": float, "min_ttr": float}
        },
        "interpretation": [str, ...],  # human-readable flags
      }
    """
    # Helper: bucket label for mae_dynamic / base_mae ratio
    def _mae_bucket(ratio: float) -> str:
        if ratio is None or ratio <= 1.05:
            return "base"
        if ratio <= 1.5:
            return "1.5x"
        if ratio <= 2.0:
            return "2x"
        return "2x+"

    # Pull resolved entries with discovery_meta_json + resolution outcome
    try:
        rows = conn.execute(
            """
            SELECT e.entry_id, e.side, e.city_resolved, e.ts,
                   e.discovery_meta_json,
                   r.payout_per_share,
                   CASE WHEN r.payout_per_share > 0 THEN 1 ELSE 0 END AS won
            FROM entries e
            JOIN resolutions r USING(entry_id)
            WHERE e.ts >= ? AND e.status = 'EXECUTED'
              AND e.discovery_meta_json IS NOT NULL
            """,
            (since_iso,),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []  # discovery_meta_json column missing pre-v8

    by_station: dict[str, dict] = defaultdict(lambda: {"n": 0, "wins": 0})
    by_mae: dict[str, dict] = defaultdict(lambda: {"n": 0, "wins": 0})
    by_multi: dict[str, dict] = defaultdict(lambda: {"n": 0, "wins": 0})
    by_om_pen: dict[str, dict] = defaultdict(lambda: {"n": 0, "wins": 0})
    by_bias: dict[str, dict] = defaultdict(lambda: {"n": 0, "wins": 0})
    auto_n = 0
    auto_wins = 0

    for r in rows:
        try:
            meta = json.loads(r["discovery_meta_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            meta = {}
        won = int(r["won"])

        station = meta.get("station") or "geocoded"
        by_station[station]["n"] += 1
        by_station[station]["wins"] += won

        base = float(meta.get("base_mae") or 0)
        dyn = float(meta.get("mae_dynamic") or 0)
        ratio = (dyn / base) if base > 0 else None
        bk = _mae_bucket(ratio)
        by_mae[bk]["n"] += 1
        by_mae[bk]["wins"] += won

        ms_key = "true" if meta.get("multi_source") else "false"
        by_multi[ms_key]["n"] += 1
        by_multi[ms_key]["wins"] += won

        om_pen_key = "true" if meta.get("om_spread_penalty") else "false"
        by_om_pen[om_pen_key]["n"] += 1
        by_om_pen[om_pen_key]["wins"] += won

        bias_applied = meta.get("bias") is not None and meta.get("bias") != 0
        bk_key = "true" if bias_applied else "false"
        by_bias[bk_key]["n"] += 1
        by_bias[bk_key]["wins"] += won

        # v9: auto-extract resolved entries carry _source="auto" inside
        # the station entry dict; if not propagated to meta, skip.
        if meta.get("station_source") == "auto":
            auto_n += 1
            auto_wins += won

    def _finalize(d: dict) -> dict:
        out = {}
        for k, v in d.items():
            n = v["n"]
            out[k] = {
                "n": n, "wins": v["wins"],
                "win_rate": round(v["wins"] / n, 3) if n else None,
            }
        return out

    # Discovery skips breakdown
    skips_breakdown: dict[str, dict] = {}
    try:
        skip_rows = conn.execute(
            """
            SELECT reason, COUNT(*) AS n, meta_json
            FROM discovery_skips
            WHERE ts >= ?
            GROUP BY reason
            """,
            (since_iso,),
        ).fetchall()
        for sr in skip_rows:
            skips_breakdown[sr["reason"]] = {"n": sr["n"]}
        # For ttr_below_min, compute avg_ttr_h from meta
        ttr_meta_rows = conn.execute(
            "SELECT meta_json FROM discovery_skips "
            "WHERE reason = 'ttr_below_min' AND ts >= ?",
            (since_iso,),
        ).fetchall()
        ttrs = []
        for mr in ttr_meta_rows:
            try:
                m = json.loads(mr["meta_json"] or "{}")
                if m.get("ttr_h") is not None:
                    ttrs.append(float(m["ttr_h"]))
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        if ttrs:
            skips_breakdown.setdefault("ttr_below_min", {})["avg_ttr_h"] = (
                round(sum(ttrs) / len(ttrs), 2))
            skips_breakdown["ttr_below_min"]["min_ttr_h"] = round(min(ttrs), 2)
            skips_breakdown["ttr_below_min"]["max_ttr_h"] = round(max(ttrs), 2)
    except sqlite3.OperationalError:
        pass  # discovery_skips table missing pre-v8

    # Interpretation: human-readable flags for the advisor LLM
    interpretation: list[str] = []
    fmt = _finalize(by_station)
    if len(fmt) >= 3:
        worst = min((k for k in fmt if fmt[k]["win_rate"] is not None),
                    key=lambda k: fmt[k]["win_rate"], default=None)
        best = max((k for k in fmt if fmt[k]["win_rate"] is not None),
                    key=lambda k: fmt[k]["win_rate"], default=None)
        if worst and best and worst != best:
            gap = fmt[best]["win_rate"] - fmt[worst]["win_rate"]
            if gap > 0.15:
                interpretation.append(
                    f"Station gap: best={best} ({fmt[best]['win_rate']*100:.0f}%) "
                    f"vs worst={worst} ({fmt[worst]['win_rate']*100:.0f}%). "
                    f"Investigate worst-station coords/bias.")

    fm = _finalize(by_mae)
    if "base" in fm and "2x+" in fm and fm["base"]["n"] >= 5 and fm["2x+"]["n"] >= 5:
        base_wr = fm["base"]["win_rate"]
        high_wr = fm["2x+"]["win_rate"]
        if base_wr is not None and high_wr is not None and high_wr > base_wr + 0.10:
            interpretation.append(
                f"Dynamic MAE working: 2x+ inflation cohort win_rate "
                f"{high_wr*100:.0f}% > base {base_wr*100:.0f}% — volatile "
                f"forecasts ARE less reliable; v7 correctly down-weights them.")
        elif base_wr is not None and high_wr is not None and high_wr < base_wr - 0.10:
            interpretation.append(
                f"Dynamic MAE possibly over-cautious: 2x+ cohort {high_wr*100:.0f}% < "
                f"base {base_wr*100:.0f}%. Re-check std_multiplier (currently 1.5).")

    return {
        "n_total_resolved": len(rows),
        "by_station": _finalize(by_station),
        "by_mae_bucket": _finalize(by_mae),
        "by_multi_source": _finalize(by_multi),
        "by_om_penalty": _finalize(by_om_pen),
        "by_bias_applied": _finalize(by_bias),
        "auto_station_resolves": {
            "n": auto_n, "wins": auto_wins,
            "win_rate": round(auto_wins / auto_n, 3) if auto_n else None,
        },
        "skips_breakdown": skips_breakdown,
        "interpretation": interpretation,
    }


def compute_ladder_breakdown(conn, since_iso: str) -> dict:
    """v9: ladder formation + performance cohort analysis.

    Reports:
      - Formation funnel: events that became 3-bin, 2-bin, single-bin,
        and how many died in atomic gate (sibling_failed, partial_failure)
      - Performance: ladder groups vs single-bin orphans — total stake,
        realized P&L, win rate, P&L per dollar
      - Short-TTR cohort (6-12h): whether the new admittance band is
        actually producing edge as predicted
      - Kelly distribution: % of legs that got non-zero stake (vs Kelly-
        capped to 0), central vs adjacent share of stake

    Used by the advisor to detect "is laddering paying off?" and tune
    the per-mode floors if a cohort is underperforming.
    """
    # Defensive: pre-v9 schemas don't have ladder columns. Return an
    # empty-but-well-formed payload so the advisor can degrade gracefully.
    try:
        conn.execute("SELECT ladder_group_id FROM entries LIMIT 1")
    except Exception:
        return {
            "schema_status": "pre_v9_no_ladder_columns",
            "formation_funnel": {"3bin_full": 0, "2bin_partial": 0,
                                  "single_orphan": 0},
            "atomic_gate_failures": {},
            "ladder_groups_performance": {},
            "single_bin_performance": {},
            "ttr_cohort_performance": {},
            "kelly_distribution_by_position": {},
            "interpretation": ["DB schema is pre-v9 (no ladder_group_id "
                                "column). Ladder analytics unavailable until "
                                "schema migrates."],
        }

    # Formation funnel — count by ladder structure
    funnel = {"3bin_full": 0, "2bin_partial": 0, "single_orphan": 0}
    rows = conn.execute(
        "SELECT ladder_group_id, COUNT(*) n "
        "FROM entries WHERE ts >= ? AND ladder_group_id IS NOT NULL "
        "GROUP BY ladder_group_id",
        (since_iso,)
    ).fetchall()
    for r in rows:
        n = r["n"]
        if n >= 3:
            funnel["3bin_full"] += 1
        elif n == 2:
            funnel["2bin_partial"] += 1
    funnel["single_orphan"] = conn.execute(
        "SELECT COUNT(*) FROM entries WHERE ts >= ? AND ladder_group_id IS NULL "
        "AND status NOT IN ('REJECTED','SKIPPED')",
        (since_iso,)
    ).fetchone()[0]

    # Atomic gate failures
    atomic_failures = {}
    for reason in ("ladder_sibling_failed", "ladder_partial_failure"):
        atomic_failures[reason] = conn.execute(
            "SELECT COUNT(*) FROM entries WHERE ts >= ? AND skip_reason = ?",
            (since_iso, reason)
        ).fetchone()[0]
    atomic_failures["ladder_aborted_any"] = conn.execute(
        "SELECT COUNT(*) FROM entries WHERE ts >= ? "
        "AND skip_reason LIKE 'ladder_aborted:%'",
        (since_iso,)
    ).fetchone()[0]

    # Performance: ladder groups vs orphan single-bin (settled only).
    # v13.8 (2026-07-05): "settled" now includes RESOLUTIONS, not just
    # cashouts — same resolution-blindness fixed in v13.6 (Win Rate by
    # City) and v13.7 (Ladders tab). Per-leg P&L precedence: cashout
    # (realized governs; covers legs with cashout AND resolution without
    # double count) → resolution ((payout - entry) * shares).
    ladder_perf = conn.execute("""
        SELECT
          COUNT(*) n_groups,
          SUM(group_pnl) total_pnl,
          SUM(group_stake) total_stake,
          SUM(CASE WHEN group_pnl > 0 THEN 1 ELSE 0 END) groups_won
        FROM (
          SELECT e.ladder_group_id,
                 SUM(COALESCE(c.realized_pnl_usd,
                              (r.payout_per_share - e.entry_price)
                                * e.size_shares)) AS group_pnl,
                 SUM(e.size_usd) AS group_stake
          FROM entries e
          LEFT JOIN cashouts c ON c.entry_id = e.entry_id
          LEFT JOIN resolutions r ON r.entry_id = e.entry_id
          WHERE e.ts >= ? AND e.ladder_group_id IS NOT NULL
            AND e.status IN ('EXECUTED', 'FAST_PATH')
            AND (c.cashout_id IS NOT NULL OR r.resolution_id IS NOT NULL)
          GROUP BY e.ladder_group_id
          HAVING group_pnl IS NOT NULL
        ) g
    """, (since_iso,)).fetchone()

    single_perf = conn.execute("""
        SELECT COUNT(*) n_trades,
               SUM(COALESCE(c.realized_pnl_usd,
                            (r.payout_per_share - e.entry_price)
                              * e.size_shares)) total_pnl,
               SUM(e.size_usd) total_stake,
               SUM(CASE WHEN COALESCE(c.realized_pnl_usd,
                                      (r.payout_per_share - e.entry_price)
                                        * e.size_shares) > 0
                        THEN 1 ELSE 0 END) wins
        FROM entries e
        LEFT JOIN cashouts c ON c.entry_id = e.entry_id
        LEFT JOIN resolutions r ON r.entry_id = e.entry_id
        WHERE e.ts >= ? AND e.ladder_group_id IS NULL
          AND e.status IN ('EXECUTED', 'FAST_PATH')
          AND (c.cashout_id IS NOT NULL OR r.resolution_id IS NOT NULL)
    """, (since_iso,)).fetchone()

    def _perf_dict(row, label_n: str, label_won: str):
        n = (row[label_n] or 0) if row else 0
        won = (row[label_won] or 0) if row else 0
        pnl = float(row["total_pnl"] or 0) if row else 0.0
        stake = float(row["total_stake"] or 0) if row else 0.0
        return {
            "n": n, "won": won,
            "win_rate": round(won / n, 3) if n else None,
            "total_pnl_usd": round(pnl, 2),
            "total_stake_usd": round(stake, 2),
            "pnl_per_dollar": round(pnl / stake, 4) if stake else None,
        }

    # TTR cohort — does the new 6-12h band actually deliver?
    ttr_cohort = {}
    for label, lo, hi in (("6-12h", 6, 12), ("12-24h", 12, 24),
                           ("24-48h", 24, 48), ("48h+", 48, 9999)):
        # v13.8: settled = cashout OR resolution (cashout precedence).
        r = conn.execute("""
            SELECT COUNT(*) n,
                   SUM(COALESCE(c.realized_pnl_usd,
                                (r.payout_per_share - e.entry_price)
                                  * e.size_shares)) pnl,
                   SUM(e.size_usd) stake,
                   SUM(CASE WHEN COALESCE(c.realized_pnl_usd,
                                          (r.payout_per_share - e.entry_price)
                                            * e.size_shares) > 0
                            THEN 1 ELSE 0 END) wins
            FROM entries e
            LEFT JOIN cashouts c ON c.entry_id = e.entry_id
            LEFT JOIN resolutions r ON r.entry_id = e.entry_id
            WHERE e.ts >= ? AND e.status IN ('EXECUTED', 'FAST_PATH')
              AND (c.cashout_id IS NOT NULL OR r.resolution_id IS NOT NULL)
              AND e.ttr_hours_at_entry >= ? AND e.ttr_hours_at_entry < ?
        """, (since_iso, lo, hi)).fetchone()
        ttr_cohort[label] = {
            "n": r["n"] or 0, "wins": r["wins"] or 0,
            "win_rate": round((r["wins"] or 0) / r["n"], 3) if r["n"] else None,
            "total_pnl_usd": round(float(r["pnl"] or 0), 2),
            "total_stake_usd": round(float(r["stake"] or 0), 2),
        }

    # Kelly distribution: how often does adjacent leg get non-zero stake?
    kelly_dist = conn.execute("""
        SELECT ladder_position,
               COUNT(*) n_legs,
               SUM(CASE WHEN ladder_stake_usd > 0 THEN 1 ELSE 0 END) n_nonzero,
               AVG(ladder_stake_usd) avg_stake_usd
        FROM entries
        WHERE ts >= ? AND ladder_position IS NOT NULL
        GROUP BY ladder_position
    """, (since_iso,)).fetchall()
    kelly_by_position = {}
    for r in kelly_dist:
        n = r["n_legs"]
        nz = r["n_nonzero"] or 0
        kelly_by_position[r["ladder_position"]] = {
            "n_legs": n,
            "n_kelly_nonzero": nz,
            "pct_nonzero": round(nz / n * 100, 1) if n else None,
            "avg_stake_usd": round(float(r["avg_stake_usd"] or 0), 2),
        }

    interpretation = []
    if funnel["3bin_full"] + funnel["2bin_partial"] == 0:
        interpretation.append("NO LADDERS FORMED in window — check "
                              "discovery filters or --ladder-mode setting")
    elif funnel["single_orphan"] > 3 * (funnel["3bin_full"] + funnel["2bin_partial"]):
        interpretation.append("Single-bin orphans dominate ladders 3:1 — most "
                              "events have only one bracket surviving filters; "
                              "consider lowering --ladder-min-leg-edge-pp or "
                              "--ladder-min-leg-price further")
    if atomic_failures["ladder_aborted_any"] > funnel["3bin_full"]:
        interpretation.append("Atomic gate aborting more ladders than "
                              "completing — most aborts are pre-execution "
                              "(edge_stale, no_orderbook); check pre-judge "
                              "threshold or executor floors")
    if (ttr_cohort["6-12h"]["n"] >= 5
            and ttr_cohort["6-12h"]["win_rate"] is not None
            and ttr_cohort["6-12h"]["win_rate"] < 0.45):
        interpretation.append(
            f"6-12h TTR cohort win rate {ttr_cohort['6-12h']['win_rate']:.0%} "
            "is below baseline — the lower --ladder-min-ttr-hours may be "
            "admitting unprofitable adverse-selection trades")

    return {
        "formation_funnel": funnel,
        "atomic_gate_failures": atomic_failures,
        "ladder_groups_performance": _perf_dict(
            ladder_perf, "n_groups", "groups_won"),
        "single_bin_performance": _perf_dict(
            single_perf, "n_trades", "wins"),
        "ttr_cohort_performance": ttr_cohort,
        "kelly_distribution_by_position": kelly_by_position,
        "interpretation": interpretation,
    }


HIGH_CONFIDENCE_THRESHOLD = 0.7


def _is_high_confidence(conf) -> bool:
    """judge_reviews.confidence is a float in [0, 1] (schema type REAL).
    Treat >= 0.7 as 'high'. Tolerate legacy string rows ('high'/'medium'/
    'low') in case any old DB carried the pre-numeric format."""
    if conf is None:
        return False
    if isinstance(conf, (int, float)):
        return float(conf) >= HIGH_CONFIDENCE_THRESHOLD
    return str(conf).lower() == "high"


def compute_judge_accuracy(conn, since_iso: str) -> dict:
    """v6: Judge accuracy + hallucination signals.

    For each RESOLVED entry the judge reviewed, compare the judge's verdict
    and probability against the actual Polymarket outcome. Produce:

      - approval_rate (APPROVE+ADJUST / total)
      - false_positive_rate: of APPROVED+ADJUSTED resolved trades, fraction
        that lost (resolved against the bet side)
      - false_negative_rate: of REJECTED resolved trades, fraction that
        would have won (resolved on the bet side)
      - brier_score: mean squared error of judge_prob vs actual outcome
      - log_loss: cross-entropy of judge_prob vs actual outcome
      - calibration_buckets: 10 bins of judge_prob → actual win rate
      - high_confidence_errors: count of APPROVE+confidence='high' that lost
      - missed_pnl_usd: sum of hypothetical P&L from rejected winners

    These feed the advisor so it can diagnose miscalibration / hallucination.
    """
    rows = conn.execute(
        "SELECT j.verdict, j.confidence, j.judge_prob, j.bot_prob, "
        "       j.rationale, j.evidence_json, "
        "       e.entry_id, e.entry_price, e.side, e.size_usd, "
        "       e.market_question, e.city_resolved, "
        "       r.final_outcome, r.payout_per_share "
        "FROM judge_reviews j "
        "JOIN entries e ON e.entry_id = j.entry_id "
        "LEFT JOIN resolutions r ON r.entry_id = e.entry_id "
        "WHERE j.ts >= ?",
        (since_iso,),
    ).fetchall()

    n_total = len(rows)
    if n_total == 0:
        return {"n_reviews": 0, "note": "no judge reviews in window"}

    n_approve = sum(1 for r in rows if r["verdict"] in ("APPROVE", "ADJUST"))

    # Resolved subset (only trades with a known outcome can be scored)
    resolved = [r for r in rows
                if r["final_outcome"] in ("YES", "NO")
                and r["judge_prob"] is not None]

    # Brier + log-loss vs actual chosen-side win
    brier_sum = 0.0
    logloss_sum = 0.0
    approved_lost = 0
    rejected_won = 0
    n_approved_resolved = 0
    n_rejected_resolved = 0
    high_conf_errors = []
    missed_pnl = 0.0
    calibration_buckets: dict[str, dict[str, int]] = defaultdict(
        lambda: {"n": 0, "wins": 0, "sum_prob": 0.0})

    def _bin(p: float) -> str:
        """10 bins: 0.0-0.1, 0.1-0.2, ..., 0.9-1.0."""
        b = max(0, min(9, int(p * 10)))
        return f"{b/10:.1f}-{(b+1)/10:.1f}"

    for r in resolved:
        jp = float(r["judge_prob"])
        won = 1.0 if r["final_outcome"] == r["side"] else 0.0
        brier_sum += (jp - won) ** 2
        # Clip prob to avoid log(0)
        p_safe = max(1e-6, min(1 - 1e-6, jp))
        logloss_sum -= won * math.log(p_safe) + (1 - won) * math.log(1 - p_safe)

        bucket = _bin(jp)
        calibration_buckets[bucket]["n"] += 1
        calibration_buckets[bucket]["sum_prob"] += jp
        if won:
            calibration_buckets[bucket]["wins"] += 1

        if r["verdict"] in ("APPROVE", "ADJUST"):
            n_approved_resolved += 1
            if not won:
                approved_lost += 1
                if _is_high_confidence(r["confidence"]):
                    high_conf_errors.append({
                        "entry_id": r["entry_id"],
                        "market": r["market_question"],
                        "city": r["city_resolved"],
                        "judge_prob": jp,
                        "side": r["side"],
                        "outcome": r["final_outcome"],
                        "rationale_excerpt": (r["rationale"] or "")[:400],
                    })
        elif r["verdict"] == "REJECT":
            n_rejected_resolved += 1
            if won:
                rejected_won += 1
                payout = float(r["payout_per_share"] or 0)
                entry_p = float(r["entry_price"] or 0)
                size = float(r["size_usd"] or 100)
                shares = size / entry_p if entry_p else 0
                missed_pnl += (payout - entry_p) * shares

    n_resolved = len(resolved)
    brier = brier_sum / n_resolved if n_resolved else None
    logloss = logloss_sum / n_resolved if n_resolved else None

    calibration_summary = {
        b: {
            "n": v["n"],
            "win_rate": round(v["wins"] / v["n"], 3) if v["n"] else None,
            "mean_judge_prob": round(v["sum_prob"] / v["n"], 3) if v["n"] else None,
            "calibration_gap": (round(v["sum_prob"] / v["n"]
                                       - v["wins"] / v["n"], 3)
                                if v["n"] else None),
        }
        for b, v in sorted(calibration_buckets.items())
    }

    return {
        "n_reviews": n_total,
        "n_resolved": n_resolved,
        "approval_rate": round(n_approve / n_total, 3),
        "false_positive_rate": (round(approved_lost / n_approved_resolved, 3)
                                 if n_approved_resolved else None),
        "false_negative_rate": (round(rejected_won / n_rejected_resolved, 3)
                                 if n_rejected_resolved else None),
        "brier_score": round(brier, 4) if brier is not None else None,
        "log_loss": round(logloss, 4) if logloss is not None else None,
        "calibration_buckets": calibration_summary,
        "n_approved_resolved": n_approved_resolved,
        "n_rejected_resolved": n_rejected_resolved,
        "approved_losers": approved_lost,
        "rejected_winners": rejected_won,
        "missed_pnl_from_rejects_usd": round(missed_pnl, 2),
        "high_confidence_errors": high_confidence_errors_subset(high_conf_errors),
        "interpretation": _interpret_judge_metrics(
            brier, n_approve / n_total if n_total else 0,
            approved_lost / n_approved_resolved if n_approved_resolved else None,
            rejected_won / n_rejected_resolved if n_rejected_resolved else None,
        ),
    }


def high_confidence_errors_subset(errors: list[dict], limit: int = 5) -> list[dict]:
    """Return up to `limit` high-confidence APPROVE→loss cases for the advisor.
    These are the strongest hallucination candidates."""
    return errors[:limit]


def _interpret_judge_metrics(brier: float | None, approval_rate: float,
                              fpr: float | None, fnr: float | None) -> list[str]:
    """Plain-English flags. Advisor uses these as starting hypotheses."""
    flags = []
    if brier is not None and brier > 0.25:
        flags.append(f"Brier={brier:.3f} > 0.25 — judge probabilities are "
                     "poorly calibrated; consider tightening confidence thresholds")
    if approval_rate > 0.7:
        flags.append(f"approval_rate={approval_rate:.0%} > 70% — judge may be "
                     "rubber-stamping; check false_positive_rate")
    if approval_rate < 0.2:
        flags.append(f"approval_rate={approval_rate:.0%} < 20% — judge may be "
                     "over-conservative; check rejected_winners + missed_pnl")
    if fpr is not None and fpr > 0.5:
        flags.append(f"false_positive_rate={fpr:.0%} > 50% — judge approves "
                     "losers more often than winners; HALLUCINATION SIGNAL")
    if fnr is not None and fnr > 0.5:
        flags.append(f"false_negative_rate={fnr:.0%} > 50% — judge rejects "
                     "winners more often than losers; OVER-CONSERVATIVE SIGNAL")
    return flags


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
        # v13.2: auto-routed verdicts (auto_reject/auto_approve) are persisted
        # without an LLM call, so cost_usd — and, defensively, the other numeric
        # fields — can be NULL. Format each None-safe so replay never 500s.
        def _f(v, spec, na="n/a"):
            return format(v, spec) if v is not None else na
        out.append(f"\n## Judge verdict: {j['verdict']} "
                   f"(confidence {_f(j['confidence'], '.2f')})")
        out.append(f"- judge_prob: {_f(j['judge_prob'], '.3f')} "
                   f"(vs bot {_f(j['bot_prob'], '.3f')}, "
                   f"delta {_f(j['prob_delta'], '.3f')})")
        out.append(f"- Rationale: {j['rationale']}")
        cost = j['cost_usd']
        out.append(f"- Cost: ${cost:.4f}" if cost is not None
                   else "- Cost: $0.0000 (auto-routed, no LLM)")

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
                     since_iso: str, triggers: dict | None = None) -> str:
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

    if triggers:
        out.append("\n## Cashouts by Trigger")
        out.append("| Trigger | N | Total $ | Mean $ | Min $ | Max $ |")
        out.append("|---|---|---|---|---|---|")
        for trig, v in sorted(triggers.items(), key=lambda kv: -kv[1]["n"]):
            out.append(f"| {trig} | {v['n']} | {v['total_pnl_usd']} | "
                       f"{v['mean_pnl_usd']} | {v['min_pnl_usd']} | "
                       f"{v['max_pnl_usd']} |")

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
        triggers = aggregate_cashout_triggers(conn, since_iso)
        suggestions = generate_suggestions(buckets, judge)

    if args.output == "json":
        report = {"since": since, "buckets": buckets, "judge": judge,
                  "cashout_triggers": triggers, "suggestions": suggestions}
        text = json.dumps(report, indent=2, default=str)
    elif args.output == "csv":
        out_path = Path(args.out or "weather_edge_report.csv")
        format_csv(buckets, out_path)
        print(f"CSV written to {out_path}", file=sys.stderr)
        return
    else:
        text = format_report_md(buckets, judge, suggestions, since_iso, triggers)

    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"Wrote {args.out}", file=sys.stderr)
    else:
        print(text)


# ---------------------------------------------------------------------------
# Inline tests for compute_ladder_breakdown (v13.8 — resoluções contam)
# Run: python weather_edge_analyzer.py --test-ladder-breakdown
# ---------------------------------------------------------------------------

def _test_ladder_breakdown():
    """Hermetic: settled = cashout OR resolution, com precedência de cashout.
    Antes da v13.8, ladder_perf/single_perf/ttr_cohort só contavam cashouts —
    grupos e singles liquidados por resolução ficavam invisíveis nos KPIs."""
    import sqlite3 as _sq
    import tempfile
    conn = _sq.connect(":memory:")
    conn.row_factory = _sq.Row
    conn.executescript("""
        CREATE TABLE entries (entry_id INTEGER PRIMARY KEY, ts TEXT,
            ladder_group_id TEXT, ladder_position TEXT, ladder_stake_usd REAL,
            status TEXT, skip_reason TEXT, size_usd REAL, size_shares REAL,
            entry_price REAL, ttr_hours_at_entry REAL);
        CREATE TABLE cashouts (cashout_id INTEGER PRIMARY KEY,
            entry_id INTEGER, realized_pnl_usd REAL);
        CREATE TABLE resolutions (resolution_id INTEGER PRIMARY KEY,
            entry_id INTEGER, final_outcome TEXT, payout_per_share REAL);
    """)
    T = "2026-07-01T00:00:00"

    def leg(eid, gid, price, shares, ttr=20.0, status='EXECUTED', pos='central'):
        conn.execute(
            "INSERT INTO entries VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
            (eid, T, gid, pos if gid else None, price * shares if gid else None,
             status, price * shares, shares, price, ttr))

    # Grupo gR: 2 legs, TODOS por resolução (win +50, loss -30 → group +20)
    leg(1, 'gR', 0.50, 100); leg(2, 'gR', 0.60, 50, status='FAST_PATH')
    conn.execute("INSERT INTO resolutions VALUES (1, 1, 'NO', 1.0)")
    conn.execute("INSERT INTO resolutions VALUES (2, 2, 'YES', 0.0)")
    # Grupo gC: 1 leg com cashout (+7) E resolução (precedência) + 1 aberto
    leg(3, 'gC', 0.40, 50); leg(4, 'gC', 0.55, 50)
    conn.execute("INSERT INTO cashouts VALUES (1, 3, 7.0)")
    conn.execute("INSERT INTO resolutions VALUES (3, 3, 'NO', 1.0)")
    # Singles: 1 resolvido win (+20), 1 resolvido loss (-24), 1 aberto
    leg(10, None, 0.60, 50, ttr=8.0)   # (1-0.6)*50 = +20, cohort 6-12h
    leg(11, None, 0.40, 60, ttr=30.0)  # (0-0.4)*60 = -24
    leg(12, None, 0.50, 10, ttr=30.0)  # aberto
    conn.execute("INSERT INTO resolutions VALUES (10, 10, 'NO', 1.0)")
    conn.execute("INSERT INTO resolutions VALUES (11, 11, 'YES', 0.0)")
    conn.commit()

    b = compute_ladder_breakdown(conn, "2026-06-01T00:00:00")

    lp = b["ladder_groups_performance"]
    assert lp["n"] == 2, lp                    # gR (só resoluções) + gC
    assert lp["won"] == 2, lp                  # gR +20, gC +7
    assert abs(lp["total_pnl_usd"] - 27.0) < 1e-6, lp
    print(f"Test 1 PASS: ladder_perf conta grupo 100%-resolução — {lp}")

    sp = b["single_bin_performance"]
    assert sp["n"] == 2 and sp["won"] == 1, sp     # aberto (12) fora
    assert abs(sp["total_pnl_usd"] - (-4.0)) < 1e-6, sp  # +20 - 24
    print(f"Test 2 PASS: single_perf conta resoluções, exclui abertos — {sp}")

    t612 = b["ttr_cohort_performance"]["6-12h"]
    assert t612["n"] == 1 and t612["wins"] == 1, t612
    assert abs(t612["total_pnl_usd"] - 20.0) < 1e-6, t612
    print(f"Test 3 PASS: ttr_cohort 6-12h vê o single resolvido — {t612}")

    # Precedência do cashout: pnl do gC = 7 (cashout), não 30 (resolução)
    print("Test 4 PASS: precedência do cashout sem dupla contagem (gC pnl=7)")

    print("\nAll ladder-breakdown tests PASS (4/4)")


if __name__ == "__main__":
    import sys as _sys
    if "--test-ladder-breakdown" in _sys.argv:
        _test_ladder_breakdown()
    else:
        main()
