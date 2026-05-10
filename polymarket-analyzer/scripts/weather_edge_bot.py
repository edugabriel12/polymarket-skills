#!/usr/bin/env python3
"""Weather edge bot — discovery, entry proposal, monitor, cashout.

Runs 24/7 as a daemon. Each loop iteration (60s tick) decides which tasks to
run based on per-task last_run timestamps:

  Discovery  → every 10 min: scan weather markets resolving in next 48h,
                parse, fetch forecast, compute edge, INSERT entries(PROPOSED).
  Execute    → every 60s: pick up entries(APPROVED) from judge, re-validate
                orderbook + risk, execute via paper_engine, mark EXECUTED.
  Monitor    → every 60s: walk open positions; per-position adaptive cadence
                (30 min if TTR<24h, 60 min if 24-48h). Cash out only when
                forecast_prob < entry_implied AND best_bid >= entry_price.
  Resolution → every 1h: poll Gamma for outcomePrices on past-end positions.
  Heartbeat  → every 5 min: emit health log line.

Paper-only mode (default). To bypass the judge gatekeeper, pass --judge-mode=off
(operator override; not recommended).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "polymarket-analyzer" / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "polymarket-scanner" / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "polymarket-paper-trader" / "scripts"))


def _load_dotenv() -> None:
    """Minimal .env loader (no python-dotenv dep). Reads agent/.env if present
    and sets missing env vars. Existing OS env vars take precedence."""
    env_path = REPO_ROOT / "agent" / ".env"
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception:
        pass


_load_dotenv()

from weather_edge_helpers import (  # noqa: E402
    parse_market, forecast_probability, compute_max_size_for_slippage,
    implied_probabilities, compute_edge, load_cities, MarketSpec,
)
import weather_edge_db as db  # noqa: E402

LOG_DIR = Path.home() / ".polymarket-paper"
LOG_FILE = LOG_DIR / "weather_edge.jsonl"

GAMMA_API = "https://gamma-api.polymarket.com"
WEATHER_TAG = "weather"
FORECAST_SCRIPT = REPO_ROOT / "polymarket-forecast-skill" / "scripts" / "get_weather.py"

# Default cadences (seconds)
DISCOVERY_INTERVAL = 600        # 10 min
MONITOR_TICK = 60               # check every minute; per-position adaptive
HEARTBEAT_INTERVAL = 300        # 5 min
RESOLUTION_SWEEP_INTERVAL = 3600  # 1 hour
EXECUTE_INTERVAL = 60           # check approved queue every minute

PAPER_DB = Path.home() / ".polymarket-paper" / "portfolio.db"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_event(event_type: str, payload: dict | None = None, level: str = "INFO") -> None:
    """Append one JSONL line to the log file AND print to stdout (journald)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    rec = {"ts": _now_iso(), "level": level, "event_type": event_type,
           "payload": payload or {}}
    line = json.dumps(rec, default=str, ensure_ascii=False)
    try:
        with LOG_FILE.open("a") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"[log-error] {e}", file=sys.stderr, flush=True)
    print(line, flush=True)


# ---------------------------------------------------------------------------
# Gamma + forecast helpers
# ---------------------------------------------------------------------------


def fetch_weather_markets(min_volume: float = 5000, limit: int = 100) -> list[dict]:
    """Fetch closed=false weather markets from Gamma."""
    try:
        r = requests.get(f"{GAMMA_API}/markets", params={
            "tag_slug": WEATHER_TAG, "active": "true", "closed": "false",
            "limit": limit, "order": "endDate", "ascending": "true",
        }, timeout=30)
        r.raise_for_status()
        markets = r.json()
    except requests.exceptions.RequestException as e:
        log_event("error", {"where": "fetch_weather_markets", "err": str(e)},
                  level="WARN")
        return []

    out = []
    for m in markets:
        try:
            vol = float(m.get("volumeNum", 0) or 0)
            if vol < min_volume:
                continue
            out.append(m)
        except (ValueError, TypeError):
            continue
    return out


