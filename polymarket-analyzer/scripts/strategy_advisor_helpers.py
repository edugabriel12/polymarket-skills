"""Helpers for weather_strategy_advisor.py.

All functions read-only. Provides:
  - collect_extras: parser confidence, observed MAE per metric, city performance
  - read_current_config: CLI defaults + MAE constants + city count
  - has_new_data_since_last_run: skip if no new resolutions
  - write_advisor_report: persist markdown + JSON sidecar
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "polymarket-analyzer" / "scripts"
HELPERS_PATH = SCRIPTS_DIR / "weather_edge_helpers.py"
BOT_PATH = SCRIPTS_DIR / "weather_edge_bot.py"
CITIES_PATH = REPO_ROOT / "polymarket-analyzer" / "references" / "weather-cities.json"
JUDGE_PROMPT_PATH = REPO_ROOT / "polymarket-analyzer" / "references" / "weather-judge-prompt.md"

REPORTS_DIR = Path.home() / ".polymarket-paper" / "advisor_reports"


def collect_extras(conn: sqlite3.Connection, since_iso: str) -> dict:
    """Aggregate signals not covered by weather_edge_analyzer:
      - parser_confidence histogram (low/med/high counts)
      - observed MAE per threshold_unit (forecast vs realized observed_value)
      - per-city performance (n trades, win rate, mean P&L, mean parser_conf)
    """
    parser_hist = _parser_confidence_hist(conn, since_iso)
    mae_observed = _observed_mae_per_unit(conn, since_iso)
    city_perf = _city_performance(conn, since_iso)
    return {
        "parser_confidence_hist": parser_hist,
        "observed_mae_per_unit": mae_observed,
        "city_performance": city_perf,
    }


def _parser_confidence_hist(conn, since_iso: str) -> dict:
    rows = conn.execute(
        "SELECT parser_confidence FROM entries WHERE ts >= ?",
        (since_iso,),
    ).fetchall()
    buckets = {"high (>=0.9)": 0, "medium (0.7-0.9)": 0,
               "low (<0.7)": 0, "missing": 0}
    for r in rows:
        c = r[0]
        if c is None:
            buckets["missing"] += 1
        elif c >= 0.9:
            buckets["high (>=0.9)"] += 1
        elif c >= 0.7:
            buckets["medium (0.7-0.9)"] += 1
        else:
            buckets["low (<0.7)"] += 1
    return buckets


def _observed_mae_per_unit(conn, since_iso: str) -> dict:
    """Compute |forecast - observed| per threshold_unit using
    entries.forecast_snapshot_json + resolutions.observed_value.

    We extract forecast value from the snapshot using a heuristic that mirrors
    weather_edge_helpers.forecast_probability — for temp markets, look for
    'temp_max' or 'temp' in the daily forecast for entry's target_date.
    Conservative: skip rows without parseable forecast.
    """
    rows = conn.execute(
        "SELECT e.threshold_unit, e.forecast_snapshot_json, e.end_date, "
        "       r.observed_value "
        "FROM entries e JOIN resolutions r ON r.entry_id = e.entry_id "
        "WHERE e.ts >= ? AND r.observed_value IS NOT NULL "
        "  AND e.threshold_unit IS NOT NULL",
        (since_iso,),
    ).fetchall()

    by_unit: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        unit = r[0]
        snapshot = r[1]
        observed = r[3]
        try:
            snap = json.loads(snapshot) if isinstance(snapshot, str) else snapshot
        except Exception:
            continue
        forecast_val = _extract_forecast_value(snap, unit)
        if forecast_val is None:
            continue
        try:
            obs = float(observed)
        except (TypeError, ValueError):
            continue
        by_unit[unit].append(abs(forecast_val - obs))

    out = {}
    for unit, errs in by_unit.items():
        if not errs:
            continue
        out[unit] = {
            "n": len(errs),
            "mae": round(sum(errs) / len(errs), 3),
            "max_err": round(max(errs), 3),
        }
    return out


def _extract_forecast_value(snap: dict, unit: str) -> Optional[float]:
    """Best-effort extraction of forecast scalar from OpenWeather snapshot.
    Returns None if not parseable."""
    if not isinstance(snap, dict):
        return None
    # OpenWeather one-call: snap["daily"][i] = {"temp": {"max": ...}, ...}
    daily = snap.get("daily") or []
    if not daily:
        return None
    day = daily[0] if isinstance(daily, list) and daily else {}
    if unit in ("F", "C"):
        temp = day.get("temp")
        if isinstance(temp, dict):
            return temp.get("max") or temp.get("day")
        return None
    if unit in ("mm", "in"):
        return day.get("rain") or day.get("snow") or 0.0
    if unit.lower() in ("kph", "mph"):
        return day.get("wind_speed")
    return None


def _city_performance(conn, since_iso: str) -> dict:
    rows = conn.execute(
        "SELECT e.city_resolved, e.entry_id, e.parser_confidence, "
        "       r.final_outcome, c.realized_pnl_usd "
        "FROM entries e "
        "LEFT JOIN resolutions r ON r.entry_id = e.entry_id "
        "LEFT JOIN cashouts c ON c.entry_id = e.entry_id "
        "WHERE e.ts >= ? AND e.city_resolved IS NOT NULL "
        "  AND e.status IN ('EXECUTED', 'FAST_PATH')",
        (since_iso,),
    ).fetchall()

    agg: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "wins": 0, "losses": 0,
                 "pnl_usd": 0.0, "parser_conf_sum": 0.0,
                 "parser_conf_n": 0})
    for r in rows:
        city = r[0]
        a = agg[city]
        a["n"] += 1
        if r[2] is not None:
            a["parser_conf_sum"] += float(r[2])
            a["parser_conf_n"] += 1
        if r[3] == "YES" or r[3] == "NO":
            # Won if outcome matches the side they bought
            # Without side info here we approximate: any cashout pnl > 0 is win
            pass
        if r[4] is not None:
            pnl = float(r[4])
            a["pnl_usd"] += pnl
            if pnl > 0:
                a["wins"] += 1
            elif pnl < 0:
                a["losses"] += 1

    out = {}
    for city, v in agg.items():
        decided = v["wins"] + v["losses"]
        out[city] = {
            "n": v["n"],
            "wins": v["wins"],
            "losses": v["losses"],
            "win_rate": round(v["wins"] / decided, 3) if decided else None,
            "total_pnl_usd": round(v["pnl_usd"], 2),
            "mean_parser_confidence": (
                round(v["parser_conf_sum"] / v["parser_conf_n"], 3)
                if v["parser_conf_n"] else None
            ),
        }
    return out


# ---------------------------------------------------------------------------
# Per-trade classification + aggregation (Advisor v2)
# ---------------------------------------------------------------------------


def classify_trade(row: dict) -> dict:
    """Given a per-trade row from query_per_trade_details, derive:
      - exit_strategy: how the position ended (profit_lock, trailing_stop,
        convergence, forecast_reversal, hold_to_resolution, still_open)
      - outcome_class: winner/loser realization (winner_realized,
        winner_resolved, loser_realized, loser_resolved, void, open,
        breakeven)
    """
    cashout_id = row.get("cashout_id")
    final_outcome = row.get("final_outcome")
    side = row.get("side")

    # Exit strategy
    if cashout_id is not None:
        reason = row.get("exit_decision_reason") or ""
        trigger = reason.split(":", 1)[0].strip() if ":" in reason else "unknown"
        if trigger not in ("profit_lock", "trailing_stop", "convergence",
                            "forecast_reversal"):
            trigger = "cashout_other" if trigger else "cashout_unknown"
        exit_strategy = trigger
    elif final_outcome is not None:
        exit_strategy = "hold_to_resolution"
    else:
        exit_strategy = "still_open"

    # Outcome
    if cashout_id is not None:
        pnl = row.get("realized_pnl_usd") or 0
        if pnl > 0.001:
            outcome_class = "winner_realized"
        elif pnl < -0.001:
            outcome_class = "loser_realized"
        else:
            outcome_class = "breakeven"
    elif final_outcome == "VOID":
        outcome_class = "void"
    elif final_outcome is not None:
        won = (
            (side == "YES" and final_outcome == "YES")
            or (side == "NO" and final_outcome == "NO")
        )
        outcome_class = "winner_resolved" if won else "loser_resolved"
    else:
        outcome_class = "open"

    return {"exit_strategy": exit_strategy, "outcome_class": outcome_class}


def _resolved_pnl(row: dict) -> Optional[float]:
    """Effective realized P&L: cashout amount, or holding payout-based P&L
    for resolved positions, or None if still open."""
    if row.get("cashout_id") is not None:
        return float(row.get("realized_pnl_usd") or 0)
    if row.get("final_outcome") in ("YES", "NO", "VOID"):
        payout = row.get("payout_per_share")
        if payout is None:
            return None
        shares = float(row.get("size_shares") or 0)
        cost = float(row.get("size_usd") or 0)
        return round(float(payout) * shares - cost, 4)
    return None


def compute_strategy_breakdown(per_trade: list[dict]) -> list[dict]:
    """Aggregate per-trade rows by exit_strategy. Skips strategies with
    fewer than 1 sample. Returns list of dicts:
      [{strategy, n_trades, n_resolved, n_wins, win_rate,
        total_pnl_usd, mean_pnl_usd}, ...]
    Sorted by n_trades desc.
    """
    bucket: dict[str, dict] = defaultdict(lambda: {
        "n_trades": 0, "n_resolved": 0, "n_wins": 0,
        "total_pnl_usd": 0.0,
    })
    for t in per_trade:
        cls = t.get("_classification") or classify_trade(t)
        strat = cls["exit_strategy"]
        outcome = cls["outcome_class"]
        b = bucket[strat]
        b["n_trades"] += 1
        if outcome in ("winner_realized", "winner_resolved",
                       "loser_realized", "loser_resolved", "breakeven"):
            b["n_resolved"] += 1
            if outcome.startswith("winner"):
                b["n_wins"] += 1
            pnl = _resolved_pnl(t)
            if pnl is not None:
                b["total_pnl_usd"] += pnl

    out = []
    for strat, b in bucket.items():
        n_res = b["n_resolved"]
        out.append({
            "strategy": strat,
            "n_trades": b["n_trades"],
            "n_resolved": n_res,
            "n_wins": b["n_wins"],
            "win_rate": round(b["n_wins"] / n_res, 3) if n_res > 0 else None,
            "total_pnl_usd": round(b["total_pnl_usd"], 2),
            "mean_pnl_usd": (round(b["total_pnl_usd"] / n_res, 2)
                             if n_res > 0 else None),
        })
    out.sort(key=lambda r: -r["n_trades"])
    return out


def compute_winner_loser_patterns(per_trade: list[dict]) -> dict:
    """Extract distributional features separately for winners and losers.
    Returns {winners: {...}, losers: {...}} where each side has:
      n, by_city (top 5), by_side, by_edge_bucket, by_ttr_bucket,
      by_judge_verdict, by_exit_strategy, mean_parser_confidence.
    """
    def _summarize(group: list[dict]) -> dict:
        if not group:
            return {"n": 0}
        by_city: dict[str, int] = defaultdict(int)
        by_side: dict[str, int] = defaultdict(int)
        by_edge: dict[str, int] = defaultdict(int)
        by_ttr: dict[str, int] = defaultdict(int)
        by_judge: dict[str, int] = defaultdict(int)
        by_strat: dict[str, int] = defaultdict(int)
        parser_confs: list[float] = []
        for t in group:
            city = (t.get("city_resolved") or "?")
            by_city[city] += 1
            by_side[t.get("side") or "?"] += 1
            edge = float(t.get("edge_pp_at_entry") or 0)
            if edge < 10:
                by_edge["<10pp"] += 1
            elif edge < 25:
                by_edge["10-25pp"] += 1
            elif edge < 50:
                by_edge["25-50pp"] += 1
            else:
                by_edge["50+pp"] += 1
            ttr = float(t.get("ttr_hours_at_entry") or 0)
            if ttr < 6:
                by_ttr["<6h"] += 1
            elif ttr < 24:
                by_ttr["6-24h"] += 1
            elif ttr < 48:
                by_ttr["24-48h"] += 1
            else:
                by_ttr[">48h"] += 1
            by_judge[t.get("judge_verdict") or "no_judge"] += 1
            cls = t.get("_classification") or classify_trade(t)
            by_strat[cls["exit_strategy"]] += 1
            pc = t.get("parser_confidence")
            if pc is not None:
                parser_confs.append(float(pc))
        top_cities = sorted(by_city.items(), key=lambda kv: -kv[1])[:5]
        return {
            "n": len(group),
            "by_city_top5": dict(top_cities),
            "by_side": dict(by_side),
            "by_edge_bucket": dict(by_edge),
            "by_ttr_bucket": dict(by_ttr),
            "by_judge_verdict": dict(by_judge),
            "by_exit_strategy": dict(by_strat),
            "mean_parser_confidence": (
                round(sum(parser_confs) / len(parser_confs), 3)
                if parser_confs else None
            ),
        }

    winners = []
    losers = []
    for t in per_trade:
        cls = t.get("_classification") or classify_trade(t)
        oc = cls["outcome_class"]
        if oc in ("winner_realized", "winner_resolved"):
            winners.append(t)
        elif oc in ("loser_realized", "loser_resolved"):
            losers.append(t)
    return {"winners": _summarize(winners), "losers": _summarize(losers)}


def compact_per_trade(per_trade: list[dict]) -> list[dict]:
    """Return compact per-trade rows for the LLM payload (one ~150-token
    dict each). Strips long fields; adds classification."""
    out = []
    for t in per_trade:
        cls = classify_trade(t)
        out.append({
            "id": t.get("entry_id"),
            "ts": (t.get("ts") or "")[:16],
            "city": t.get("city_resolved"),
            "side": t.get("side"),
            "entry_price": _r(t.get("entry_price"), 3),
            "size_usd": _r(t.get("size_usd"), 2),
            "edge_pp": _r(t.get("edge_pp_at_entry"), 1),
            "ttr_h": _r(t.get("ttr_hours_at_entry"), 1),
            "forecast_prob_at_entry": _r(t.get("forecast_prob_at_entry"), 3),
            "parser_confidence": _r(t.get("parser_confidence"), 2),
            "judge_verdict": t.get("judge_verdict"),
            "judge_prob": _r(t.get("judge_prob"), 3),
            "judge_confidence": _r(t.get("judge_confidence"), 2),
            "exit_strategy": cls["exit_strategy"],
            "exit_price": _r(t.get("exit_price"), 3),
            "realized_pnl_usd": _r(t.get("realized_pnl_usd"), 2),
            "final_outcome": t.get("final_outcome"),
            "counterfactual_delta_usd": _r(t.get("counterfactual_delta_usd"), 2),
            "outcome_class": cls["outcome_class"],
        })
    return out


def compute_divergent_judge_samples(conn, since_iso: str,
                                      limit: int = 10) -> list[dict]:
    """v6: For each resolved trade where the judge's verdict and the actual
    outcome diverge, surface the full rationale + input context so the
    advisor can spot hallucination patterns.

    Returns up to `limit` divergent cases, prioritized by:
      1. APPROVE → loss with high confidence (worst hallucination)
      2. REJECT → win (missed opportunity, judge over-conservative)
      3. Other APPROVE → loss
    """
    rows = conn.execute(
        "SELECT j.verdict, j.confidence, j.judge_prob, j.bot_prob, "
        "       j.rationale, j.evidence_json, j.input_context_json, "
        "       e.entry_id, e.market_question, e.city_resolved, "
        "       e.entry_price, e.side, e.size_usd, "
        "       r.final_outcome, r.payout_per_share "
        "FROM judge_reviews j "
        "JOIN entries e ON e.entry_id = j.entry_id "
        "JOIN resolutions r ON r.entry_id = e.entry_id "
        "WHERE j.ts >= ? "
        "  AND r.final_outcome IN ('YES','NO')",
        (since_iso,),
    ).fetchall()

    divergent = []
    for r in rows:
        won = r["final_outcome"] == r["side"]
        verdict = r["verdict"]
        is_approve = verdict in ("APPROVE", "ADJUST")
        is_reject = verdict == "REJECT"
        diverged = (is_approve and not won) or (is_reject and won)
        if not diverged:
            continue

        # Priority: 0 = high-conf APPROVE→loss, 1 = REJECT→win, 2 = other APPROVE→loss
        # judge_reviews.confidence is a float in [0,1]; treat >= 0.7 as high
        # (tolerate legacy string rows defensively).
        conf_raw = r["confidence"]
        if isinstance(conf_raw, (int, float)):
            is_high_conf = float(conf_raw) >= 0.7
        else:
            is_high_conf = str(conf_raw or "").lower() == "high"
        if is_approve and not won and is_high_conf:
            priority = 0
        elif is_reject and won:
            priority = 1
        else:
            priority = 2

        # Compute hypothetical PnL impact (signed: negative for our loss)
        entry_p = float(r["entry_price"] or 0)
        size = float(r["size_usd"] or 100)
        shares = size / entry_p if entry_p else 0
        payout = float(r["payout_per_share"] or 0)
        if is_approve and not won:
            # We took the bet and lost
            pnl_impact = -size
        elif is_reject and won:
            # We didn't take a winning bet
            pnl_impact = (payout - entry_p) * shares
        else:
            pnl_impact = 0

        divergent.append({
            "entry_id": r["entry_id"],
            "priority": priority,
            "pattern": ("approve_lost" if is_approve and not won
                        else "rejected_won"),
            "market": r["market_question"],
            "city": r["city_resolved"],
            "side": r["side"],
            "outcome": r["final_outcome"],
            "verdict": verdict,
            "judge_confidence": r["confidence"],
            "judge_prob": _r(r["judge_prob"], 3),
            "bot_prob": _r(r["bot_prob"], 3),
            "entry_price": _r(r["entry_price"], 3),
            "pnl_impact_usd": round(pnl_impact, 2),
            "rationale": r["rationale"] or "",
            "evidence_summary": _try_json(r["evidence_json"]),
            "input_context": _try_json(r["input_context_json"]),
        })

    # Sort by priority then by abs pnl impact
    divergent.sort(key=lambda d: (d["priority"], -abs(d["pnl_impact_usd"])))
    return divergent[:limit]


def _try_json(s):
    if not s:
        return None
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return None


# ---------------------------------------------------------------------------
# v10: Loss forensics — for each losing trade, reconstruct what the weather
# actually did vs what we forecast. Pulls realized values from multiple
# sources (Open-Meteo archive, Visual Crossing historical, observed_value
# if Polymarket reported it) and the full forecast trajectory from
# forecast_history. Lets the advisor diagnose WHY a bet failed:
#   - forecast was right but we exited too early?
#   - forecast was wrong from the start?
#   - forecast drifted against us during the wait?
# ---------------------------------------------------------------------------


def _is_loser_row(row: dict) -> bool:
    """A losing trade is one where realized P&L is negative — either
    because we cashed out at a loss, or because we held to resolution
    and the side didn't pay out (payout < entry_price)."""
    cashout_id = row.get("cashout_id")
    if cashout_id is not None:
        return float(row.get("realized_pnl_usd") or 0) < -0.01
    final_outcome = row.get("final_outcome")
    side = row.get("side")
    if final_outcome in ("YES", "NO"):
        return final_outcome != side
    return False


