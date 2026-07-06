"""Weather Edge Backtester — replay past trades with alternative cashout
policy parameters and report what P&L different (profit_lock_pp,
trailing_drawdown_pct, convergence_pp) combinations would have produced.

Used by:
  - the strategy advisor (weather_strategy_advisor.py) to anchor
    threshold suggestions with concrete simulated P&L numbers
  - ad-hoc CLI (`python weather_edge_backtest.py --since-days 30`)
    for the operator to explore parameter sweeps directly

Pure replay — no API calls, no DB writes. Reads monitor_checks timeline
per entry and feeds each checkpoint through evaluate_cashout_triggers.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "polymarket-analyzer" / "scripts"))

import weather_edge_db as db  # noqa: E402
from weather_edge_helpers import evaluate_cashout_triggers  # noqa: E402


@dataclass(frozen=True)
class BacktestParams:
    profit_lock_pp: float = 50.0
    trailing_drawdown_pct: float = 30.0
    trailing_min_gain_pp: float = 20.0
    convergence_pp: float = 5.0
    # Friction model (v5). Defaults 0/0 preserve old behavior.
    bid_slippage_pct: float = 0.0    # haircut on bid at cashout exits
    fee_rate: float = 0.0            # deducted from gross proceeds
    # v11 cheap_convexity: exit margin for the fair_target trigger. Only used
    # for entries tagged strategy='cheap_convexity' (fair read from their
    # discovery_meta_json.fair_yes_raw).
    fair_target_margin_pp: float = 1.0


def replay_entry(entry: dict, monitor_checks: list[dict],
                 resolution: Optional[dict], params: BacktestParams) -> dict:
    """Replay one entry's lifecycle with the given params.

    Returns dict with keys:
      - sim_exit_ts: ISO timestamp of simulated exit (or None)
      - sim_exit_bid: bid at simulated exit (or None)
      - sim_trigger: which cashout trigger fired, or 'hold_to_resolution',
                     or 'still_open'
      - sim_pnl_usd: realized + holding P&L in USD (None if still open)
    """
    entry_price = float(entry.get("entry_price") or 0)
    side = entry.get("side")
    shares = float(entry.get("size_shares") or 0)
    size_usd = float(entry.get("size_usd") or 0)
    peak = 0.0

    # v11 cheap_convexity: replay the fair_target exit using the raw fair
    # captured at entry time (discovery_meta_json.fair_yes_raw). The live
    # monitor recomputes fair each cycle, but for replay the entry-time raw
    # fair is the available anchor.
    is_cc = (entry.get("strategy") == "cheap_convexity")
    cc_fair_yes_raw = None
    if is_cc:
        dm = entry.get("discovery_meta_json")
        if dm:
            try:
                meta = json.loads(dm) if isinstance(dm, str) else dm
                cc_fair_yes_raw = meta.get("fair_yes_raw")
            except (TypeError, ValueError):
                cc_fair_yes_raw = None

    for check in monitor_checks:  # already sorted by ts ASC
        bid = float(check.get("market_best_bid") or 0)
        if bid <= 0:
            continue
        if bid > peak:
            peak = bid
        fpn = check.get("forecast_prob_now")
        if fpn is None:
            continue
        forecast_prob_yes = float(fpn) if side == "YES" else 1.0 - float(fpn)
        verdict = evaluate_cashout_triggers(
            side=side,
            entry_price=entry_price,
            current_bid=bid,
            peak_bid_seen=peak,
            forecast_prob_yes=forecast_prob_yes,
            profit_lock_pp=params.profit_lock_pp,
            trailing_drawdown_pct=params.trailing_drawdown_pct,
            trailing_min_gain_pp=params.trailing_min_gain_pp,
            convergence_pp=params.convergence_pp,
            enable_fair_target=is_cc and cc_fair_yes_raw is not None,
            fair_uncapped_yes=cc_fair_yes_raw,
            fair_target_margin_pp=params.fair_target_margin_pp,
        )
        if verdict["decision"] == "CASHOUT":
            # Apply friction: bid haircut (slippage) + fee deduction on
            # gross proceeds. Defaults 0/0 reduce to (bid - entry) * shares.
            effective_bid = bid * (1.0 - params.bid_slippage_pct / 100.0)
            gross = effective_bid * shares
            fee = gross * params.fee_rate
            net_proceeds = gross - fee
            sim_pnl = net_proceeds - entry_price * shares
            return {
                "sim_exit_ts": check.get("ts"),
                "sim_exit_bid": round(effective_bid, 4),
                "sim_trigger": verdict["trigger"],
                "sim_pnl_usd": round(sim_pnl, 2),
            }

    # No cashout fired — fall through to resolution outcome
    if resolution and resolution.get("payout_per_share") is not None:
        payout = float(resolution["payout_per_share"])
        gross = payout * shares
        fee = gross * params.fee_rate
        net_proceeds = gross - fee
        sim_pnl = net_proceeds - size_usd
        return {
            "sim_exit_ts": None,
            "sim_exit_bid": None,
            "sim_trigger": "hold_to_resolution",
            "sim_pnl_usd": round(sim_pnl, 2),
        }

    return {
        "sim_exit_ts": None,
        "sim_exit_bid": None,
        "sim_trigger": "still_open",
        "sim_pnl_usd": None,
    }


def load_replay_data(conn, since_iso: str, limit: int = 200,
                     strategy: Optional[str] = None,
                     max_entry_price: Optional[float] = None) -> list[dict]:
    """Aggregate per-entry: entry row + ordered monitor_checks + resolution.

    Filters to EXECUTED/FAST_PATH entries since `since_iso`. Returns up to
    `limit` rows (most recent first). Each output dict:
      {"entry": {...}, "checks": [...], "resolution": {...|None}}

    `strategy` (v11): if given, restrict to entries of that strategy, treating
    a NULL strategy column as the legacy 'weather_edge'. Used by the
    cheap_convexity price-band report, and to measure the sub-30c band over
    the legacy history as a proxy before any cheap_convexity entry exists.
    `max_entry_price`: if given, restrict to entries with entry_price below it.
    """
    where = ["ts >= ?", "status IN ('EXECUTED', 'FAST_PATH')"]
    params: list = [since_iso]
    if strategy is not None:
        where.append("COALESCE(strategy, 'weather_edge') = ?")
        params.append(strategy)
    if max_entry_price is not None:
        where.append("entry_price < ?")
        params.append(max_entry_price)
    params.append(limit)
    entries = conn.execute(
        f"SELECT * FROM entries WHERE {' AND '.join(where)} "
        "ORDER BY ts DESC LIMIT ?",
        params,
    ).fetchall()

    out = []
    for e in entries:
        eid = e["entry_id"]
        checks = conn.execute(
            "SELECT ts, market_best_bid, forecast_prob_now, decision "
            "FROM monitor_checks WHERE entry_id = ? ORDER BY ts ASC",
            (eid,),
        ).fetchall()
        res = conn.execute(
            "SELECT * FROM resolutions WHERE entry_id = ? LIMIT 1",
            (eid,),
        ).fetchone()
        out.append({
            "entry": dict(e),
            "checks": [dict(c) for c in checks],
            "resolution": dict(res) if res else None,
        })
    return out


def grid_search(replay_data: list[dict],
                params_grid: list[BacktestParams]) -> list[dict]:
    """For each params combo, replay all entries and aggregate. Returns
    list of dicts ordered by total_pnl_usd desc:
      {params, total_pnl_usd, win_rate, n_resolved, n_open,
       per_trigger_counts}
    """
    results = []
    for params in params_grid:
        total_pnl = 0.0
        n_wins = 0
        n_losses = 0
        n_open = 0
        per_trigger: dict[str, int] = defaultdict(int)
        for data in replay_data:
            r = replay_entry(data["entry"], data["checks"],
                              data["resolution"], params)
            per_trigger[r["sim_trigger"]] += 1
            if r["sim_pnl_usd"] is None:
                n_open += 1
                continue
            total_pnl += r["sim_pnl_usd"]
            if r["sim_pnl_usd"] > 0.001:
                n_wins += 1
            elif r["sim_pnl_usd"] < -0.001:
                n_losses += 1
        n_resolved = n_wins + n_losses
        results.append({
            "params": asdict(params),
            "total_pnl_usd": round(total_pnl, 2),
            "win_rate": round(n_wins / n_resolved, 3) if n_resolved else None,
            "n_resolved": n_resolved,
            "n_open": n_open,
            "n_wins": n_wins,
            "n_losses": n_losses,
            "per_trigger_counts": dict(per_trigger),
        })
    results.sort(key=lambda r: -r["total_pnl_usd"])
    return results


def _hold_pnl(entry: dict, resolution: Optional[dict],
              params: BacktestParams) -> Optional[float]:
    """P&L in USD if the entry were held to resolution (no cashout), with the
    same fee model as replay_entry. None if unresolved. Computed directly from
    the resolution payout rather than by coercing evaluate_cashout_triggers,
    because trigger 4 (forecast_reversal) is not param-gated and would exit."""
    if not resolution or resolution.get("payout_per_share") is None:
        return None
    shares = float(entry.get("size_shares") or 0)
    size_usd = float(entry.get("size_usd") or 0)
    gross = float(resolution["payout_per_share"]) * shares
    net = gross - gross * params.fee_rate
    return round(net - size_usd, 2)


def segment_by_price_band(replay_data: list[dict], params: BacktestParams,
                          band_width: float = 0.10) -> list[dict]:
    """Segment replayed entries by entry-price band (tenths of [0,1] by
    default) and report, per band, the realized return under the strategy
    (cashout policy in `params`) and the delta vs holding to resolution.

    This is the operational answer to the deep-research finding that on
    Polymarket cheap tokens (price <= 0.30) have historically NEGATIVE
    realized returns: it measures realized P&L / ROI by price band on our own
    data, and whether cashing out beat holding for the cheap bands.

    Per band: {band, price_lo, price_hi, n, total_pnl_usd, win_rate, mean_roi,
               n_delta, mean_delta_cashout_vs_hold_usd}. mean_roi = mean of
               (sim_pnl / entry cost). Delta is only over entries where both a
               cashout-path P&L and a hold-path P&L are computable (resolved).
    """
    n_bands = int(round(1.0 / band_width))
    agg: dict[int, dict] = {
        b: {"pnls": [], "rois": [], "deltas": []} for b in range(n_bands)
    }
    for data in replay_data:
        entry = data["entry"]
        price = float(entry.get("entry_price") or 0)
        # clamp into [0, n_bands-1]; price==1.0 lands in the top band
        b = min(n_bands - 1, max(0, int(price / band_width)))
        r = replay_entry(entry, data["checks"], data["resolution"], params)
        pnl = r["sim_pnl_usd"]
        if pnl is None:
            continue  # still open — no realized return to attribute
        agg[b]["pnls"].append(pnl)
        cost = price * float(entry.get("size_shares") or 0)
        if cost > 0:
            agg[b]["rois"].append(pnl / cost)
        hold = _hold_pnl(entry, data["resolution"], params)
        if hold is not None:
            agg[b]["deltas"].append(pnl - hold)

    out = []
    for b in range(n_bands):
        pnls = agg[b]["pnls"]
        if not pnls:
            continue
        wins = sum(1 for p in pnls if p > 0.001)
        losses = sum(1 for p in pnls if p < -0.001)
        resolved = wins + losses
        rois = agg[b]["rois"]
        deltas = agg[b]["deltas"]
        out.append({
            "band": b,
            "price_lo": round(b * band_width, 2),
            "price_hi": round((b + 1) * band_width, 2),
            "n": len(pnls),
            "total_pnl_usd": round(sum(pnls), 2),
            "win_rate": round(wins / resolved, 3) if resolved else None,
            "mean_roi": round(sum(rois) / len(rois), 4) if rois else None,
            "n_delta": len(deltas),
            "mean_delta_cashout_vs_hold_usd": (
                round(sum(deltas) / len(deltas), 2) if deltas else None),
        })
    return out


def default_param_grid(bid_slippage_pct: float = 0.0,
                        fee_rate: float = 0.0) -> list[BacktestParams]:
    """4 × 3 × 3 = 36 combos centered around current production defaults.
    Friction params (slippage, fee) are constant across the grid — only
    the cashout-policy thresholds vary."""
    grid = []
    for pl in (30, 40, 50, 60):
        for td in (20, 30, 40):
            for cv in (3, 5, 7):
                grid.append(BacktestParams(
                    profit_lock_pp=pl,
                    trailing_drawdown_pct=td,
                    convergence_pp=cv,
                    bid_slippage_pct=bid_slippage_pct,
                    fee_rate=fee_rate,
                ))
    return grid


def _format_summary_table(results: list[dict], top_k: int) -> str:
    rows = ["", "Top configurations by simulated P&L:", ""]
    rows.append(f"{'Rank':<5} {'profit_lock':<12} {'trailing':<10} "
                f"{'converge':<10} {'P&L $':<10} {'Win rate':<10} "
                f"{'Wins/Loss':<12}")
    rows.append("-" * 75)
    for i, r in enumerate(results[:top_k]):
        p = r["params"]
        wr = f"{r['win_rate']*100:.0f}%" if r["win_rate"] is not None else "—"
        rows.append(
            f"{i+1:<5} {p['profit_lock_pp']:<12} "
            f"{p['trailing_drawdown_pct']:<10} {p['convergence_pp']:<10} "
            f"${r['total_pnl_usd']:<9.2f} {wr:<10} "
            f"{r['n_wins']}W/{r['n_losses']}L"
        )
    return "\n".join(rows)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--since-days", type=int, default=30,
                   help="Lookback window in days (default 30).")
    p.add_argument("--limit", type=int, default=200,
                   help="Max entries to replay (default 200).")
    p.add_argument("--top-k", type=int, default=10,
                   help="Print top K configurations (default 10).")
    p.add_argument("--slippage-pct", type=float, default=0.0,
                   help="Bid haircut applied at cashout exits (default 0; "
                        "e.g. 2.0 simulates a 2%% slippage on the bid).")
    p.add_argument("--fee-rate", type=float, default=0.0,
                   help="Fee deducted from gross proceeds (default 0; "
                        "e.g. 0.02 simulates a 2%% fee).")
    p.add_argument("--output", choices=("text", "json"), default="text",
                   help="Output format (default text).")
    p.add_argument("--strategy", default=None,
                   help="Restrict to entries of this strategy (v11), e.g. "
                        "'cheap_convexity' or 'weather_edge'. NULL is treated "
                        "as 'weather_edge'. Omit for all strategies.")
    p.add_argument("--max-entry-price", type=float, default=None,
                   help="Restrict to entries priced below this (e.g. 0.30 to "
                        "measure realized return of the cheap band).")
    p.add_argument("--price-band-report", action="store_true",
                   help="Print realized return + cashout-vs-hold delta per "
                        "0.10 price band (the FLB-by-price-decile view).")
    args = p.parse_args()

    db.init_db()
    since = (datetime.now(timezone.utc)
             - timedelta(days=args.since_days)).isoformat()

    with db.connect() as conn:
        data = load_replay_data(conn, since, limit=args.limit,
                                strategy=args.strategy,
                                max_entry_price=args.max_entry_price)

    if not data:
        print(f"No executed entries found since {since[:10]}.",
              file=sys.stderr)
        return 0

    grid = default_param_grid(bid_slippage_pct=args.slippage_pct,
                                fee_rate=args.fee_rate)
    results = grid_search(data, grid)

    if args.price_band_report:
        # Use the best grid config's cashout policy for the band report.
        best = results[0]["params"]
        bp = BacktestParams(profit_lock_pp=best["profit_lock_pp"],
                            trailing_drawdown_pct=best["trailing_drawdown_pct"],
                            convergence_pp=best["convergence_pp"],
                            bid_slippage_pct=args.slippage_pct,
                            fee_rate=args.fee_rate)
        bands = segment_by_price_band(data, bp)
        if args.output == "json":
            print(json.dumps({"since_iso": since, "n": len(data),
                              "price_bands": bands}, indent=2, default=str))
        else:
            print(f"\nRealized return by price band "
                  f"({len(data)} trades, best cashout policy):\n")
            print(f"{'band':<12} {'n':<5} {'P&L $':<10} {'win':<7} "
                  f"{'mean ROI':<10} {'Δ cashout-hold $':<16}")
            print("-" * 62)
            for bd in bands:
                wr = f"{bd['win_rate']*100:.0f}%" if bd["win_rate"] is not None else "—"
                roi = f"{bd['mean_roi']*100:.0f}%" if bd["mean_roi"] is not None else "—"
                dl = ("—" if bd["mean_delta_cashout_vs_hold_usd"] is None
                      else f"{bd['mean_delta_cashout_vs_hold_usd']:+.2f}")
                print(f"{bd['price_lo']:.2f}-{bd['price_hi']:.2f}{'':<3} "
                      f"{bd['n']:<5} ${bd['total_pnl_usd']:<9.2f} {wr:<7} "
                      f"{roi:<10} {dl:<16}")
            print()
        return 0

    if args.output == "json":
        out = {
            "since_iso": since,
            "n_trades_replayed": len(data),
            "n_configs_tested": len(grid),
            "top_k": [r for r in results[:args.top_k]],
        }
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"Backtested {len(data)} trades across {len(grid)} param combos.")
        print(_format_summary_table(results, args.top_k))
        print()
    return 0


# ---------------------------------------------------------------------------
# Inline tests
# ---------------------------------------------------------------------------


def _run_tests() -> int:
    # Synthetic entry: NO @ $0.13, 100 shares
    entry = {
        "entry_id": 1, "side": "NO", "entry_price": 0.13,
        "size_shares": 100, "size_usd": 13.0,
    }
    # Monitor checks: bid climbing 0.20 → 0.55 then crashing to 0.30
    checks = [
        {"ts": f"2026-05-12T0{i}:00:00Z",
         "market_best_bid": bid,
         "forecast_prob_now": 0.95,  # P(NO) — chosen side
         "decision": "HOLD"}
        for i, bid in enumerate([0.20, 0.35, 0.50, 0.55, 0.40, 0.30])
    ]

    # Test 1: profit_lock_pp=50 → cap at 0.63, never reached → still_open
    r = replay_entry(entry, checks, resolution=None,
                     params=BacktestParams(profit_lock_pp=50))
    # peak 0.55, never hit 0.63. trailing drawdown: peak 0.55, 30% =
    # threshold 0.385; bid 0.30 < 0.385 → trailing fires
    assert r["sim_trigger"] == "trailing_stop", r
    print(f"Test 1 PASS: profit_lock=50 → trailing fires (bid 0.30 vs peak 0.55)")

    # Test 2: profit_lock_pp=30 → cap at 0.43, hit at bid 0.50 (3rd check)
    r = replay_entry(entry, checks, resolution=None,
                     params=BacktestParams(profit_lock_pp=30))
    assert r["sim_trigger"] == "profit_lock", r
    assert r["sim_exit_bid"] == 0.50  # first check >= 0.43
    print(f"Test 2 PASS: profit_lock=30 → cashout at $0.50, "
          f"P&L ${r['sim_pnl_usd']}")

    # Test 3: profit_lock=80 (cap 0.93, never), trailing=50% (peak*0.5=0.275),
    # bid never falls below 0.275 (min is 0.30). Sem cashout → resolution.
    res = {"final_outcome": "NO", "payout_per_share": 1.0}
    r = replay_entry(entry, checks, resolution=res,
                     params=BacktestParams(profit_lock_pp=80,
                                            trailing_drawdown_pct=50))
    assert r["sim_trigger"] == "hold_to_resolution", r
    # Holding: payout 1.0 * 100 shares - 13 cost = 87
    assert r["sim_pnl_usd"] == 87.0, r
    print(f"Test 3 PASS: hold_to_resolution → ${r['sim_pnl_usd']}")

    # Test 4: grid_search over 2 entries × 4 configs
    entry2 = {**entry, "entry_id": 2}
    data = [{"entry": entry, "checks": checks, "resolution": res},
            {"entry": entry2, "checks": checks, "resolution": res}]
    grid = [
        BacktestParams(profit_lock_pp=30),
        BacktestParams(profit_lock_pp=50),
        BacktestParams(profit_lock_pp=70),
        BacktestParams(profit_lock_pp=90),
    ]
    results = grid_search(data, grid)
    assert len(results) == 4
    # Best config should have higher total_pnl than worst
    assert results[0]["total_pnl_usd"] >= results[-1]["total_pnl_usd"]
    # 2 entries replayed each config
    for r in results:
        assert r["n_resolved"] + r["n_open"] == 2
    print(f"Test 4 PASS: grid_search → best ${results[0]['total_pnl_usd']} "
          f"vs worst ${results[-1]['total_pnl_usd']}")

    # Test 5: default_param_grid is 36 combos
    g = default_param_grid()
    assert len(g) == 36, len(g)
    print(f"Test 5 PASS: default_param_grid → {len(g)} combos")

    # Test 6: slippage 10% on a cashout. profit_lock=30 → cashout at first
    # bid >= 0.43. With 10% slippage, effective_bid = 0.50*0.9 = 0.45.
    r = replay_entry(entry, checks, resolution=None,
                     params=BacktestParams(profit_lock_pp=30,
                                            bid_slippage_pct=10.0))
    assert r["sim_trigger"] == "profit_lock", r
    assert abs(r["sim_exit_bid"] - 0.45) < 0.001, r
    # sim_pnl = (0.45 - 0.13) * 100 = 32.0
    assert abs(r["sim_pnl_usd"] - 32.0) < 0.01, r
    print(f"Test 6 PASS: slippage=10% → effective_bid {r['sim_exit_bid']}, "
          f"P&L ${r['sim_pnl_usd']}")

    # Test 7: fee 2% on hold_to_resolution. payout=1.0, shares=100, cost=13.
    # gross = 100.0, fee = 2.0, net = 98.0, pnl = 98 - 13 = 85.
    r = replay_entry(entry, checks, resolution=res,
                     params=BacktestParams(profit_lock_pp=80,
                                            trailing_drawdown_pct=50,
                                            fee_rate=0.02))
    assert r["sim_trigger"] == "hold_to_resolution", r
    assert abs(r["sim_pnl_usd"] - 85.0) < 0.01, r
    print(f"Test 7 PASS: fee=2% on hold → P&L ${r['sim_pnl_usd']} (was 87 w/o fee)")

    # Test 8: segment_by_price_band — cheap NO @ 0.13 (band 1) that holds to a
    # winning resolution, plus a pricier YES @ 0.55 (band 5) that loses at
    # resolution. Verifies banding, per-band P&L, and cashout-vs-hold delta.
    cheap = {"entry_id": 10, "side": "NO", "entry_price": 0.13,
             "size_shares": 100, "size_usd": 13.0}
    cheap_res = {"final_outcome": "NO", "payout_per_share": 1.0}
    # bid climbs but never triggers cashout under a loose policy → holds
    cheap_checks = [{"ts": f"2026-06-0{i}T00:00:00Z", "market_best_bid": bid,
                     "forecast_prob_now": 0.95, "decision": "HOLD"}
                    for i, bid in enumerate([0.20, 0.30, 0.40], start=1)]
    pricey = {"entry_id": 11, "side": "YES", "entry_price": 0.55,
              "size_shares": 100, "size_usd": 55.0}
    pricey_res = {"final_outcome": "NO", "payout_per_share": 0.0}
    pricey_checks = [{"ts": f"2026-06-0{i}T00:00:00Z", "market_best_bid": bid,
                      "forecast_prob_now": 0.10, "decision": "HOLD"}
                     for i, bid in enumerate([0.50, 0.45, 0.40], start=1)]
    data8 = [
        {"entry": cheap, "checks": cheap_checks, "resolution": cheap_res},
        {"entry": pricey, "checks": pricey_checks, "resolution": pricey_res},
    ]
    # Loose policy so neither cashes out → both hold to resolution.
    bands = segment_by_price_band(
        data8, BacktestParams(profit_lock_pp=999, trailing_drawdown_pct=100,
                              trailing_min_gain_pp=999, convergence_pp=0))
    by_band = {b["band"]: b for b in bands}
    assert set(by_band) == {1, 5}, by_band          # 0.13→band1, 0.55→band5
    assert by_band[1]["n"] == 1 and by_band[5]["n"] == 1
    # cheap held to win: payout 1.0*100 - 13 = +87; pricey held to loss: -55
    assert by_band[1]["total_pnl_usd"] == 87.0, by_band[1]
    assert by_band[5]["total_pnl_usd"] == -55.0, by_band[5]
    # both paths held → cashout-vs-hold delta ~0
    assert by_band[1]["mean_delta_cashout_vs_hold_usd"] == 0.0, by_band[1]
    print("Test 8 PASS: segment_by_price_band → band1 $87 / band5 -$55, "
          "delta 0 (both held)")

    print("\nAll backtest tests PASS")
    return 0


if __name__ == "__main__":
    if "--test" in sys.argv:
        sys.exit(_run_tests())
    sys.exit(main())