def fetch_orderbook(token_id: str) -> Optional[dict]:
    """Fetch orderbook for a CLOB token. Returns {bids, asks} or None on error."""
    try:
        from py_clob_client.client import ClobClient
        client = ClobClient("https://clob.polymarket.com")
        book = client.get_order_book(token_id)
        return {
            "bids": [{"price": float(b.price), "size": float(b.size)} for b in book.bids],
            "asks": [{"price": float(a.price), "size": float(a.size)} for a in book.asks],
        }
    except Exception as e:
        log_event("error", {"where": "fetch_orderbook", "token_id": token_id[:12],
                            "err": str(e)}, level="WARN")
        return None


def fetch_forecast(city: str, days: int = 5) -> Optional[dict]:
    """Subprocess get_weather.py forecast and return parsed JSON, or None."""
    try:
        result = subprocess.run(
            [sys.executable, str(FORECAST_SCRIPT), "forecast", city, str(days)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            log_event("error", {"where": "fetch_forecast", "city": city,
                                "stderr": result.stderr[:500]}, level="WARN")
            return None
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        log_event("error", {"where": "fetch_forecast", "city": city,
                            "err": str(e)}, level="WARN")
        return None


# ---------------------------------------------------------------------------
# Discovery cycle: scan + propose entries
# ---------------------------------------------------------------------------


def run_discovery(args, cities: dict) -> int:
    """Run one discovery scan. Returns count of new proposals inserted."""
    log_event("discovery_start", {"min_edge_pp": args.min_edge_pp,
                                  "min_volume": args.min_volume,
                                  "window_hours": args.window_hours})
    raw_markets = fetch_weather_markets(min_volume=args.min_volume)
    log_event("discovery_markets_fetched", {"count": len(raw_markets)})
    if not raw_markets:
        return 0

    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=args.window_hours)
    proposed = 0
    # Diagnostic counters
    skipped = {
        "no_end_date": 0, "outside_window": 0, "parser_failed": 0,
        "parser_low_confidence": 0, "no_token_ids": 0,
        "orderbook_unavailable": 0, "no_implied_prices": 0,
        "price_band_miss": 0, "forecast_unavailable": 0,
        "no_forecast_for_target_date": 0, "low_edge": 0,
        "already_proposed": 0,
    }

    with db.connect() as conn:
        for m in raw_markets:
            slug = m.get("slug", "")
            question = m.get("question", "")
            end_date_str = m.get("endDate", "")
            try:
                end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                skipped["no_end_date"] += 1
                continue

            # Window filter: must resolve within window
            if end_date < now or end_date > cutoff:
                skipped["outside_window"] += 1
                if args.debug:
                    log_event("market_skipped", {"slug": slug,
                                                  "reason": "outside_window",
                                                  "end_date": end_date_str,
                                                  "ttr_h": round((end_date - now).total_seconds() / 3600, 1)})
                continue
            ttr_hours = (end_date - now).total_seconds() / 3600.0

            # Parse market spec
            spec = parse_market(question, end_date_str, cities)
            if not spec:
                skipped["parser_failed"] += 1
                log_event("market_skipped", {"slug": slug, "reason": "parser_failed",
                                              "question": question[:100]})
                continue
            if spec.confidence < 0.5:
                skipped["parser_low_confidence"] += 1
                log_event("market_skipped", {"slug": slug,
                                              "reason": "parser_low_confidence",
                                              "confidence": spec.confidence})
                continue

            # Token IDs
            try:
                token_ids = json.loads(m.get("clobTokenIds", "[]"))
                if len(token_ids) < 2:
                    skipped["no_token_ids"] += 1
                    continue
                token_id_yes, token_id_no = str(token_ids[0]), str(token_ids[1])
            except (json.JSONDecodeError, TypeError):
                skipped["no_token_ids"] += 1
                continue

            # Fetch orderbooks
            book_yes = fetch_orderbook(token_id_yes)
            book_no = fetch_orderbook(token_id_no)
            if not book_yes or not book_no:
                skipped["orderbook_unavailable"] += 1
                continue
            implied = implied_probabilities(book_yes, book_no)
            if implied["yes_ask"] is None or implied["no_ask"] is None:
                skipped["no_implied_prices"] += 1
                continue

            # Price band filter
            if not (args.min_price <= implied["yes_ask"] <= args.max_price or
                    args.min_price <= implied["no_ask"] <= args.max_price):
                skipped["price_band_miss"] += 1
                if args.debug:
                    log_event("market_skipped", {"slug": slug, "reason": "price_band_miss",
                                                  "yes_ask": implied["yes_ask"],
                                                  "no_ask": implied["no_ask"]})
                continue

            # Fetch forecast
            forecast = fetch_forecast(spec.city,
                                      days=max(2, int(ttr_hours / 24) + 1))
            if not forecast:
                skipped["forecast_unavailable"] += 1
                log_event("market_skipped", {"slug": slug, "reason": "forecast_unavailable",
                                              "city": spec.city})
                continue

            forecast_prob = forecast_probability(spec, forecast)
            if forecast_prob is None:
                skipped["no_forecast_for_target_date"] += 1
                log_event("market_skipped", {"slug": slug, "reason": "no_forecast_for_target_date"})
                continue

            edge = compute_edge(forecast_prob, implied)
            if edge["best_side"] is None or edge["edge_pp_at_best"] < args.min_edge_pp:
                skipped["low_edge"] += 1
                log_event("market_evaluated", {
                    "slug": slug, "side": edge["best_side"],
                    "edge_pp": edge["edge_pp_at_best"],
                    "decision": "skipped_low_edge",
                    "forecast_prob": forecast_prob,
                    "yes_ask": implied["yes_ask"],
                    "no_ask": implied["no_ask"],
                })
                continue

            side = edge["best_side"]
            entry_price = implied["yes_ask"] if side == "YES" else implied["no_ask"]
            # Recheck price band on the chosen side
            if not (args.min_price <= entry_price <= args.max_price):
                skipped["price_band_miss"] += 1
                continue

            # Already proposed?
            if db.market_already_proposed(conn, slug, side):
                skipped["already_proposed"] += 1
                continue

            # Propose
            entry_id = db.insert_entry(
                conn,
                ts=_now_iso(),
                market_slug=slug,
                market_question=question,
                condition_id=m.get("conditionId", ""),
                token_id_yes=token_id_yes,
                token_id_no=token_id_no,
                end_date=end_date_str,
                side=side,
                entry_price=entry_price,
                forecast_prob_at_entry=forecast_prob if side == "YES" else 1.0 - forecast_prob,
                implied_prob_at_entry=entry_price,
                edge_pp_at_entry=edge["edge_pp_at_best"],
                forecast_snapshot_json=forecast,
                parser_confidence=spec.confidence,
                city_resolved=spec.city,
                threshold_value=spec.threshold_value,
                threshold_unit=spec.threshold_unit,
                comparison=spec.comparison,
                ttr_hours_at_entry=ttr_hours,
                status="PROPOSED",
            )
            proposed += 1
            log_event("entry_proposed", {
                "entry_id": entry_id, "slug": slug, "side": side,
                "entry_price": entry_price,
                "forecast_prob": forecast_prob,
                "edge_pp": edge["edge_pp_at_best"],
                "city": spec.city, "ttr_h": round(ttr_hours, 1),
            })

    log_event("discovery_end", {"proposed": proposed,
                                 "fetched": len(raw_markets),
                                 "skipped_breakdown": skipped})
    return proposed


# ---------------------------------------------------------------------------
# Execute cycle: pick up APPROVED entries from judge, execute paper trade
# ---------------------------------------------------------------------------


def run_execute(args) -> int:
    """Pick up APPROVED entries and execute them via paper_engine."""
    executed = 0
    with db.connect() as conn:
        rows = db.query_approved_unexecuted(conn)
        if not rows:
            return 0

        # Lazy import paper_engine
        try:
            from paper_engine import PaperEngine, DEFAULT_FEE_RATE
        except ImportError as e:
            log_event("error", {"where": "execute", "err": f"paper_engine import: {e}"},
                      level="ERROR")
            return 0

        engine = PaperEngine(portfolio=args.portfolio)

        for row in rows:
            entry_id = row["entry_id"]
            side = row["side"]
            token_id = row["token_id_yes"] if side == "YES" else row["token_id_no"]
            book = fetch_orderbook(token_id)
            if not book or not book.get("asks"):
                log_event("execute_skipped", {"entry_id": entry_id,
                                              "reason": "no_orderbook"})
                continue

            # Slippage-aware sizing
            sizing = compute_max_size_for_slippage(book, "BUY",
                                                   max_slippage=args.max_slippage)
            if sizing["max_shares"] == 0:
                log_event("execute_skipped", {"entry_id": entry_id,
                                              "reason": "zero_max_size"})
                continue

            # Cap by portfolio caps (10% per trade) — paper engine validates again
            try:
                portfolio = engine.get_portfolio()
                portfolio_value = portfolio.get("total_value", 0)
            except Exception as e:
                log_event("error", {"where": "execute_portfolio", "err": str(e)})
                continue
            per_trade_cap_usd = portfolio_value * 0.10

            target_usd = min(sizing["max_usd"], per_trade_cap_usd)
            if target_usd < 10:
                log_event("execute_skipped", {"entry_id": entry_id,
                                              "reason": "size_below_min_$10"})
                continue

            if args.dry_run:
                log_event("execute_dry_run", {"entry_id": entry_id,
                                              "side": side, "target_usd": target_usd,
                                              "avg_fill": sizing["avg_fill"],
                                              "slippage_pct": sizing["slippage_pct"]})
                db.update_entry_status(conn, entry_id, "EXECUTED",
                                       size_usd=target_usd,
                                       size_shares=sizing["max_shares"],
                                       entry_price=sizing["avg_fill"])
                executed += 1
                continue

            # Real paper execution via PaperEngine
            try:
                result = engine.open_position(
                    token_id=token_id,
                    side=side,
                    size_usd=target_usd,
                    market_question=row["market_question"][:200],
                    fee_rate=DEFAULT_FEE_RATE,
                    confidence=0.65,  # generic confidence; judge gives real one
                    reasoning=f"weather_edge_bot entry_id={entry_id}",
                )
                if result.get("status") == "executed":
                    db.update_entry_status(conn, entry_id, "EXECUTED",
                                           size_usd=result.get("cost_usd"),
                                           size_shares=result.get("shares_filled"),
                                           entry_price=result.get("avg_price"))
                    log_event("entry_executed", {"entry_id": entry_id,
                                                 "shares": result.get("shares_filled"),
                                                 "avg_price": result.get("avg_price")})
                    executed += 1
                else:
                    log_event("execute_rejected", {"entry_id": entry_id,
                                                   "reason": result.get("reason")})
                    db.update_entry_status(conn, entry_id, "SKIPPED",
                                           skip_reason=str(result.get("reason"))[:200])
            except Exception as e:
                log_event("error", {"where": "open_position",
                                    "entry_id": entry_id, "err": str(e)})

    return executed


# ---------------------------------------------------------------------------
# Monitor cycle: per-position adaptive cashout check
# ---------------------------------------------------------------------------


def _ttr_hours(end_date_str: str) -> float:
    try:
        end = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        return max(0, (end - datetime.now(timezone.utc)).total_seconds() / 3600)
    except (ValueError, TypeError):
        return 0


def _monitor_interval_for_ttr(ttr_h: float) -> int:
    if ttr_h < 24:
        return 30 * 60   # 30 min
    return 60 * 60       # 60 min


_last_monitor_per_entry: dict[int, float] = {}


def run_monitor_tick(args, cities: dict) -> None:
    """Check each open position; if its adaptive interval elapsed, do a check."""
    now_mono = time.monotonic()
    with db.connect() as conn:
        rows = db.query_open_positions(conn)
        for row in rows:
            entry_id = row["entry_id"]
            ttr_h = _ttr_hours(row["end_date"] or "")
            interval = _monitor_interval_for_ttr(ttr_h)
            last = _last_monitor_per_entry.get(entry_id, 0)
            if now_mono - last < interval:
                continue
            _last_monitor_per_entry[entry_id] = now_mono
            _do_monitor_check(conn, row, cities, args)


def _do_monitor_check(conn, row, cities: dict, args) -> None:
    entry_id = row["entry_id"]
    side = row["side"]
    spec = parse_market(row["market_question"], row["end_date"], cities)
    forecast = fetch_forecast(spec.city) if spec else None
    if not spec or not forecast:
        log_event("monitor_check", {"entry_id": entry_id, "decision": "HOLD",
                                    "reason": "no_forecast_or_spec"})
        return
    forecast_prob_yes = forecast_probability(spec, forecast)
    if forecast_prob_yes is None:
        return
    forecast_prob_now = forecast_prob_yes if side == "YES" else 1 - forecast_prob_yes
    entry_implied = float(row["implied_prob_at_entry"] or row["entry_price"])

    token_id = row["token_id_yes"] if side == "YES" else row["token_id_no"]
    book = fetch_orderbook(token_id)
    if not book:
        return
    bid = book["bids"][0]["price"] if book.get("bids") else 0
    ask = book["asks"][0]["price"] if book.get("asks") else 0

    decision = "HOLD"
    reason = ""
    if forecast_prob_now < entry_implied:
        if bid >= float(row["entry_price"]):
            decision = "CASHOUT"
            reason = f"forecast_below_entry ({forecast_prob_now:.3f}<{entry_implied:.3f}); bid {bid:.3f}>=entry {row['entry_price']:.3f}"
        else:
            decision = "TRY_CASHOUT_BLOCKED"
            reason = f"forecast_below_entry but bid {bid:.3f}<entry {row['entry_price']:.3f}; holding"
    else:
        reason = f"forecast_prob {forecast_prob_now:.3f} >= entry {entry_implied:.3f}; edge intact"

    db.insert_monitor_check(
        conn, entry_id=entry_id,
        ts=_now_iso(),
        forecast_prob_now=forecast_prob_now,
        forecast_snapshot_json=forecast,
        market_best_bid=bid,
        market_best_ask=ask,
        decision=decision,
        decision_reason=reason,
    )
    log_event("monitor_check", {"entry_id": entry_id, "decision": decision,
                                "forecast_prob_now": forecast_prob_now,
                                "entry_implied": entry_implied,
                                "bid": bid, "reason": reason})

    if decision == "CASHOUT":
        _do_cashout(conn, row, bid, forecast, forecast_prob_now, args, reason)


def _do_cashout(conn, row, bid: float, forecast: dict,
                forecast_prob_now: float, args, reason: str) -> None:
    entry_id = row["entry_id"]
    side = row["side"]
    token_id = row["token_id_yes"] if side == "YES" else row["token_id_no"]

    if args.dry_run:
        log_event("cashout_dry_run", {"entry_id": entry_id, "bid": bid})
        return

    try:
        from paper_engine import PaperEngine
        engine = PaperEngine(portfolio=args.portfolio)
        result = engine.close_position(token_id=token_id, side=side,
                                        reasoning=f"weather_edge_bot: {reason}")
        if result.get("status") == "closed":
            db.insert_cashout(
                conn, entry_id=entry_id,
                ts=_now_iso(),
                exit_price=result.get("avg_price"),
                exit_shares=result.get("shares_sold"),
                realized_pnl_usd=result.get("realized_pnl"),
                forecast_prob_at_exit=forecast_prob_now,
                forecast_snapshot_json=forecast,
                reason=reason[:200],
            )
            log_event("cashout_executed", {"entry_id": entry_id,
                                            "exit_price": result.get("avg_price"),
                                            "pnl": result.get("realized_pnl")})
        else:
            log_event("cashout_rejected", {"entry_id": entry_id,
                                            "reason": result.get("reason")})
    except Exception as e:
        log_event("error", {"where": "cashout", "entry_id": entry_id, "err": str(e)})


# ---------------------------------------------------------------------------
# Resolution sweep
# ---------------------------------------------------------------------------


def run_resolution_sweep() -> int:
    """For each EXECUTED position past end_date, fetch outcomePrices and persist."""
    resolved = 0
    with db.connect() as conn:
        rows = db.query_unresolved_past_end(conn, _now_iso())
        for row in rows:
            slug = row["market_slug"]
            try:
                r = requests.get(f"{GAMMA_API}/markets",
                                 params={"slug": slug}, timeout=15)
                r.raise_for_status()
                results = r.json()
                if not isinstance(results, list) or not results:
                    continue
                m = results[0]
                outcomes = json.loads(m.get("outcomes", "[]"))
                prices = [float(p) for p in json.loads(m.get("outcomePrices", "[]"))]
                if not outcomes or not prices or len(outcomes) != len(prices):
                    continue
                # YES is index 0, NO is index 1 (Polymarket convention)
                final_outcome = "YES" if prices[0] >= 0.99 else \
                                "NO" if prices[1] >= 0.99 else "VOID"
                payout = 1.0 if (final_outcome == row["side"]) else 0.0
                if final_outcome == "VOID":
                    payout = float(row["entry_price"] or 0)  # neutral
                db.insert_resolution(
                    conn, entry_id=row["entry_id"],
                    ts_resolved=_now_iso(),
                    final_outcome=final_outcome,
                    payout_per_share=payout,
                )
                resolved += 1
                log_event("resolution_observed", {"entry_id": row["entry_id"],
                                                   "slug": slug,
                                                   "outcome": final_outcome,
                                                   "payout": payout})
            except Exception as e:
                log_event("error", {"where": "resolution_sweep",
                                    "slug": slug, "err": str(e)}, level="WARN")
    return resolved


# ---------------------------------------------------------------------------
# Main daemon loop
# ---------------------------------------------------------------------------


_shutdown = False


def _handle_sigterm(signum, frame):
    global _shutdown
    log_event("shutdown_signal", {"signal": signum})
    _shutdown = True


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--min-edge-pp", type=float, default=10.0)
    p.add_argument("--min-volume", type=float, default=5000)
    p.add_argument("--min-price", type=float, default=0.20)
    p.add_argument("--max-price", type=float, default=0.70)
    p.add_argument("--max-slippage", type=float, default=0.20)
    p.add_argument("--window-hours", type=float, default=48)
    p.add_argument("--daemon", action="store_true", default=True)
    p.add_argument("--once", action="store_true",
                   help="Run discovery+monitor once and exit")
    p.add_argument("--discovery-interval-min", type=float, default=10)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--judge-mode", choices=("sync", "off"), default="sync")
    p.add_argument("--fast-path-ttr-min", type=int, default=60)
    p.add_argument("--portfolio", default="default")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()

    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    db.init_db()
    cities = load_cities()
    log_event("startup", {"args": vars(args), "cities_loaded": len(cities.get("us_top50", [])) +
                          len(cities.get("world", [])) + len(cities.get("europe_top30", [])) +
                          len(cities.get("north_america_extra", []))})

    if args.once:
        run_discovery(args, cities)
        if args.judge_mode == "off":
            run_execute(args)
        run_monitor_tick(args, cities)
        run_resolution_sweep()
        return

    discovery_int = args.discovery_interval_min * 60
    last_discovery = 0.0
    last_heartbeat = 0.0
    last_resolution = 0.0
    last_execute = 0.0

    while not _shutdown:
        now_mono = time.monotonic()
        try:
            if now_mono - last_discovery >= discovery_int:
                run_discovery(args, cities)
                last_discovery = now_mono
            if now_mono - last_execute >= EXECUTE_INTERVAL:
                # Bot only auto-executes if judge is off OR judge already approved.
                # In sync mode, query_approved_unexecuted picks up only judge-vetted.
                run_execute(args)
                last_execute = now_mono
            run_monitor_tick(args, cities)
            if now_mono - last_resolution >= RESOLUTION_SWEEP_INTERVAL:
                run_resolution_sweep()
                last_resolution = now_mono
            if now_mono - last_heartbeat >= HEARTBEAT_INTERVAL:
                with db.connect() as conn:
                    open_count = len(db.query_open_positions(conn))
                    pending_count = len(db.query_pending_proposals(conn))
                log_event("heartbeat", {"open_positions": open_count,
                                         "pending_proposals": pending_count})
                last_heartbeat = now_mono
        except Exception as e:
            log_event("error", {"where": "main_loop", "err": str(e),
                                "type": type(e).__name__}, level="ERROR")

        time.sleep(MONITOR_TICK)

    log_event("shutdown_clean", {})


if __name__ == "__main__":
    main()