def _summarize_forecast_trajectory(snapshots: list[sqlite3.Row],
                                     realized_value: Optional[float],
                                     threshold_value: float,
                                     comparison: str,
                                     side: str) -> dict:
    """Given chronological forecast snapshots (from forecast_history) and
    the realized value, compute when the forecast started disagreeing with
    our bet. Returns:
      {"n_snapshots": int, "first_ts": str, "last_ts": str,
       "first_value": float, "last_value": float, "drift": float,
       "would_lose_at_first": bool, "would_lose_at_last": bool,
       "first_adverse_ts": str|None, "first_adverse_value": float|None,
       "samples": [{ts, source, value, would_win_if_realized}, ...] (top 8)}
    `would_lose_at_*` projects the snapshot value as if it were realized
    and applies the bet's threshold/comparison/side to decide outcome.
    """
    if not snapshots:
        return {"n_snapshots": 0}

    def _resolves_yes(val: float) -> bool:
        if val is None:
            return False
        c = comparison
        t = threshold_value
        if c == "exceed":
            return val > t
        if c == "below":
            return val < t
        if c == "at_least":
            return val >= t
        if c == "at_most":
            return val <= t
        # range markets: side stored at threshold_value (low end)
        return False

    def _would_win(val: Optional[float]) -> Optional[bool]:
        if val is None:
            return None
        yes_resolves = _resolves_yes(float(val))
        return (side == "YES" and yes_resolves) or (side == "NO" and not yes_resolves)

    sorted_snaps = sorted(snapshots, key=lambda r: r["ts"])
    first = sorted_snaps[0]
    last = sorted_snaps[-1]
    first_val = float(first["predicted_value"])
    last_val = float(last["predicted_value"])

    first_adverse_ts = None
    first_adverse_value = None
    for s in sorted_snaps:
        if _would_win(float(s["predicted_value"])) is False:
            first_adverse_ts = s["ts"]
            first_adverse_value = float(s["predicted_value"])
            break

    samples = []
    step = max(1, len(sorted_snaps) // 8)
    for s in sorted_snaps[::step][:8]:
        v = float(s["predicted_value"])
        samples.append({
            "ts": s["ts"][:16],
            "source": s["source"],
            "value": round(v, 2),
            "would_win_if_realized": _would_win(v),
        })

    return {
        "n_snapshots": len(sorted_snaps),
        "first_ts": first["ts"][:16],
        "last_ts": last["ts"][:16],
        "first_value": round(first_val, 2),
        "last_value": round(last_val, 2),
        "drift": round(last_val - first_val, 2),
        "would_win_at_first": _would_win(first_val),
        "would_win_at_last": _would_win(last_val),
        "first_adverse_ts": first_adverse_ts[:16] if first_adverse_ts else None,
        "first_adverse_value": (round(first_adverse_value, 2)
                                  if first_adverse_value is not None else None),
        "realized_value": (round(realized_value, 2)
                            if realized_value is not None else None),
        "would_have_won": _would_win(realized_value),
        "samples": samples,
    }


def _fetch_realized_multi(city: str, lat: Optional[float], lon: Optional[float],
                            target_iso: str,
                            observed_value: Optional[float],
                            threshold_unit: Optional[str] = None) -> dict:
    """Pull realized weather from up to 3 sources in parallel:
      1. Open-Meteo archive (lat/lon required)
      2. Visual Crossing historical (city name required)
      3. Polymarket's reported observed_value (already in DB if present)
    Returns:
      {"sources": {"open_meteo_archive": {...}, "visual_crossing": {...},
                    "polymarket_observed": float|None},
       "consensus_max_c": float|None, "n_sources": int}

    `threshold_unit` is the entry's unit ("F"/"C"); needed to fold
    `observed_value` (reported in that unit by Polymarket) into the
    Celsius consensus.
    """
    # Import locally so this module stays importable without the
    # weather_edge_helpers chain being fully loaded.
    import sys as _sys
    _sys.path.insert(0, str(SCRIPTS_DIR))
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import weather_edge_helpers as weh  # noqa: E402

    out: dict = {"sources": {}, "consensus_max_c": None, "n_sources": 0}
    futures = {}
    with ThreadPoolExecutor(max_workers=2) as ex:
        if lat is not None and lon is not None:
            futures[ex.submit(weh.fetch_open_meteo_archive, lat, lon, target_iso)] \
                = "open_meteo_archive"
        if city:
            futures[ex.submit(weh.fetch_visual_crossing, city, target_iso)] \
                = "visual_crossing"
        for fut in as_completed(futures):
            label = futures[fut]
            try:
                res = fut.result()
            except Exception as e:
                res = {"error": str(e)}
            if res:
                out["sources"][label] = res

    # Polymarket observed_value: usually F for US markets, but unit varies.
    # Store raw; the caller already knows the unit.
    if observed_value is not None:
        out["sources"]["polymarket_observed"] = {
            "value": float(observed_value),
            "source": "polymarket-resolution",
        }

    # Consensus: median of max_c across sources where we can extract it.
    max_cs = []
    om = out["sources"].get("open_meteo_archive")
    if isinstance(om, dict) and om.get("observed_max_c") is not None:
        max_cs.append(float(om["observed_max_c"]))
    vc = out["sources"].get("visual_crossing")
    if isinstance(vc, dict) and vc.get("days"):
        day0 = vc["days"][0]
        tmax_f = day0.get("tempmax")
        if tmax_f is not None:
            # VC returns Fahrenheit in unitGroup=us; convert to C.
            max_cs.append((float(tmax_f) - 32) * 5 / 9)
    # Polymarket's observed_value is authoritative when present — fold it
    # into consensus after converting to Celsius based on the unit the
    # market was denominated in.
    if observed_value is not None and threshold_unit:
        try:
            ov = float(observed_value)
        except (TypeError, ValueError):
            ov = None
        if ov is not None:
            u = threshold_unit.upper()
            if u == "C":
                max_cs.append(ov)
            elif u == "F":
                max_cs.append((ov - 32) * 5 / 9)
    if max_cs:
        max_cs.sort()
        n = len(max_cs)
        out["consensus_max_c"] = round(
            max_cs[n // 2] if n % 2 else (max_cs[n // 2 - 1] + max_cs[n // 2]) / 2,
            2,
        )
        out["n_sources"] = n
    return out


def compute_loss_forensics(conn: sqlite3.Connection, since_iso: str
                            ) -> list[dict]:
    """v10: For each losing trade in the window, reconstruct the realized
    weather and the forecast trajectory between entry and resolution. The
    advisor uses this to classify losses as (a) forecast was always wrong,
    (b) forecast turned mid-flight and we could have exited, or
    (c) we exited too early on a forecast that ended up correct.

    Returns one dict per loss, sorted by abs(realized_pnl) desc.
    """
    import sys as _sys
    _sys.path.insert(0, str(SCRIPTS_DIR))
    import weather_edge_helpers as weh  # noqa: E402
    cities_cfg = weh.load_cities()

    # Pull per-trade detail (same join used by the per-trade payload).
    import weather_edge_db as wdb  # noqa: E402
    rows = [dict(r) for r in wdb.query_per_trade_details(
        conn, since_iso, limit=10_000)]

    losers: list[dict] = []
    for t in rows:
        if not _is_loser_row(t):
            continue
        entry_id = t["entry_id"]
        # Pull entry's full row for end_date, threshold, comparison.
        e = conn.execute(
            "SELECT end_date, ts, city_resolved, threshold_value, "
            "       threshold_unit, comparison, side, entry_price, "
            "       market_question, forecast_snapshot_json, "
            "       discovery_meta_json "
            "FROM entries WHERE entry_id = ?", (entry_id,),
        ).fetchone()
        if e is None:
            continue
        end_date = (e["end_date"] or "")[:10]
        city = e["city_resolved"] or ""
        threshold = e["threshold_value"]
        comparison = e["comparison"]
        side = e["side"]
        if not (end_date and city and threshold is not None and comparison):
            continue

        # Resolve lat/lon via station override → fallback to discovery_meta.
        lat = lon = None
        station = weh.resolve_station(city, cities_cfg)
        if station:
            lat = station.get("lat")
            lon = station.get("lon")
        if lat is None or lon is None:
            dm = _try_json(e["discovery_meta_json"])
            if isinstance(dm, dict):
                lat = dm.get("lat") or (dm.get("station") or {}).get("lat")
                lon = dm.get("lon") or (dm.get("station") or {}).get("lon")

        # Look up Polymarket's observed_value if present.
        obs_row = conn.execute(
            "SELECT observed_value FROM resolutions WHERE entry_id = ?",
            (entry_id,)).fetchone()
        observed_value = obs_row[0] if obs_row else None

        # Pull realized weather from external sources.
        realized = _fetch_realized_multi(
            city, lat, lon, end_date, observed_value,
            threshold_unit=e["threshold_unit"])

        # Pull forecast trajectory from forecast_history for this
        # (city, target_date) — bot writes per discovery cycle, so this
        # captures every refresh between entry and resolution.
        snapshots = conn.execute(
            "SELECT ts, source, predicted_value "
            "FROM forecast_history "
            "WHERE city = ? AND target_date = ? "
            "  AND ts >= ? "
            "ORDER BY ts ASC",
            (city, end_date, t["ts"]),
        ).fetchall()

        # forecast_history.predicted_value is stored in the market's unit
        # (see weather_edge_bot:insert_forecast_history). Convert the
        # Celsius consensus into that same unit so the trajectory's
        # would_win check uses consistent scale.
        consensus_max_c = realized.get("consensus_max_c")
        unit = (e["threshold_unit"] or "").upper()
        if consensus_max_c is None:
            consensus_in_unit = None
        elif unit == "C":
            consensus_in_unit = float(consensus_max_c)
        elif unit == "F":
            consensus_in_unit = float(consensus_max_c) * 9.0 / 5.0 + 32.0
        else:
            consensus_in_unit = None  # non-temp metric — skip projection
        trajectory = _summarize_forecast_trajectory(
            snapshots, consensus_in_unit, float(threshold), comparison, side)

        losers.append({
            "entry_id": entry_id,
            "ts_entry": (t["ts"] or "")[:16],
            "city": city,
            "target_date": end_date,
            "market_question": e["market_question"],
            "side": side,
            "comparison": comparison,
            "threshold": _r(threshold, 2),
            "entry_price": _r(t.get("entry_price"), 3),
            "forecast_prob_at_entry": _r(t.get("forecast_prob_at_entry"), 3),
            "judge_verdict": t.get("judge_verdict"),
            "judge_prob": _r(t.get("judge_prob"), 3),
            "exit_strategy": classify_trade(t)["exit_strategy"],
            "realized_pnl_usd": _r(_resolved_pnl(t), 2),
            "realized_weather": realized,
            "forecast_trajectory": trajectory,
        })

    losers.sort(key=lambda d: -abs(d.get("realized_pnl_usd") or 0))
    return losers


def _r(v, digits: int):
    if v is None:
        return None
    try:
        return round(float(v), digits)
    except (TypeError, ValueError):
        return None


def read_current_config() -> dict:
    """Parse current CLI defaults + MAE constants + city count from source.
    Used by the advisor to know what's currently configured."""
    cfg: dict = {"cli_defaults": {}, "mae_constants": {}, "cities_count": 0,
                 "judge_prompt_excerpt": ""}

    # MAE constants from weather_edge_helpers.py
    if HELPERS_PATH.exists():
        text = HELPERS_PATH.read_text(encoding="utf-8")
        for name in ("MAE_TEMP_F", "MAE_TEMP_C", "MAE_PRECIP_MM", "MAE_WIND_KPH"):
            m = re.search(rf"^{name}\s*=\s*([0-9.]+)", text, re.MULTILINE)
            if m:
                cfg["mae_constants"][name] = float(m.group(1))

    # CLI defaults from weather_edge_bot.py
    if BOT_PATH.exists():
        text = BOT_PATH.read_text(encoding="utf-8")
        for flag in ("--min-edge-pp", "--min-price", "--max-price",
                     "--profit-lock-pp", "--trailing-drawdown-pct",
                     "--convergence-pp", "--fast-path-ttr-min",
                     # v11: surface the TTR floor + risk gate to the advisor
                     "--min-ttr-hours", "--ladder-min-ttr-hours",
                     "--max-drawdown-halt-pct", "--daily-loss-limit-pct"):
            # Match: p.add_argument("--flag", ..., default=VALUE
            pat = (rf'add_argument\(\s*["\']{re.escape(flag)}["\'][^)]*?'
                   r'default\s*=\s*([0-9.\-]+)')
            m = re.search(pat, text)
            if m:
                cfg["cli_defaults"][flag] = float(m.group(1))

    # Cities count
    if CITIES_PATH.exists():
        try:
            cities = json.loads(CITIES_PATH.read_text(encoding="utf-8"))
            if isinstance(cities, dict):
                cfg["cities_count"] = sum(
                    len(v) if isinstance(v, list) else 0
                    for v in cities.values()
                )
            elif isinstance(cities, list):
                cfg["cities_count"] = len(cities)
        except Exception:
            cfg["cities_count"] = -1

    # Judge prompt excerpt (first 800 chars for brevity)
    if JUDGE_PROMPT_PATH.exists():
        cfg["judge_prompt_excerpt"] = JUDGE_PROMPT_PATH.read_text(encoding="utf-8")[:800]

    return cfg


def has_new_data_since_last_run(conn: sqlite3.Connection) -> bool:
    """True if any new resolution/cashout/entry exists since the most recent
    successful advisor_run.ts. Always True if no prior run exists."""
    row = conn.execute(
        "SELECT MAX(ts) FROM advisor_runs WHERE status = 'ok'"
    ).fetchone()
    last_ts = row[0] if row else None
    if not last_ts:
        return True
    new_resolutions = conn.execute(
        "SELECT COUNT(*) FROM resolutions WHERE ts_resolved > ?", (last_ts,)
    ).fetchone()[0]
    new_cashouts = conn.execute(
        "SELECT COUNT(*) FROM cashouts WHERE ts > ?", (last_ts,)
    ).fetchone()[0]
    new_entries = conn.execute(
        "SELECT COUNT(*) FROM entries WHERE ts > ?", (last_ts,)
    ).fetchone()[0]
    return (new_resolutions + new_cashouts + new_entries) > 0


def write_advisor_report(payload: dict, since_iso: str,
                         analyzer_md: str) -> tuple[Path, Path]:
    """Persist markdown report + JSON sidecar. Returns (md_path, json_path)."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # If multiple runs same day, suffix with timestamp
    base = REPORTS_DIR / f"{today}_strategy_report"
    md_path = Path(f"{base}.md")
    json_path = Path(f"{base}.json")
    if md_path.exists():
        ts_suffix = datetime.now(timezone.utc).strftime("%H%M%S")
        md_path = Path(f"{base}_{ts_suffix}.md")
        json_path = Path(f"{base}_{ts_suffix}.json")

    md = _format_markdown(payload, since_iso, analyzer_md)
    md_path.write_text(md, encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, default=str),
                          encoding="utf-8")
    return md_path, json_path


def _format_markdown(payload: dict, since_iso: str, analyzer_md: str) -> str:
    out = [
        "# Weather Edge — Weekly Strategy Advisor Report",
        "",
        f"_Generated {datetime.now(timezone.utc).isoformat()}_  ",
        f"_Window analyzed: since {since_iso}_  ",
        f"_Trades analyzed: {payload.get('n_trades_analyzed', 'n/a')}_",
        "",
        "## Executive Summary",
        "",
        payload.get("summary", "_(no summary)_"),
        "",
        "## Suggestions",
        "",
    ]
    suggestions = payload.get("suggestions", [])
    if not suggestions:
        out.append("_No actionable suggestions this run. See research notes below._")
    for s in suggestions:
        out.append(f"### [{s.get('priority', '?').upper()}] {s.get('title', '(no title)')}")
        out.append("")
        out.append(f"- **Category**: `{s.get('category', '?')}`  "
                   f"**Confidence**: {s.get('confidence', '?')}")
        if "current_value" in s and "proposed_value" in s:
            out.append(f"- **Change**: `{s.get('param_path', '?')}`: "
                       f"`{s['current_value']}` → `{s['proposed_value']}`")
        elif "param_path" in s:
            out.append(f"- **Target**: `{s['param_path']}`")
        out.append(f"- **Rationale**: {s.get('rationale', '')}")
        if s.get("counterfactual"):
            out.append(f"- **Counterfactual**: {s['counterfactual']}")
        if s.get("supporting_data"):
            out.append(f"- **Supporting data**: `{json.dumps(s['supporting_data'])}`")
        if s.get("web_citations"):
            out.append("- **Web citations**:")
            for cit in s["web_citations"]:
                out.append(f"  - <{cit.get('url', '')}> — {cit.get('snippet', '')}")
        out.append("")

    # === Advisor v2 sections ===
    strategy_breakdown = payload.get("strategy_breakdown") or []
    if strategy_breakdown:
        out.append("## Strategy Breakdown")
        out.append("")
        out.append("| Strategy | N | Win rate | Total P&L | Mean P&L | Notes |")
        out.append("|---|---|---|---|---|---|")
        for s in strategy_breakdown:
            wr = s.get("win_rate")
            wr_s = f"{wr*100:.0f}%" if wr is not None else "—"
            mean = s.get("mean_pnl_usd")
            mean_s = f"${mean:+.2f}" if mean is not None else "—"
            total = s.get("total_pnl_usd", 0)
            out.append(
                f"| `{s.get('strategy', '?')}` | {s.get('n_trades', 0)} | "
                f"{wr_s} | ${total:+.2f} | {mean_s} | "
                f"{s.get('notes', '')} |"
            )
        out.append("")

    if payload.get("winner_patterns"):
        out.append("## What winners had in common")
        out.append("")
        out.append(payload["winner_patterns"])
        out.append("")

    if payload.get("loser_patterns"):
        out.append("## What losers had in common")
        out.append("")
        out.append(payload["loser_patterns"])
        out.append("")

    insights = payload.get("insights") or []
    if insights:
        out.append("## Key Insights")
        out.append("")
        for i, ins in enumerate(insights, 1):
            out.append(f"### {i}. {ins.get('title', '(no title)')}")
            out.append("")
            out.append(f"- **Category**: `{ins.get('applies_to_category', '?')}`")
            out.append(f"- **Supporting trades**: "
                       f"{ins.get('n_supporting_trades', 0)} "
                       f"({', '.join(f'#{i}' for i in (ins.get('supporting_trade_ids') or [])[:10])})")
            out.append(f"- **Observation**: {ins.get('observation', '')}")
            out.append("")

    if payload.get("research_notes"):
        out.append("## Research Notes")
        out.append("")
        out.append(payload["research_notes"])
        out.append("")

    out.append("## Source Analyzer Report")
    out.append("")
    out.append("<details><summary>Click to expand the underlying analyzer report</summary>")
    out.append("")
    out.append(analyzer_md)
    out.append("")
    out.append("</details>")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Inline tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    import tempfile

    # Test read_current_config
    cfg = read_current_config()
    assert "cli_defaults" in cfg and "mae_constants" in cfg
    assert "--profit-lock-pp" in cfg["cli_defaults"], cfg["cli_defaults"]
    assert cfg["cli_defaults"]["--profit-lock-pp"] == 50.0, cfg["cli_defaults"]
    assert "MAE_TEMP_F" in cfg["mae_constants"], cfg["mae_constants"]
    assert cfg["cities_count"] > 0
    print(f"Test 1 PASS: read_current_config — cli_defaults has "
          f"{len(cfg['cli_defaults'])} flags, MAE_TEMP_F={cfg['mae_constants'].get('MAE_TEMP_F')}, "
          f"cities={cfg['cities_count']}")

    # Test has_new_data + collect_extras with synthetic DB
    tmp = tempfile.mkdtemp()
    db_path = Path(tmp) / "test.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # Minimal schema: entries, resolutions, cashouts, advisor_runs
    conn.executescript("""
        CREATE TABLE entries (entry_id INTEGER PRIMARY KEY, ts TEXT,
            parser_confidence REAL, city_resolved TEXT, threshold_unit TEXT,
            forecast_snapshot_json TEXT, status TEXT, end_date TEXT);
        CREATE TABLE resolutions (resolution_id INTEGER PRIMARY KEY,
            entry_id INTEGER, ts_resolved TEXT, final_outcome TEXT,
            observed_value REAL);
        CREATE TABLE cashouts (cashout_id INTEGER PRIMARY KEY,
            entry_id INTEGER, ts TEXT, realized_pnl_usd REAL);
        CREATE TABLE advisor_runs (run_id INTEGER PRIMARY KEY, ts TEXT,
            status TEXT);
    """)
    # Insert: 3 entries in São Paulo (1 win $5, 1 loss -$3, 1 still open)
    conn.execute(
        "INSERT INTO entries VALUES (1, '2026-05-01T00:00:00Z', 0.95, "
        "'São Paulo', 'C', '{\"daily\":[{\"temp\":{\"max\":24.0}}]}', "
        "'EXECUTED', '2026-05-02T00:00:00Z')")
    conn.execute(
        "INSERT INTO entries VALUES (2, '2026-05-02T00:00:00Z', 0.80, "
        "'São Paulo', 'C', '{\"daily\":[{\"temp\":{\"max\":18.0}}]}', "
        "'EXECUTED', '2026-05-03T00:00:00Z')")
    conn.execute(
        "INSERT INTO entries VALUES (3, '2026-05-03T00:00:00Z', 0.60, "
        "'São Paulo', 'C', '{\"daily\":[{\"temp\":{\"max\":22.0}}]}', "
        "'EXECUTED', '2026-05-05T00:00:00Z')")
    conn.execute("INSERT INTO resolutions VALUES (1, 1, '2026-05-02T12:00:00Z', "
                 "'YES', 23.0)")
    conn.execute("INSERT INTO resolutions VALUES (2, 2, '2026-05-03T12:00:00Z', "
                 "'NO', 20.0)")
    conn.execute("INSERT INTO cashouts VALUES (1, 1, '2026-05-02T13:00:00Z', 5.0)")
    conn.execute("INSERT INTO cashouts VALUES (2, 2, '2026-05-03T13:00:00Z', -3.0)")
    conn.commit()

    extras = collect_extras(conn, "2026-04-01T00:00:00Z")
    assert extras["parser_confidence_hist"]["high (>=0.9)"] == 1
    assert extras["parser_confidence_hist"]["medium (0.7-0.9)"] == 1
    assert extras["parser_confidence_hist"]["low (<0.7)"] == 1
    assert "C" in extras["observed_mae_per_unit"]
    # MAE: |24-23|=1, |18-20|=2 → mean 1.5
    assert extras["observed_mae_per_unit"]["C"]["mae"] == 1.5, extras["observed_mae_per_unit"]
    assert extras["observed_mae_per_unit"]["C"]["n"] == 2
    sp = extras["city_performance"]["São Paulo"]
    assert sp["n"] == 3 and sp["wins"] == 1 and sp["losses"] == 1
    assert sp["total_pnl_usd"] == 2.0
    print(f"Test 2 PASS: collect_extras — São Paulo {sp}, MAE_C "
          f"{extras['observed_mae_per_unit']['C']}")

    # has_new_data_since_last_run: no prior run → True
    assert has_new_data_since_last_run(conn) is True
    print("Test 3 PASS: has_new_data — True when no prior run")

    # Insert a prior run timestamp AFTER all entries; should return False
    conn.execute("INSERT INTO advisor_runs VALUES (1, '2026-12-31T00:00:00Z', 'ok')")
    conn.commit()
    assert has_new_data_since_last_run(conn) is False
    print("Test 4 PASS: has_new_data — False when last run after all data")

    # Insert a new entry and verify True again
    conn.execute(
        "INSERT INTO entries VALUES (4, '2027-01-01T00:00:00Z', 0.95, 'X', 'C', "
        "'{}', 'EXECUTED', '2027-01-02T00:00:00Z')")
    conn.commit()
    assert has_new_data_since_last_run(conn) is True
    print("Test 5 PASS: has_new_data — True after new entry inserted")

    # write_advisor_report smoke
    payload = {
        "n_trades_analyzed": 47,
        "summary": "Test summary",
        "suggestions": [{
            "id": "sug_001", "category": "threshold", "priority": "high",
            "confidence": "high", "title": "Lower profit-lock-pp",
            "current_value": 50, "proposed_value": 35,
            "param_path": "weather_edge_bot.py:--profit-lock-pp",
            "rationale": "Lock too late.",
            "counterfactual": "Delta -$12 over 47 trades.",
            "supporting_data": {"n_samples": 15},
        }],
    }
    # Write to tmp dir to not pollute home
    orig_dir = REPORTS_DIR
    globals()["REPORTS_DIR"] = Path(tmp) / "reports"
    md_p, json_p = write_advisor_report(payload, "2026-04-01T00:00:00Z",
                                         "# fake analyzer report\n\nbody")
    globals()["REPORTS_DIR"] = orig_dir
    assert md_p.exists() and json_p.exists()
    md_text = md_p.read_text()
    assert "Lower profit-lock-pp" in md_text
    assert "Delta -$12" in md_text
    assert "fake analyzer report" in md_text
    json_payload = json.loads(json_p.read_text())
    assert json_payload["suggestions"][0]["title"] == "Lower profit-lock-pp"
    print(f"Test 6 PASS: write_advisor_report — wrote {md_p.name}, "
          f"{len(md_text)} chars md, {len(json_p.read_text())} chars json")

    # === Per-trade classification + breakdown tests (Advisor v2) ===
    sample = [
        # 1. profit_lock winner
        {"entry_id": 1, "side": "NO", "size_usd": 30, "size_shares": 100,
         "city_resolved": "Tokyo", "edge_pp_at_entry": 28,
         "ttr_hours_at_entry": 10, "parser_confidence": 1.0,
         "judge_verdict": "APPROVE",
         "cashout_id": 1, "realized_pnl_usd": 12.5,
         "exit_decision_reason": "profit_lock: bid 0.65 >= entry+50pp",
         "final_outcome": None, "payout_per_share": None},
        # 2. trailing_stop loser
        {"entry_id": 2, "side": "NO", "size_usd": 30, "size_shares": 100,
         "city_resolved": "Manhattan", "edge_pp_at_entry": 22,
         "ttr_hours_at_entry": 5, "parser_confidence": 0.6,
         "judge_verdict": "ADJUST",
         "cashout_id": 2, "realized_pnl_usd": -3.2,
         "exit_decision_reason": "trailing_stop: bid 0.34 <= peak*0.70",
         "final_outcome": None, "payout_per_share": None},
        # 3. convergence winner
        {"entry_id": 3, "side": "YES", "size_usd": 25, "size_shares": 80,
         "city_resolved": "Tokyo", "edge_pp_at_entry": 35,
         "ttr_hours_at_entry": 22, "parser_confidence": 0.95,
         "judge_verdict": "APPROVE",
         "cashout_id": 3, "realized_pnl_usd": 8.0,
         "exit_decision_reason": "convergence: bid within 5pp of fair",
         "final_outcome": None, "payout_per_share": None},
        # 4. hold_to_resolution winner
        {"entry_id": 4, "side": "NO", "size_usd": 40, "size_shares": 200,
         "city_resolved": "Paris", "edge_pp_at_entry": 60,
         "ttr_hours_at_entry": 40, "parser_confidence": 1.0,
         "judge_verdict": "APPROVE",
         "cashout_id": None, "realized_pnl_usd": None,
         "exit_decision_reason": None,
         "final_outcome": "NO", "payout_per_share": 1.0},
        # 5. hold_to_resolution loser
        {"entry_id": 5, "side": "NO", "size_usd": 35, "size_shares": 150,
         "city_resolved": "Manhattan", "edge_pp_at_entry": 18,
         "ttr_hours_at_entry": 32, "parser_confidence": 0.7,
         "judge_verdict": "APPROVE",
         "cashout_id": None, "realized_pnl_usd": None,
         "exit_decision_reason": None,
         "final_outcome": "YES", "payout_per_share": 0.0},
        # 6. forecast_reversal breakeven
        {"entry_id": 6, "side": "YES", "size_usd": 20, "size_shares": 70,
         "city_resolved": "London", "edge_pp_at_entry": 15,
         "ttr_hours_at_entry": 8, "parser_confidence": 0.85,
         "judge_verdict": "ADJUST",
         "cashout_id": 6, "realized_pnl_usd": 0.0,
         "exit_decision_reason": "forecast_reversal: forecast P(YES) below entry",
         "final_outcome": None, "payout_per_share": None},
        # 7. still_open
        {"entry_id": 7, "side": "NO", "size_usd": 30, "size_shares": 90,
         "city_resolved": "Tokyo", "edge_pp_at_entry": 30,
         "ttr_hours_at_entry": 18, "parser_confidence": 1.0,
         "judge_verdict": "APPROVE",
         "cashout_id": None, "realized_pnl_usd": None,
         "exit_decision_reason": None,
         "final_outcome": None, "payout_per_share": None},
        # 8. VOID
        {"entry_id": 8, "side": "YES", "size_usd": 15, "size_shares": 50,
         "city_resolved": "Berlin", "edge_pp_at_entry": 12,
         "ttr_hours_at_entry": 50, "parser_confidence": 0.9,
         "judge_verdict": "APPROVE",
         "cashout_id": None, "realized_pnl_usd": None,
         "exit_decision_reason": None,
         "final_outcome": "VOID", "payout_per_share": 0.5},
    ]

    # classify_trade
    cls_map = {t["entry_id"]: classify_trade(t) for t in sample}
    assert cls_map[1] == {"exit_strategy": "profit_lock",
                          "outcome_class": "winner_realized"}, cls_map[1]
    assert cls_map[2] == {"exit_strategy": "trailing_stop",
                          "outcome_class": "loser_realized"}, cls_map[2]
    assert cls_map[3] == {"exit_strategy": "convergence",
                          "outcome_class": "winner_realized"}, cls_map[3]
    assert cls_map[4] == {"exit_strategy": "hold_to_resolution",
                          "outcome_class": "winner_resolved"}, cls_map[4]
    assert cls_map[5] == {"exit_strategy": "hold_to_resolution",
                          "outcome_class": "loser_resolved"}, cls_map[5]
    assert cls_map[6]["exit_strategy"] == "forecast_reversal"
    assert cls_map[6]["outcome_class"] == "breakeven"
    assert cls_map[7] == {"exit_strategy": "still_open",
                          "outcome_class": "open"}, cls_map[7]
    assert cls_map[8]["outcome_class"] == "void", cls_map[8]
    print("Per-trade Test 1 PASS: classify_trade — 8 scenarios")

    # compute_strategy_breakdown
    sb = compute_strategy_breakdown(sample)
    by_strat = {r["strategy"]: r for r in sb}
    assert by_strat["profit_lock"]["n_trades"] == 1
    assert by_strat["profit_lock"]["n_wins"] == 1
    assert by_strat["profit_lock"]["win_rate"] == 1.0
    assert by_strat["trailing_stop"]["n_wins"] == 0
    # entries 4 (winner_resolved) + 5 (loser_resolved) + 8 (void) → 3 hold_to_resolution
    assert by_strat["hold_to_resolution"]["n_trades"] == 3
    assert by_strat["hold_to_resolution"]["n_wins"] == 1
    # n_resolved counts only outcomes that are W/L/breakeven (excludes void)
    assert by_strat["hold_to_resolution"]["n_resolved"] == 2
    assert by_strat["hold_to_resolution"]["win_rate"] == 0.5
    assert by_strat["still_open"]["n_resolved"] == 0
    print(f"Per-trade Test 2 PASS: compute_strategy_breakdown — "
          f"{len(sb)} strategies, hold win_rate "
          f"{by_strat['hold_to_resolution']['win_rate']}")

    # compute_winner_loser_patterns
    wl = compute_winner_loser_patterns(sample)
    assert wl["winners"]["n"] == 3  # entries 1, 3, 4
    assert wl["losers"]["n"] == 2   # entries 2, 5
    # Manhattan should be heavily represented in losers (2 of 2)
    assert wl["losers"]["by_city_top5"].get("Manhattan") == 2, wl["losers"]
    # Winners had high parser_confidence avg
    assert wl["winners"]["mean_parser_confidence"] is not None
    assert wl["winners"]["mean_parser_confidence"] > 0.9
    print(f"Per-trade Test 3 PASS: winner_loser_patterns — "
          f"winners n={wl['winners']['n']}, losers n={wl['losers']['n']}, "
          f"Manhattan in losers: {wl['losers']['by_city_top5']['Manhattan']}/2")

    # _resolved_pnl
    assert _resolved_pnl(sample[0]) == 12.5  # cashout
    assert _resolved_pnl(sample[3]) == 1.0 * 200 - 40  # hold winner: 200-40=160
    assert _resolved_pnl(sample[3]) == 160.0
    assert _resolved_pnl(sample[4]) == 0.0 * 150 - 35  # hold loser: -35
    assert _resolved_pnl(sample[4]) == -35.0
    assert _resolved_pnl(sample[6]) is None  # still open
    print("Per-trade Test 4 PASS: _resolved_pnl")

    # compact_per_trade
    compact = compact_per_trade(sample)
    assert len(compact) == 8
    assert compact[0]["exit_strategy"] == "profit_lock"
    assert compact[0]["outcome_class"] == "winner_realized"
    print(f"Per-trade Test 5 PASS: compact_per_trade — {len(compact)} rows, "
          f"first.exit_strategy={compact[0]['exit_strategy']}")

    print("\nAll strategy_advisor_helpers tests PASS")
