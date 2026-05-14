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
        )
        if verdict["decision"] == "CASHOUT":
            sim_pnl = (bid - entry_price) * shares
            return {
                "sim_exit_ts": check.get("ts"),
                "sim_exit_bid": round(bid, 4),
                "sim_trigger": verdict["trigger"],
                "sim_pnl_usd": round(sim_pnl, 2),
            }

    # No cashout fired — fall through to resolution outcome
    if resolution and resolution.get("payout_per_share") is not None:
        payout = float(resolution["payout_per_share"])
        sim_pnl = payout * shares - size_usd
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


def load_replay_data(conn, since_iso: str, limit: int = 200) -> list[dict]:
    """Aggregate per-entry: entry row + ordered monitor_checks + resolution.

    Filters to EXECUTED/FAST_PATH entries since `since_iso`. Returns up to
    `limit` rows (most recent first). Each output dict:
      {"entry": {...}, "checks": [...], "resolution": {...|None}}
    """
    entries = conn.execute(
        "SELECT * FROM entries WHERE ts >= ? "
        "  AND status IN ('EXECUTED', 'FAST_PATH') "
        "ORDER BY ts DESC LIMIT ?",
        (since_iso, limit),
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


def default_param_grid() -> list[BacktestParams]:
    """4 × 3 × 3 = 36 combos centered around current production defaults."""
    grid = []
    for pl in (30, 40, 50, 60):
        for td in (20, 30, 40):
            for cv in (3, 5, 7):
                grid.append(BacktestParams(
                    profit_lock_pp=pl,
                    trailing_drawdown_pct=td,
                    convergence_pp=cv,
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
    p.add_argument("--output", choices=("text", "json"), default="text",
                   help="Output format (default text).")
    args = p.parse_args()

    db.init_db()
    since = (datetime.now(timezone.utc)
             - timedelta(days=args.since_days)).isoformat()

    with db.connect() as conn:
        data = load_replay_data(conn, since, limit=args.limit)

    if not data:
        print(f"No executed entries found since {since[:10]}.",
              file=sys.stderr)
        return 0

    grid = default_param_grid()
    results = grid_search(data, grid)

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

    print("\nAll backtest tests PASS")
    return 0


if __name__ == "__main__":
    if "--test" in sys.argv:
        sys.exit(_run_tests())
    sys.exit(main())
