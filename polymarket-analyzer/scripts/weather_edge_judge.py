#!/usr/bin/env python3
"""Weather edge judge — Claude API daemon that reviews bot proposals.

Polls weather_edge.db for entries with status=PROPOSED, gathers additional
forecast sources (NWS, Visual Crossing, web search), calls Claude with the
versioned judge prompt + structured output schema, and records APPROVE /
REJECT / ADJUST verdict back to the DB.

Default model: claude-sonnet-4-6 (good cost/perf for 24/7 with prompt caching).
Override via CLAUDE_JUDGE_MODEL env var.

Daily budget cap: JUDGE_DAILY_BUDGET_USD (default $5). When exceeded, judge
marks remaining proposals as SKIPPED with reason=judge_budget_exceeded.

Required env vars:
  ANTHROPIC_API_KEY
  VISUAL_CROSSING_API_KEY  (free tier: visualcrossing.com)
  NWS_USER_AGENT           (per NWS policy: "<app> <contact email>")

Optional:
  CLAUDE_JUDGE_MODEL       (default claude-sonnet-4-6)
  JUDGE_POLL_INTERVAL_SEC  (default 120)
  JUDGE_DAILY_BUDGET_USD   (default 5)
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "polymarket-analyzer" / "scripts"))


def _load_dotenv() -> None:
    """Minimal .env loader. Reads agent/.env if present, OS env wins."""
    env_path = REPO_ROOT / "agent" / ".env"
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception:
        pass


_load_dotenv()

import weather_edge_db as db  # noqa: E402

LOG_DIR = Path.home() / ".polymarket-paper"
LOG_FILE = LOG_DIR / "weather_edge.jsonl"
PROMPT_PATH = REPO_ROOT / "polymarket-analyzer" / "references" / "weather-judge-prompt.md"

DEFAULT_MODEL = os.environ.get("CLAUDE_JUDGE_MODEL", "claude-sonnet-4-6")
POLL_INTERVAL = int(os.environ.get("JUDGE_POLL_INTERVAL_SEC", "120"))
DAILY_BUDGET_USD = float(os.environ.get("JUDGE_DAILY_BUDGET_USD", "5.0"))

# Pricing per 1M tokens (Sonnet 4.6 / Opus 4.7 — adjust if model changed)
PRICING = {
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00, "cache_read": 0.30},
    "claude-opus-4-7": {"input": 5.00, "output": 25.00, "cache_read": 0.50},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00, "cache_read": 0.10},
}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_event(event_type: str, payload: dict | None = None, level: str = "INFO") -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    rec = {"ts": _now_iso(), "level": level, "event_type": event_type,
           "payload": payload or {}, "actor": "judge"}
    line = json.dumps(rec, default=str, ensure_ascii=False)
    try:
        with LOG_FILE.open("a") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"[log-error] {e}", file=sys.stderr, flush=True)
    print(line, flush=True)


# ---------------------------------------------------------------------------
# External weather sources (judge tools)
# ---------------------------------------------------------------------------


def get_nws_forecast(lat: float, lon: float) -> Optional[dict]:
    """NWS API (US only). Returns parsed forecast or None."""
    ua = os.environ.get("NWS_USER_AGENT", "polymarket-skills weather-edge-bot")
    headers = {"User-Agent": ua, "Accept": "application/geo+json"}
    try:
        # 1. Find the gridpoint
        r = requests.get(f"https://api.weather.gov/points/{lat},{lon}",
                         headers=headers, timeout=10)
        if r.status_code != 200:
            return None
        forecast_url = r.json()["properties"]["forecast"]
        # 2. Get forecast
        r2 = requests.get(forecast_url, headers=headers, timeout=10)
        if r2.status_code != 200:
            return None
        periods = r2.json()["properties"]["periods"]
        # Return next 4 periods (today + tomorrow ~12h chunks)
        return {"periods": periods[:4]}
    except Exception:
        return None


def get_visual_crossing(city: str, date_iso: Optional[str] = None) -> Optional[dict]:
    """Visual Crossing API. date_iso = YYYY-MM-DD or None for today."""
    api_key = os.environ.get("VISUAL_CROSSING_API_KEY")
    if not api_key:
        return None
    base = "https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline"
    url = f"{base}/{city}/{date_iso}" if date_iso else f"{base}/{city}"
    try:
        r = requests.get(url, params={"key": api_key, "unitGroup": "us",
                                       "include": "days", "elements":
                                       "tempmax,tempmin,precip,precipprob,conditions"},
                         timeout=15)
        if r.status_code != 200:
            return None
        data = r.json()
        # Return just the days array
        return {"days": data.get("days", [])[:5]}
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Anthropic API call
# ---------------------------------------------------------------------------


def _load_system_prompt() -> str:
    if not PROMPT_PATH.exists():
        return "You are a weather forecast verification assistant. Respond with JSON."
    return PROMPT_PATH.read_text()


_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["APPROVE", "REJECT", "ADJUST"]},
        "confidence": {"type": "number"},
        "judge_prob": {"type": "number"},
        "rationale": {"type": "string"},
        # evidence_summary is a JSON-serialized string of arbitrary
        # evidence the judge gathered (forecast deltas, ECMWF/NWS
        # comparisons, web search hits). String form avoids the
        # 'additionalProperties: true' rejection from Anthropic API.
        "evidence_summary": {"type": "string"},
        # adjusted_side is "YES"/"NO" when verdict=ADJUST, null otherwise.
        # Using anyOf instead of type-array+enum because Anthropic's schema
        # validator does not accept enum values mixed with a nullable type.
        "adjusted_side": {
            "anyOf": [
                {"type": "string", "enum": ["YES", "NO"]},
                {"type": "null"},
            ],
        },
        "adjusted_size_usd": {"type": ["number", "null"]},
    },
    "required": ["verdict", "confidence", "judge_prob", "rationale", "evidence_summary"],
    "additionalProperties": False,
}


def call_claude(entry_row: dict, evidence: dict, system_prompt: str) -> Optional[dict]:
    """Call Claude API to render a verdict. Returns parsed JSON or None on error."""
    try:
        import anthropic
    except ImportError:
        log_event("error", {"where": "call_claude", "err": "anthropic SDK not installed"},
                  level="ERROR")
        return None

    client = anthropic.Anthropic()  # uses ANTHROPIC_API_KEY

    # Build user content: market context + evidence
    user_content = json.dumps({
        "market_question": entry_row["market_question"],
        "market_slug": entry_row["market_slug"],
        "city": entry_row["city_resolved"],
        "threshold_value": entry_row["threshold_value"],
        "threshold_unit": entry_row["threshold_unit"],
        "comparison": entry_row["comparison"],
        "end_date": entry_row["end_date"],
        "ttr_hours": entry_row["ttr_hours_at_entry"],
        "bot_proposal": {
            "side": entry_row["side"],
            "entry_price": entry_row["entry_price"],
            "forecast_prob": entry_row["forecast_prob_at_entry"],
            "implied_prob": entry_row["implied_prob_at_entry"],
            "edge_pp": entry_row["edge_pp_at_entry"],
            "openweather_forecast": json.loads(entry_row["forecast_snapshot_json"] or "{}"),
        },
        "additional_evidence": evidence,
    }, indent=2, ensure_ascii=False)

    t0 = time.monotonic()
    try:
        response = client.messages.create(
            model=DEFAULT_MODEL,
            max_tokens=2048,
            system=[{"type": "text", "text": system_prompt,
                     "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
            messages=[{"role": "user", "content": user_content}],
            thinking={"type": "adaptive"},
            output_config={
                "effort": "medium",
                "format": {"type": "json_schema", "schema": _VERDICT_SCHEMA},
            },
        )
        duration_ms = int((time.monotonic() - t0) * 1000)
    except Exception as e:
        log_event("error", {"where": "anthropic_call", "err": str(e),
                            "type": type(e).__name__}, level="ERROR")
        return None

    # Parse the structured JSON response
    text_block = next((b.text for b in response.content if b.type == "text"), None)
    if not text_block:
        log_event("error", {"where": "claude_response_empty",
                            "stop_reason": response.stop_reason}, level="ERROR")
        return None
    try:
        parsed = json.loads(text_block)
    except json.JSONDecodeError as e:
        log_event("error", {"where": "verdict_json_decode", "err": str(e),
                            "text": text_block[:500]}, level="ERROR")
        return None

    # Compute cost
    usage = response.usage
    pricing = PRICING.get(DEFAULT_MODEL, PRICING["claude-sonnet-4-6"])
    cost = (
        usage.input_tokens * pricing["input"] / 1_000_000 +
        usage.output_tokens * pricing["output"] / 1_000_000 +
        (usage.cache_read_input_tokens or 0) * pricing["cache_read"] / 1_000_000
    )

    parsed["_meta"] = {
        "tokens_in": usage.input_tokens,
        "tokens_out": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_input_tokens or 0,
        "cost_usd": cost,
        "duration_ms": duration_ms,
        "model": DEFAULT_MODEL,
    }
    return parsed


# ---------------------------------------------------------------------------
# Per-proposal review
# ---------------------------------------------------------------------------


# OpenWeather geocode lookup (we get lat/lon from the bot's stored forecast if available,
# else hit OpenWeather's geocoding API. Cheap.)
def _geocode_city(city: str) -> Optional[tuple[float, float]]:
    api_key = os.environ.get("OPENWEATHER_API_KEY")
    if not api_key:
        return None
    try:
        r = requests.get("https://api.openweathermap.org/geo/1.0/direct",
                         params={"q": city, "limit": 1, "appid": api_key},
                         timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        if not data:
            return None
        return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception:
        return None


def review_proposal(entry: dict, system_prompt: str) -> Optional[dict]:
    """Gather evidence + call Claude + return verdict dict (with _meta)."""
    city = entry["city_resolved"] or ""
    target_date = (entry["end_date"] or "")[:10]  # YYYY-MM-DD

    evidence: dict[str, Any] = {}

    # NWS (US only)
    coords = _geocode_city(city) if city else None
    if coords:
        nws = get_nws_forecast(*coords)
        if nws:
            evidence["nws"] = nws

    # Visual Crossing
    vc = get_visual_crossing(city, target_date) if city else None
    if vc:
        evidence["visual_crossing"] = vc

    if not evidence:
        log_event("judge_no_evidence", {"entry_id": entry["entry_id"],
                                         "city": city}, level="WARN")
        # Proceed anyway; Claude can fall back to OpenWeather data alone.
        evidence["note"] = "All external sources unavailable"

    return call_claude(entry, evidence, system_prompt)


def apply_verdict(conn, entry_row, verdict: dict) -> None:
    """Persist the verdict to judge_reviews and update entry status."""
    meta = verdict.pop("_meta", {})
    bot_prob = float(entry_row["forecast_prob_at_entry"] or 0)
    judge_prob = float(verdict["judge_prob"])

    db.insert_judge_review(
        conn,
        entry_id=entry_row["entry_id"],
        ts=_now_iso(),
        verdict=verdict["verdict"],
        confidence=verdict["confidence"],
        judge_prob=judge_prob,
        bot_prob=bot_prob,
        prob_delta=judge_prob - bot_prob,
        rationale=verdict["rationale"][:1500],
        evidence_json=verdict.get("evidence_summary", {}),
        adjusted_side=verdict.get("adjusted_side"),
        adjusted_size_usd=verdict.get("adjusted_size_usd"),
        llm_model=meta.get("model"),
        tokens_in=meta.get("tokens_in"),
        tokens_out=meta.get("tokens_out"),
        cache_read_tokens=meta.get("cache_read_tokens"),
        cost_usd=meta.get("cost_usd"),
        duration_ms=meta.get("duration_ms"),
    )

    new_status = {"APPROVE": "APPROVED",
                  "REJECT": "REJECTED",
                  "ADJUST": "ADJUSTED"}.get(verdict["verdict"], "REJECTED")
    db.update_entry_status(conn, entry_row["entry_id"], new_status)


# ---------------------------------------------------------------------------
# Daily budget tracker
# ---------------------------------------------------------------------------


_today_spend = {"date": None, "usd": 0.0}


def _spent_today() -> float:
    today = datetime.now(timezone.utc).date().isoformat()
    if _today_spend["date"] != today:
        _today_spend["date"] = today
        _today_spend["usd"] = 0.0
    return _today_spend["usd"]


def _record_spend(amount: float) -> None:
    _spent_today()  # rolls date if needed
    _today_spend["usd"] += amount


# ---------------------------------------------------------------------------
# Daemon loop
# ---------------------------------------------------------------------------


_shutdown = False


def _handle_sig(signum, frame):
    global _shutdown
    log_event("shutdown_signal", {"signal": signum})
    _shutdown = True


def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log_event("error", {"where": "startup", "err": "ANTHROPIC_API_KEY missing"},
                  level="ERROR")
        sys.exit(1)

    signal.signal(signal.SIGTERM, _handle_sig)
    signal.signal(signal.SIGINT, _handle_sig)

    db.init_db()
    system_prompt = _load_system_prompt()
    log_event("judge_startup", {"model": DEFAULT_MODEL,
                                 "poll_sec": POLL_INTERVAL,
                                 "daily_budget_usd": DAILY_BUDGET_USD,
                                 "system_prompt_chars": len(system_prompt)})

    while not _shutdown:
        try:
            spent = _spent_today()
            if spent >= DAILY_BUDGET_USD:
                log_event("judge_budget_exceeded", {"spent_usd": spent,
                                                     "cap": DAILY_BUDGET_USD},
                          level="WARN")
                # Skip remaining proposals for the day
                with db.connect() as conn:
                    rows = db.query_pending_proposals(conn)
                    for r in rows:
                        db.update_entry_status(conn, r["entry_id"], "SKIPPED",
                                               judge_skipped_reason="judge_budget_exceeded",
                                               skip_reason="judge_budget_exceeded")
                time.sleep(POLL_INTERVAL)
                continue

            with db.connect() as conn:
                rows = db.query_pending_proposals(conn, limit=10)

            if not rows:
                time.sleep(POLL_INTERVAL)
                continue

            log_event("judge_polling", {"pending": len(rows),
                                         "spent_today_usd": round(spent, 2)})

            for row in rows:
                if _shutdown:
                    break
                row_dict = dict(row)
                t0 = time.monotonic()
                verdict = review_proposal(row_dict, system_prompt)
                duration_s = time.monotonic() - t0

                if not verdict:
                    log_event("judge_failed", {"entry_id": row_dict["entry_id"]},
                              level="WARN")
                    with db.connect() as conn:
                        db.update_entry_status(conn, row_dict["entry_id"], "SKIPPED",
                                                judge_skipped_reason="judge_unavailable",
                                                skip_reason="judge_unavailable")
                    continue

                cost = verdict.get("_meta", {}).get("cost_usd", 0)
                _record_spend(cost)

                with db.connect() as conn:
                    apply_verdict(conn, row_dict, verdict)

                log_event("judge_verdict", {
                    "entry_id": row_dict["entry_id"],
                    "verdict": verdict["verdict"],
                    "confidence": verdict["confidence"],
                    "judge_prob": verdict["judge_prob"],
                    "bot_prob": float(row_dict["forecast_prob_at_entry"] or 0),
                    "cost_usd": round(cost, 4),
                    "duration_s": round(duration_s, 1),
                })

                # Throttle between API calls
                if not _shutdown:
                    time.sleep(2)

        except Exception as e:
            log_event("error", {"where": "judge_loop", "err": str(e),
                                "type": type(e).__name__}, level="ERROR")
            time.sleep(POLL_INTERVAL)

        time.sleep(POLL_INTERVAL)

    log_event("judge_shutdown_clean", {})


if __name__ == "__main__":
    main()
