"""Weather Strategy Advisor — Weekly meta-agent that reads bot performance
data and proposes tuning suggestions for operator review.

Read-only. Output: markdown report + JSON sidecar in
~/.polymarket-paper/advisor_reports/.

Usage:
    # On-demand single run
    python weather_strategy_advisor.py --once --since-days 30

    # Dry run (skip API call, print what would be sent)
    python weather_strategy_advisor.py --dry-run --since-days 30

    # Mock LLM (skip API, inject canned suggestions, persist artifacts)
    python weather_strategy_advisor.py --once --mock-llm --since-days 30
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "polymarket-analyzer" / "scripts"))


def _load_dotenv() -> None:
    """Minimal .env loader: agent/.env > OS env."""
    env_path = REPO_ROOT / "agent" / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


_load_dotenv()

import weather_edge_db as db  # noqa: E402
import strategy_advisor_helpers as helpers  # noqa: E402
import weather_edge_backtest as backtest  # noqa: E402
from weather_edge_analyzer import (  # noqa: E402
    aggregate_by_bucket,
    aggregate_judge,
    aggregate_cashout_triggers,
    compute_counterfactuals,
    compute_judge_accuracy,
    compute_discovery_meta_breakdown,  # v8 observability
    compute_ladder_breakdown,           # v9 ladder analytics
    format_report_md,
)

PROMPT_PATH = REPO_ROOT / "polymarket-analyzer" / "references" / "strategy-advisor-prompt.md"
LOG_PATH = Path.home() / ".polymarket-paper" / "weather_edge.jsonl"

DEFAULT_MODEL = os.environ.get("ADVISOR_MODEL", "claude-opus-4-7")
WEEKLY_BUDGET_USD = float(os.environ.get("ADVISOR_WEEKLY_BUDGET_USD", "5.0"))
MIN_TRADES_FOR_REC = int(os.environ.get("ADVISOR_MIN_TRADES_FOR_REC", "10"))
# Output-token budget. Must be large enough to cover adaptive-thinking
# tokens + web_search/web_fetch tool turns + the full structured-JSON
# report. The old 8192 was shared across all three and the JSON got
# truncated mid-string (operator run #3, 2026-06-01: "JSONDecodeError:
# Unterminated string ... char 13372"). Opus 4.x supports up to 32K output.
MAX_TOKENS = int(os.environ.get("ADVISOR_MAX_TOKENS", "32000"))

PRICING = {
    "claude-opus-4-7": {"input": 15.00, "output": 75.00,
                        "cache_read": 1.50, "cache_write_1h": 18.75},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00,
                          "cache_read": 0.30, "cache_write_1h": 3.75},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00,
                         "cache_read": 0.10, "cache_write_1h": 1.25},
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_event(event_type: str, payload: dict | None = None,
              level: str = "INFO") -> None:
    rec = {"ts": _now_iso(), "level": level, "actor": "advisor",
           "event_type": event_type, "payload": payload or {}}
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")


_SUGGESTIONS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "n_trades_analyzed": {"type": "integer"},
        "summary": {"type": "string"},
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "category": {
                        "type": "string",
                        "enum": ["threshold", "mae_constant", "city",
                                 "judge_prompt", "data_source", "risk_limit"],
                    },
                    "priority": {"type": "string",
                                 "enum": ["high", "medium", "low"]},
                    "confidence": {"type": "string",
                                   "enum": ["high", "medium", "low"]},
                    "title": {"type": "string"},
                    "rationale": {"type": "string"},
                    "counterfactual": {"type": "string"},
                    "current_value": {"type": ["string", "number", "null"]},
                    "proposed_value": {"type": ["string", "number", "null"]},
                    "param_path": {"type": "string"},
                    # JSON-serialized string of supporting data dict.
                    "supporting_data": {"type": "string"},
                    "web_citations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "url": {"type": "string"},
                                "snippet": {"type": "string"},
                            },
                            "required": ["url", "snippet"],
                        },
                    },
                },
                "required": ["id", "category", "priority", "confidence",
                             "title", "rationale", "param_path"],
            },
        },
        "research_notes": {"type": "string"},
        # === Advisor v2: per-trade analysis ===
        "strategy_breakdown": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "strategy": {"type": "string"},
                    "n_trades": {"type": "integer"},
                    "win_rate": {"type": ["number", "null"]},
                    "total_pnl_usd": {"type": "number"},
                    "mean_pnl_usd": {"type": ["number", "null"]},
                    "notes": {"type": "string"},
                },
                "required": ["strategy", "n_trades", "notes"],
            },
        },
        "winner_patterns": {"type": "string"},
        "loser_patterns": {"type": "string"},
        "insights": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string"},
                    "observation": {"type": "string"},
                    "applies_to_category": {
                        "type": "string",
                        "enum": ["threshold", "mae_constant", "city",
                                 "judge_prompt", "data_source",
                                 "risk_limit", "operational"],
                    },
                    "n_supporting_trades": {"type": "integer"},
                    "supporting_trade_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                },
                "required": ["title", "observation",
                             "applies_to_category",
                             "n_supporting_trades"],
            },
        },
    },
    "required": ["n_trades_analyzed", "summary", "suggestions",
                 "research_notes", "strategy_breakdown",
                 "winner_patterns", "loser_patterns", "insights"],
}


def _spent_this_week(conn) -> float:
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM advisor_runs WHERE ts >= ?",
        (week_ago,),
    ).fetchone()
    return float(row[0] or 0)


def call_advisor_llm(system_prompt: str, user_payload: dict,
                     model: str, budget_usd: float) -> Optional[dict]:
    """Call Anthropic API. Returns parsed JSON dict or None on error.
    On failure the actual reason is stashed on the function attribute
    `call_advisor_llm.last_error` so main() can put it in
    advisor_runs.error_msg instead of a generic placeholder."""
    call_advisor_llm.last_error = None
    try:
        import anthropic
    except ImportError:
        log_event("error", {"where": "call_advisor", "err":
                            "anthropic SDK not installed"}, level="ERROR")
        call_advisor_llm.last_error = (
            "anthropic SDK not installed (pip install anthropic)")
        return None

    if not os.environ.get("ANTHROPIC_API_KEY"):
        log_event("error", {"where": "call_advisor",
                            "err": "ANTHROPIC_API_KEY missing"}, level="ERROR")
        call_advisor_llm.last_error = (
            "ANTHROPIC_API_KEY env var not set — the advisor cannot reach "
            "the Claude API")
        return None

    client = anthropic.Anthropic()
    user_text = json.dumps(user_payload, indent=2, default=str,
                           ensure_ascii=False)

    t0 = time.monotonic()
    try:
        response = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": system_prompt,
                     "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
            messages=[{"role": "user", "content": user_text}],
            thinking={"type": "adaptive"},
            tools=[
                {"type": "web_search_20250305", "name": "web_search",
                 "max_uses": 5},
                {"type": "web_fetch_20250910", "name": "web_fetch",
                 "max_uses": 10},
            ],
            output_config={
                "effort": "medium",
                "format": {"type": "json_schema",
                           "schema": _SUGGESTIONS_SCHEMA},
            },
        )
        duration_ms = int((time.monotonic() - t0) * 1000)
    except Exception as e:
        log_event("error", {"where": "anthropic_call", "err": str(e),
                            "type": type(e).__name__}, level="ERROR")
        # Stash so main() can surface the actual reason in advisor_runs.error_msg
        # (the previous behavior wrote a useless "API call failed; see
        # weather_edge.jsonl" with no hint of WHY).
        call_advisor_llm.last_error = f"{type(e).__name__}: {e}"
        return None

    text_block = next((b.text for b in response.content
                       if getattr(b, "type", None) == "text"), None)
    if not text_block:
        log_event("error", {"where": "advisor_response_empty",
                            "stop_reason": response.stop_reason},
                  level="ERROR")
        call_advisor_llm.last_error = (
            f"empty response from LLM (stop_reason={response.stop_reason})")
        return None

    try:
        parsed = json.loads(text_block)
    except json.JSONDecodeError as e:
        # The most common cause is the response being cut off at the output
        # cap (thinking + tool turns + JSON exceeded max_tokens), leaving an
        # unterminated string. Detect it and give an actionable message.
        truncated = getattr(response, "stop_reason", None) == "max_tokens"
        log_event("error", {"where": "advisor_json_decode", "err": str(e),
                            "stop_reason": getattr(response, "stop_reason", None),
                            "out_tokens": getattr(response.usage, "output_tokens", None),
                            "max_tokens": MAX_TOKENS,
                            "text_tail": text_block[-300:]}, level="ERROR")
        if truncated:
            call_advisor_llm.last_error = (
                f"LLM response truncated at max_tokens={MAX_TOKENS} "
                f"(output_tokens={getattr(response.usage, 'output_tokens', '?')}). "
                f"Raise ADVISOR_MAX_TOKENS or lower --per-trade-limit.")
        else:
            call_advisor_llm.last_error = f"JSONDecodeError: {e}"
        return None

    usage = response.usage
    pricing = PRICING.get(model, PRICING["claude-opus-4-7"])
    cost = (
        usage.input_tokens * pricing["input"] / 1_000_000
        + usage.output_tokens * pricing["output"] / 1_000_000
        + (getattr(usage, "cache_read_input_tokens", 0) or 0)
        * pricing["cache_read"] / 1_000_000
        + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
        * pricing["cache_write_1h"] / 1_000_000
    )

    if cost > budget_usd:
        log_event("warn", {"where": "budget_exceeded", "cost_usd": cost,
                           "budget_usd": budget_usd}, level="WARN")

    parsed["_meta"] = {
        "tokens_in": usage.input_tokens,
        "tokens_out": usage.output_tokens,
        "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_creation_tokens": getattr(
            usage, "cache_creation_input_tokens", 0) or 0,
        "cost_usd": round(cost, 4),
        "duration_ms": duration_ms,
        "model": model,
    }
    return parsed


def _mock_llm_response(n_trades: int) -> dict:
    """Deterministic mock for testing without API call (Advisor v2 shape)."""
    return {
        "n_trades_analyzed": n_trades,
        "summary": ("MOCK RESPONSE — synthetic per-trade analysis for testing. "
                    "Production prompt would analyze the data."),
        "strategy_breakdown": [
            {"strategy": "profit_lock", "n_trades": 5,
             "win_rate": 1.0, "total_pnl_usd": 50.0, "mean_pnl_usd": 10.0,
             "notes": "MOCK: all profit_lock exits profitable."},
            {"strategy": "trailing_stop", "n_trades": 3,
             "win_rate": 0.0, "total_pnl_usd": -8.0, "mean_pnl_usd": -2.67,
             "notes": "MOCK: trailing fires too early."},
        ],
        "winner_patterns": ("MOCK: winners had parser_confidence > 0.9, "
                            "edge > 30pp, TTR < 24h."),
        "loser_patterns": ("MOCK: losers concentrated in Manhattan/parser "
                           "confidence < 0.8, often exited via trailing_stop."),
        "insights": [{
            "title": "MOCK: tighten trailing_drawdown_pct",
            "observation": "MOCK observation referencing trades.",
            "applies_to_category": "threshold",
            "n_supporting_trades": 5,
            "supporting_trade_ids": [1, 2, 3, 4, 5],
        }],
        "suggestions": [{
            "id": "sug_mock_001",
            "category": "threshold",
            "priority": "medium",
            "confidence": "low",
            "title": "MOCK: Reduce --profit-lock-pp from 50 to 40",
            "rationale": "Mock rationale (no real analysis performed).",
            "counterfactual": "Mock counterfactual: $0.00 over N trades.",
            "current_value": 50.0,
            "proposed_value": 40.0,
            "param_path": "weather_edge_bot.py:--profit-lock-pp default",
            "supporting_data": "{\"n_samples\": 10, \"mock\": true}",
            "web_citations": [],
        }],
        "research_notes": "MOCK: replace with real LLM output in production.",
        "_meta": {"tokens_in": 0, "tokens_out": 0, "cache_read_tokens": 0,
                  "cache_creation_tokens": 0, "cost_usd": 0.0,
                  "duration_ms": 0, "model": "mock"},
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true",
                   help="Run a single cycle and exit (default true).")
    p.add_argument("--since-days", type=int, default=30,
                   help="Lookback window in days (default 30). Advisor v2 "
                        "analyzes per-trade detail across this entire window.")
    p.add_argument("--per-trade-limit", type=int, default=200,
                   help="Max per-trade rows sent to the LLM (default 200). "
                        "Caps token cost at ~30K input. Most recent trades "
                        "are kept when over the cap.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would be sent to API; do not call.")
    p.add_argument("--mock-llm", action="store_true",
                   help="Skip API call; inject mock JSON response.")
    p.add_argument("--force", action="store_true",
                   help="Run even if no new data since last run.")
    p.add_argument("--budget-usd", type=float, default=None,
                   help="Override per-run budget (default ADVISOR_WEEKLY_BUDGET_USD).")
    p.add_argument("--model", default=None,
                   help="Override model (default ADVISOR_MODEL).")
    p.add_argument("--trigger", default="cli",
                   choices=["scheduled_weekly", "on_demand", "cli"])
    p.add_argument("--job-id", type=int, default=None,
                   help="Update advisor_jobs row with this ID at start "
                        "(status='running') and end (status='done'/'failed' + "
                        "resulting_run_id). Set by the dashboard when "
                        "spawning the advisor as a subprocess.")
    p.add_argument("--slippage-pct", type=float, default=None,
                   help="Backtest bid haircut (default from "
                        "ADVISOR_BACKTEST_SLIPPAGE_PCT or 0.0)")
    p.add_argument("--fee-rate", type=float, default=None,
                   help="Backtest fee rate (default from "
                        "ADVISOR_BACKTEST_FEE_RATE or 0.0)")
    args = p.parse_args()

    db.init_db()
    model = args.model or DEFAULT_MODEL
    budget_usd = args.budget_usd or WEEKLY_BUDGET_USD

    since_iso = (datetime.now(timezone.utc)
                 - timedelta(days=args.since_days)).isoformat()

    log_event("advisor_startup", {"model": model, "since_days": args.since_days,
                                  "trigger": args.trigger,
                                  "dry_run": args.dry_run,
                                  "mock_llm": args.mock_llm})

    with db.connect() as conn:
        # Check skip conditions before doing real work
        if not args.force and not args.dry_run and not args.mock_llm:
            if not helpers.has_new_data_since_last_run(conn):
                log_event("advisor_skipped", {"reason": "no_new_data"})
                print("No new data since last run; skipping. Use --force to override.",
                      file=sys.stderr)
                return 0
            spent = _spent_this_week(conn)
            if spent >= budget_usd:
                log_event("advisor_skipped", {"reason": "weekly_budget_exhausted",
                                              "spent": spent, "budget": budget_usd})
                print(f"Weekly budget exhausted: spent ${spent:.2f} of "
                      f"${budget_usd:.2f}.", file=sys.stderr)
                return 0

        # Collect data
        compute_counterfactuals(conn, recompute=False)
        buckets = aggregate_by_bucket(conn, since_iso)
        judge = aggregate_judge(conn, since_iso)
        triggers = aggregate_cashout_triggers(conn, since_iso)
        extras = helpers.collect_extras(conn, since_iso)
        analyzer_md = format_report_md(buckets, judge, [], since_iso, triggers)
        current_config = helpers.read_current_config()

        # Per-trade detail (Advisor v2): one row per executed entry with
        # entry/judge/exit/resolution/counterfactual fields, classified by
        # exit_strategy and outcome_class.
        per_trade_rows = [dict(r) for r in db.query_per_trade_details(
            conn, since_iso, limit=args.per_trade_limit)]
        # Pre-classify so subsequent steps see _classification cached
        for t in per_trade_rows:
            t["_classification"] = helpers.classify_trade(t)
        per_trade_sample = helpers.compact_per_trade(per_trade_rows)
        strategy_breakdown = helpers.compute_strategy_breakdown(per_trade_rows)
        winner_loser = helpers.compute_winner_loser_patterns(per_trade_rows)

        # Advisor v3+v5: backtest with slippage/fee friction model.
        replay_data = backtest.load_replay_data(
            conn, since_iso, limit=args.per_trade_limit)
        defaults = current_config.get("cli_defaults", {})

        # Friction: CLI flags override env vars; default 0.
        if args.slippage_pct is not None:
            slippage_pct = float(args.slippage_pct)
        else:
            slippage_pct = float(os.environ.get(
                "ADVISOR_BACKTEST_SLIPPAGE_PCT", "0") or 0)
        if args.fee_rate is not None:
            fee_rate = float(args.fee_rate)
        else:
            fee_rate = float(os.environ.get(
                "ADVISOR_BACKTEST_FEE_RATE", "0") or 0)

        baseline_params = backtest.BacktestParams(
            profit_lock_pp=float(defaults.get("--profit-lock-pp", 50.0)),
            trailing_drawdown_pct=float(defaults.get("--trailing-drawdown-pct", 30.0)),
            convergence_pp=float(defaults.get("--convergence-pp", 5.0)),
            bid_slippage_pct=slippage_pct,
            fee_rate=fee_rate,
        )
        baseline_results = backtest.grid_search(replay_data, [baseline_params])
        baseline = baseline_results[0] if baseline_results else None
        alt_results = backtest.grid_search(
            replay_data,
            backtest.default_param_grid(
                bid_slippage_pct=slippage_pct, fee_rate=fee_rate))
        backtest_results = {
            "n_trades_replayed": len(replay_data),
            "current_baseline": baseline,
            "top_alternatives": alt_results[:10],
            "friction": {"slippage_pct": slippage_pct, "fee_rate": fee_rate},
        }

        # Count trades analyzed
        n_trades = conn.execute(
            "SELECT COUNT(*) FROM entries WHERE ts >= ? AND status IN "
            "('EXECUTED', 'FAST_PATH')",
            (since_iso,),
        ).fetchone()[0]

        # Advisor v6: judge accuracy + hallucination signals
        judge_accuracy = compute_judge_accuracy(conn, since_iso)
        divergent_judge_samples = helpers.compute_divergent_judge_samples(
            conn, since_iso, limit=10)

        # Advisor v8: discovery_meta cohort breakdown (per-station,
        # per-mae-bucket, per-multi-source, per-om-penalty, per-bias)
        # so the LLM can recommend tuning specific features.
        discovery_meta = compute_discovery_meta_breakdown(conn, since_iso)

        # Advisor v9: ladder formation funnel, ladder-vs-orphan P&L,
        # 6-12h TTR cohort performance, Kelly distribution. Lets the
        # LLM evaluate whether laddering is paying off and whether the
        # per-mode floors (--ladder-min-leg-edge-pp, --ladder-min-ttr-
        # hours, --ladder-min-leg-price) need tuning.
        ladder_breakdown = compute_ladder_breakdown(conn, since_iso)

        # Advisor v10: per-loss forensic reconstruction. For every losing
        # trade in the window, pulls realized weather from Open-Meteo
        # archive + Visual Crossing + Polymarket observed_value, plus the
        # full forecast trajectory from forecast_history between entry
        # and resolution. The advisor uses this to classify each loss as
        # (a) forecast was always wrong, (b) forecast turned mid-flight
        # and we should have exited, or (c) we exited too early on a
        # forecast that ended up correct.
        loss_forensics = helpers.compute_loss_forensics(conn, since_iso)

        user_payload = {
            "since_iso": since_iso,
            "n_trades_analyzed": n_trades,
            "min_trades_for_rec": MIN_TRADES_FOR_REC,
            "analyzer_report_md": analyzer_md,
            "extras": extras,
            "current_config": current_config,
            # Advisor v2: per-trade payload
            "per_trade_sample": per_trade_sample,
            "per_trade_count_returned": len(per_trade_sample),
            "per_trade_limit_applied": args.per_trade_limit,
            "strategy_breakdown_precomputed": strategy_breakdown,
            "winner_loser_patterns_precomputed": winner_loser,
            # Advisor v3: backtest grid-search results
            "backtest_results": backtest_results,
            # Advisor v6: judge accuracy + divergent rationale samples
            "judge_accuracy": judge_accuracy,
            "divergent_judge_samples": divergent_judge_samples,
            # Advisor v8: per-cohort win rate breakdown by discovery meta
            # (station, mae_dynamic ratio, multi_source on/off, OM penalty,
            # bias applied) + discovery_skips breakdown
            "discovery_meta_breakdown": discovery_meta,
            # Advisor v9: 3-bin laddering formation, ladder-vs-single P&L,
            # TTR cohort performance (does the 6-12h band pay off?),
            # Kelly stake distribution by position (central/below/above).
            "ladder_breakdown": ladder_breakdown,
            # Advisor v10: per-loss forensic reconstruction (realized
            # weather from Open-Meteo archive + VC + Polymarket observed,
            # plus forecast trajectory from entry to resolution).
            "loss_forensics": loss_forensics,
            "loss_forensics_count": len(loss_forensics),
        }

        if args.dry_run:
            print("=" * 70)
            print("DRY RUN — payload that would be sent to Claude:")
            print("=" * 70)
            print(json.dumps(user_payload, indent=2, default=str,
                             ensure_ascii=False)[:4000])
            print("\n... (truncated to 4000 chars for preview)")
            log_event("advisor_dry_run", {"n_trades": n_trades})
            return 0

        # Call LLM (or mock)
        if args.mock_llm:
            response = _mock_llm_response(n_trades)
        else:
            if not PROMPT_PATH.exists():
                log_event("error", {"where": "load_prompt",
                                    "err": f"missing {PROMPT_PATH}"},
                          level="ERROR")
                return 2
            system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
            response = call_advisor_llm(system_prompt, user_payload,
                                         model=model, budget_usd=budget_usd)
            if response is None:
                # Surface the ACTUAL failure (set by call_advisor_llm) so the
                # dashboard's "View" button shows a usable diagnostic instead
                # of the old generic "API call failed; see weather_edge.jsonl".
                llm_err = (getattr(call_advisor_llm, "last_error", None)
                           or "API call failed; see weather_edge.jsonl")
                db.insert_advisor_run(
                    conn, ts=_now_iso(), trigger=args.trigger,
                    since_iso=since_iso, report_path="", json_path="",
                    n_suggestions=0, llm_model=model, status="error",
                    error_msg=llm_err,
                )
                if args.job_id is not None:
                    db.update_advisor_job(
                        conn, args.job_id, status="failed",
                        ts_finished=_now_iso(), exit_code=3,
                        error_msg=llm_err,
                    )
                conn.commit()
                return 3

        meta = response.pop("_meta", {})
        # Persist
        md_path, json_path = helpers.write_advisor_report(
            response, since_iso, analyzer_md)
        new_run_id = db.insert_advisor_run(
            conn, ts=_now_iso(), trigger=args.trigger,
            since_iso=since_iso, report_path=str(md_path),
            json_path=str(json_path),
            n_suggestions=len(response.get("suggestions", [])),
            llm_model=meta.get("model", model),
            cost_usd=meta.get("cost_usd"),
            tokens_in=meta.get("tokens_in"),
            tokens_out=meta.get("tokens_out"),
            cache_read_tokens=meta.get("cache_read_tokens"),
            status="ok",
        )
        # If invoked with --job-id (from the dashboard's "Run Advisor Now"),
        # close the job row by linking the resulting_run_id and flipping
        # status to 'done'.
        if args.job_id is not None:
            db.update_advisor_job(
                conn, args.job_id,
                status="done",
                ts_finished=_now_iso(),
                resulting_run_id=new_run_id,
                exit_code=0,
            )
        conn.commit()

        log_event("advisor_completed", {
            "n_suggestions": len(response.get("suggestions", [])),
            "cost_usd": meta.get("cost_usd"),
            "report_path": str(md_path),
        })
        print(f"Wrote {md_path}")
        print(f"Wrote {json_path}")
        print(f"Suggestions: {len(response.get('suggestions', []))}; "
              f"cost: ${meta.get('cost_usd', 0):.4f}")

    return 0


def _handle_sig(signum, frame):
    log_event("advisor_signal", {"signal": signum})
    sys.exit(130)


def _record_collection_failure(args, model: str, err: Exception) -> int:
    """Persist a status=error advisor_runs row when data collection crashed
    BEFORE the LLM call (e.g. AttributeError in compute_judge_accuracy,
    OperationalError on a missing schema column). Without this, an exception
    here just propagates and the dashboard shows no advisor_runs row at all
    — or a status=error row with no diagnostic.
    """
    import traceback as _tb
    tb = _tb.format_exc()
    err_msg = f"{type(err).__name__}: {err}"[:500]
    log_event("error", {"where": "advisor_collection",
                        "err": err_msg, "traceback": tb[:2000]},
              level="ERROR")
    try:
        since_iso = (datetime.now(timezone.utc)
                     - timedelta(days=args.since_days)).isoformat()
        with db.connect() as conn:
            db.insert_advisor_run(
                conn, ts=_now_iso(), trigger=args.trigger,
                since_iso=since_iso, report_path="", json_path="",
                n_suggestions=0, llm_model=model, status="error",
                error_msg=f"collection failed: {err_msg}",
            )
            if args.job_id is not None:
                db.update_advisor_job(
                    conn, args.job_id, status="failed",
                    ts_finished=_now_iso(), exit_code=4,
                    error_msg=f"collection failed: {err_msg}",
                )
            conn.commit()
    except Exception as inner:
        log_event("error", {"where": "record_collection_failure",
                            "err": f"{type(inner).__name__}: {inner}"},
                  level="ERROR")
    return 4


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)
    # Pre-parse just enough to know which job_id / model / since_days to use
    # in the failure-record path (full argparse runs inside main()).
    import argparse as _ap
    _pre = _ap.ArgumentParser(add_help=False)
    _pre.add_argument("--since-days", type=int, default=30)
    _pre.add_argument("--trigger", default="cli")
    _pre.add_argument("--job-id", type=int, default=None)
    _pre.add_argument("--model", default=None)
    _pre_args, _ = _pre.parse_known_args()
    _pre_args.model = _pre_args.model or DEFAULT_MODEL
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as _e:
        sys.exit(_record_collection_failure(_pre_args,
                                              _pre_args.model, _e))
