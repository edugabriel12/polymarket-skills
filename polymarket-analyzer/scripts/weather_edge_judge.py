#!/usr/bin/env python3
"""Weather edge judge — Claude API daemon that reviews bot proposals.

Polls weather_edge.db for entries with status=PROPOSED, gathers additional
forecast sources (NWS, Visual Crossing, web search), calls Claude with the
versioned judge prompt + structured output schema, and records APPROVE /
REJECT / ADJUST verdict back to the DB.

Default model: claude-sonnet-5 for both the full judge (CLAUDE_JUDGE_MODEL) and
the web-search anomaly scan (JUDGE_ANOMALY_SCAN_MODEL). The v13.2 conditional
gate (option B) plus the option-1 anomaly scan mean the full judge runs on only
the genuinely hard, low-volume cases. Point JUDGE_ANOMALY_SCAN_MODEL at a
cheaper model (e.g. claude-haiku-4-5) if you want the gating scan to cost less
than the escalated full-judge calls.

Daily budget cap: JUDGE_DAILY_BUDGET_USD (default $15). When exceeded, judge
marks remaining proposals as SKIPPED with reason=judge_budget_exceeded.

v13.2 (option B, conditional gate): the LLM is no longer a universal per-trade
gate. `_judge_route()` resolves the decisive cases WITHOUT an LLM call —
deterministic threshold/range proximity coin-flips are AUTO-REJECTED, and
tight-ensemble bets whose bin sits ≥ AUTOAPPROVE_MAE_MULT×σ away are
AUTO-APPROVED. Only the genuinely uncertain cases (non-ensemble single-source
fallback, ensemble with the bin near the forecast) reach the LLM, where its
independent cross-check earns its cost. Set JUDGE_AUTOROUTE=0 to restore the
universal-LLM behavior. Every routed decision still flows through
apply_verdict(), so Rule 6 / proximity / range-calibration guards all still run.

v14 (2026-07-05, temperature-only policy): non-temp proposals (rain/snow
binaries, numeric precip) are deterministically AUTO-REJECTED before any
routing — fail-closed, active even with JUDGE_AUTOROUTE=0. The bot prices
those off an uncalibrated clipped POP, so they must never reach a gate that
could approve them (see weather_edge_helpers.is_tradeable_spec).

v13.2 option-1 (anomaly scan): the ensemble prices in synoptic weather but is
blind to NON-meteorological catalysts (breaking news, resolution ambiguity).
So an auto-approve isn't a blind pass — it first runs a CHEAP web-search-only
scan (_anomaly_scan, no NWS/VC, no full judge reasoning) asking "is there an
active catalyst for this city/date?". Clean → auto-approve (full judge skipped);
catalyst found or scan unavailable → escalate to the full judge (fail-safe).
Set JUDGE_ANOMALY_SCAN=0 to auto-approve directly without the scan.

Required env vars:
  ANTHROPIC_API_KEY
  VISUAL_CROSSING_API_KEY  (free tier: visualcrossing.com)
  NWS_USER_AGENT           (per NWS policy: "<app> <contact email>")

Optional:
  CLAUDE_JUDGE_MODEL         (default claude-sonnet-5)
  JUDGE_ANOMALY_SCAN_MODEL   (default claude-sonnet-5)
  JUDGE_POLL_INTERVAL_SEC    (default 120)
  JUDGE_DAILY_BUDGET_USD     (default 15)
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

# Force UTF-8 stdout/stderr so unicode chars in weather data (°C, ≈, etc)
# don't crash the print() during JSONL log writes on Windows (cp1252).
# Without this, the verdict's rationale string can be cut mid-character,
# corrupting the JSON output and causing silent REJECT verdicts.
# See log analysis 2026-05-15 (charmap codec errors at lines 5:50, 7:53).
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass  # older Python or non-tty stream

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

DEFAULT_MODEL = os.environ.get("CLAUDE_JUDGE_MODEL", "claude-sonnet-5")
# The anomaly scan (v13.2 option-1) runs on its own model — see
# JUDGE_ANOMALY_SCAN_MODEL. It can be pointed at a cheaper model than the full
# judge to keep gating cheap, but currently tracks the judge at Sonnet 5.
ANOMALY_SCAN_MODEL = os.environ.get("JUDGE_ANOMALY_SCAN_MODEL", "claude-sonnet-5")
POLL_INTERVAL = int(os.environ.get("JUDGE_POLL_INTERVAL_SEC", "120"))
DAILY_BUDGET_USD = float(os.environ.get("JUDGE_DAILY_BUDGET_USD", "15.0"))
# Pre-judge edge re-check threshold (pp). If the entry's current
# top-of-book edge has decayed below this floor between proposal and
# judge pickup, skip the LLM call entirely (saves ~$0.04/skip).
# Default 15.0 sits between the bot's --min-edge-pp 20 (discovery floor)
# and --execute-min-edge-pp 8 (execution floor) — aggressive enough to
# bound LLM cost when the market clearly moved, loose enough to let
# small-decay proposals reach the judge. Set to a large negative
# number (e.g. -100) to disable the recheck.
PREJUDGE_MIN_EDGE_PP = float(os.environ.get("JUDGE_PREJUDGE_MIN_EDGE_PP", "15.0"))

# v15.1 (post-mortem 2026-07-07): LADDER legs get their own, LOWER floor.
# Discovery deliberately proposes ladder legs down to --ladder-min-leg-edge-pp
# (default 10 — adjacent legs are hedges with structurally smaller edges),
# but the single 15pp floor above made every leg born in [10, 15) dead on
# arrival: skipped as "edge_decay" even when its edge IMPROVED, killing the
# whole group under strict atomicity. Incident data: 156/167 (93%) of
# edge_decay_prejudge skips were such DOA legs; only 11/167 ladder groups
# ever executed. Default 10.0 mirrors the bot's --ladder-min-leg-edge-pp
# (separate processes — keep the two in sync when tuning either).
PREJUDGE_MIN_EDGE_PP_LADDER = float(os.environ.get(
    "JUDGE_PREJUDGE_MIN_EDGE_PP_LADDER", "10.0"))

# v12 (advisor sug_003/004, 2026-06-01): range-market calibration caps.
# On Polymarket 'range' markets the bin is ~1°C wide, so directional
# consensus (forecast below the bin) is necessary but NOT sufficient —
# both bin edges sit within ~2×MAE of the forecast. The judge's 0.8-0.9
# confidence bucket won only 17% on these. Hard-cap the stored judge_prob
# and force conservative ADJUST sizing on the marginal ones.
RANGE_PROB_CAP = float(os.environ.get("JUDGE_RANGE_PROB_CAP", "0.70"))
RANGE_PROB_CAP_NEAR = float(os.environ.get("JUDGE_RANGE_PROB_CAP_NEAR", "0.65"))
RANGE_NEAR_MAE_MULT = float(os.environ.get("JUDGE_RANGE_NEAR_MAE_MULT", "2.0"))
RANGE_ADJUST_SIZE_USD = float(os.environ.get("JUDGE_RANGE_ADJUST_SIZE_USD", "20.0"))

# v13.1 (2026-06-15): Rule-6 divergence on an ADJUST verdict no longer hard-
# REJECTs. The judge emitting ADJUST means "same side, less conviction —
# size down", not "kill". When such an ADJUST diverges from the bot by
# >20pp, we keep ADJUST but cap the stake to this tiny size (the judge's
# conviction, not the bot's, governs exposure). APPROVE that diverges still
# hard-REJECTs (the bot wanted full size and the judge fundamentally
# disagrees). A trade the judge itself sees as -EV (judge_prob <= the side's
# price) is still REJECTed — never trade without a quantifiable edge.
RULE6_DOWNSIZE_USD = float(os.environ.get("JUDGE_RULE6_DOWNSIZE_USD", "10.0"))

# v13.2 (option B, 2026-07-01): CONDITIONAL judge gating. The LLM judge was a
# universal per-trade gate, but forensics showed it is a *worse* forecaster
# than the calibrated 3-model ensemble it second-guesses (judge Brier 0.41,
# FPR 68%, the 0.8-0.9 judge bucket won only 17% on ranges) — and ~$5-9/day
# was being spent mostly to REJECT trades the deterministic guards already
# handle. So we fire the LLM ONLY where the cheap ensemble is weak or blind:
#   - non-ensemble (single-source fallback) entries,
#   - ensemble entries where the bin is NEAR the forecast (< mult × sigma).
# (v14: non-temp markets — rain/snow — no longer go to the LLM: they are
# deterministically auto-rejected by the temperature-only policy, which
# runs before this gate and ignores JUDGE_AUTOROUTE.)
# Tight-ensemble, far-from-bin bets are AUTO-APPROVED on the code guards
# (no LLM spend); deterministic threshold-proximity coin-flips are
# AUTO-REJECTED (no LLM spend). Every routed decision still flows through
# apply_verdict(), so Rule 6 / proximity / range-calibration all still run.
# Set JUDGE_AUTOROUTE=0 to restore the universal-LLM behavior.
JUDGE_AUTOROUTE = os.environ.get("JUDGE_AUTOROUTE", "1").strip().lower() not in (
    "0", "false", "no", "off", "")
# Bin must sit at least this many ensemble-sigmas from the forecast to skip
# the LLM. Kept == RANGE_NEAR_MAE_MULT so an auto-approved case is, by
# construction, never one the range calibration would flag as "near".
AUTOAPPROVE_MAE_MULT = float(os.environ.get("JUDGE_AUTOAPPROVE_MAE_MULT", "2.0"))

# v13.2 option-1 (2026-07-01): a CHEAP web-search-only catalyst scan on the
# cases the auto-router would otherwise APPROVE with no LLM at all. The
# calibrated ensemble already prices in synoptic weather (fronts, heat domes) —
# but it is blind to NON-meteorological anomalies (breaking news after the
# model run, resolution-source ambiguity, event-day stories). This scan runs
# ONE narrow LLM call with web search only (no NWS/VC, no full judge reasoning)
# asking "is there an active catalyst for this city/date?". Clean → auto-approve
# as before; catalyst found (or scan unavailable) → escalate to the full judge.
# Set JUDGE_ANOMALY_SCAN=0 to auto-approve directly (pure v13.2 behavior).
JUDGE_ANOMALY_SCAN = os.environ.get("JUDGE_ANOMALY_SCAN", "1").strip().lower() not in (
    "0", "false", "no", "off", "")
ANOMALY_SCAN_MAX_USES = int(os.environ.get("JUDGE_ANOMALY_SCAN_MAX_USES", "3"))
ANOMALY_SCAN_MAX_TOKENS = int(os.environ.get("JUDGE_ANOMALY_SCAN_MAX_TOKENS", "1500"))

# Pricing per 1M tokens (adjust if model changed)
PRICING = {
    "claude-opus-4-8": {"input": 5.00, "output": 25.00, "cache_read": 0.50},
    "claude-sonnet-5": {"input": 3.00, "output": 15.00, "cache_read": 0.30},
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


def get_brightsky_forecast(lat: float, lon: float,
                           target_date: str) -> Optional[dict]:
    """DWD MOSMIX point forecast for `target_date` via the free Bright Sky
    JSON API (no key). Bright Sky serves DWD's MOSMIX model (a per-station
    MOS product combining ICON + ECMWF IFS) for ~5400 stations, most in
    Europe — the independent corroboration source the judge lacks in Europe
    (get_nws_forecast is US-only: api.weather.gov 404s abroad -> None).

    Returns {"source_station", "distance_m", "max_c", "min_c", "n_hours"} for
    the target day, or None on any non-200 / empty / exception (fail-open:
    the judge then proceeds on Visual Crossing + the bot's ensemble, exactly
    as it does when NWS returns None). Never raises.

    Mirrors get_nws_forecast's contract so review_proposal can add it to the
    evidence dict with no country branch.
    """
    base = os.environ.get("BRIGHTSKY_BASE_URL", "https://api.brightsky.dev")
    ua = os.environ.get("BRIGHTSKY_USER_AGENT",
                        os.environ.get("NWS_USER_AGENT",
                                       "polymarket-skills weather-edge-bot"))
    try:
        r = requests.get(
            f"{base}/weather",
            params={"lat": lat, "lon": lon,
                    "date": target_date, "last_date": target_date,
                    "units": "dwd"},
            headers={"User-Agent": ua, "Accept": "application/json"},
            timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        temps = [row.get("temperature") for row in (data.get("weather") or [])
                 if row.get("temperature") is not None]
        if not temps:
            return None
        sources = data.get("sources") or []
        src = sources[0] if sources else {}
        return {
            "provider": "dwd_mosmix",
            "source_station": src.get("station_name") or src.get("wmo_station_id"),
            "distance_m": src.get("distance"),
            "max_c": round(max(temps), 2),
            "min_c": round(min(temps), 2),
            "n_hours": len(temps),
        }
    except Exception:
        return None


# v16: city -> IPMA globalIdLocal (Portugal daily-forecast endpoint). Small
# curated map for the two Portuguese cities the bot trades; extend as needed
# from https://api.ipma.pt/open-data/distrits-islands.json
_IPMA_GLOBAL_ID = {
    "Lisbon": 1110600, "Lisboa": 1110600,
    "Porto": 1131200,
}


def get_ipma_forecast(city: str, target_date: str) -> Optional[dict]:
    """IPMA (Portugal national service) daily city forecast for `target_date`.

    Free, no API key, JSON. IPMA blends ECMWF + AROME with statistical
    (MOS-style) post-processing, so for Lisbon/Porto it is a genuinely
    independent, MOS-quality corroboration source — the European analogue of
    NWS for Portugal (where Bright Sky/MOSMIX may be thin and met.no is raw
    ECMWF). Daily granularity only (tMin/tMax), which matches the °C
    highest/lowest-temperature markets.

    Returns {"provider":"ipma","max_c","min_c"} for the day or None (unknown
    city / non-200 / date absent / exception). Fail-open, never raises.
    """
    gid = _IPMA_GLOBAL_ID.get(city)
    if not gid:
        return None
    base = os.environ.get("IPMA_BASE_URL", "https://api.ipma.pt")
    try:
        r = requests.get(
            f"{base}/open-data/forecast/meteorology/cities/daily/{gid}.json",
            headers={"Accept": "application/json"}, timeout=10)
        if r.status_code != 200:
            return None
        days = (r.json() or {}).get("data") or []
        row = next((d for d in days
                    if str(d.get("forecastDate")) == target_date), None)
        if not row:
            return None
        tmax = row.get("tMax")
        tmin = row.get("tMin")
        if tmax is None and tmin is None:
            return None
        out = {"provider": "ipma"}
        if tmax is not None:
            out["max_c"] = round(float(tmax), 2)
        if tmin is not None:
            out["min_c"] = round(float(tmin), 2)
        return out
    except Exception:
        return None


def get_metno_forecast(lat: float, lon: float,
                       target_date: str) -> Optional[dict]:
    """MET Norway Locationforecast point forecast for `target_date`.

    Free, no key, global — BUT met.no applies per-station MOS post-processing
    only in the Nordics/Arctic; elsewhere it is raw ECMWF-HRES (so it is a
    strong INDEPENDENT corroborator only for Oslo/Stockholm/Copenhagen/
    Helsinki/Reykjavik, and correlated-with-the-ensemble noise elsewhere —
    the judge prompt says as much). Requires a custom User-Agent (missing UA
    -> HTTP 403).

    Returns {"provider":"metno","max_c","min_c","n_hours"} for the day or
    None. Fail-open, never raises.
    """
    ua = os.environ.get("METNO_USER_AGENT",
                        os.environ.get("NWS_USER_AGENT",
                                       "polymarket-skills weather-edge-bot"))
    base = os.environ.get("METNO_BASE_URL",
                          "https://api.met.no/weatherapi/locationforecast/2.0")
    try:
        r = requests.get(
            f"{base}/compact",
            params={"lat": round(float(lat), 4), "lon": round(float(lon), 4)},
            headers={"User-Agent": ua, "Accept": "application/json"},
            timeout=10)
        if r.status_code != 200:
            return None
        series = (((r.json() or {}).get("properties") or {}).get("timeseries")
                  or [])
        temps = []
        for pt in series:
            t = str(pt.get("time") or "")
            if not t.startswith(target_date):
                continue
            val = (((pt.get("data") or {}).get("instant") or {})
                   .get("details") or {}).get("air_temperature")
            if val is not None:
                temps.append(float(val))
        if not temps:
            return None
        return {"provider": "metno", "max_c": round(max(temps), 2),
                "min_c": round(min(temps), 2), "n_hours": len(temps)}
    except Exception:
        return None


def get_visual_crossing(city: str, date_iso: Optional[str] = None) -> Optional[dict]:
    """v7: thin wrapper over weather_edge_helpers.fetch_visual_crossing,
    which is now the canonical implementation (cached + shared with the
    bot's discovery loop). Kept here as `get_visual_crossing` for
    name-stability with prior judge code."""
    from weather_edge_helpers import fetch_visual_crossing
    return fetch_visual_crossing(city, date_iso)


# ---------------------------------------------------------------------------
# Anthropic API call
# ---------------------------------------------------------------------------


def _load_system_prompt() -> str:
    if not PROMPT_PATH.exists():
        return "You are a weather forecast verification assistant. Respond with JSON."
    return PROMPT_PATH.read_text(encoding="utf-8")


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

    # v13.1 (C): surface the calibrated Open-Meteo ensemble (ICON+GFS+ECMWF)
    # the bot sized with, so the judge calibrates against the SAME evidence
    # rather than applying a blanket range cap. When the ensemble members
    # agree tightly, the bot's high P(side) is well-founded and the judge
    # should not penalize it down to 0.65 out of generic range skepticism.
    ensemble_block = None
    try:
        dm = json.loads(entry_row["discovery_meta_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        dm = {}
    if dm.get("ensemble_calibrated"):
        ensemble_block = {
            "source": "Open-Meteo ICON+GFS+ECMWF (NGR-calibrated)",
            "ensemble_mean_mu": dm.get("mu"),
            "ensemble_sigma": dm.get("mae_dynamic"),
            "members": dm.get("ensemble_members"),
            "unit": entry_row["threshold_unit"],
            "note": ("This (mu, sigma) is what the bot sized with. If the "
                     "members agree tightly (small sigma), a high P(side) is "
                     "well-founded — do NOT cap your judge_prob down out of "
                     "generic range-market skepticism. Disagree only with "
                     "independent evidence (NWS/VC/news/climatology)."),
        }

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
        "ensemble_forecast": ensemble_block,
        "additional_evidence": evidence,
    }, indent=2, ensure_ascii=False)

    t0 = time.monotonic()
    # v9.6 (2026-05-22): "adaptive" thinking is only supported on Opus and
    # Sonnet families. Haiku 4.5 rejects with 400 invalid_request_error.
    # Build kwargs conditionally so the same code path works for any
    # configured model. Same caution for output_config.effort which is
    # also extended-thinking-coupled on some models.
    is_haiku = "haiku" in DEFAULT_MODEL.lower()
    request_kwargs = {
        "model": DEFAULT_MODEL,
        "max_tokens": 8192,
        "system": [{"type": "text", "text": system_prompt,
                     "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
        "messages": [{"role": "user", "content": user_content}],
        "output_config": {
            "format": {"type": "json_schema", "schema": _VERDICT_SCHEMA},
        },
    }
    if not is_haiku:
        # Opus / Sonnet: enable adaptive thinking + medium effort
        request_kwargs["thinking"] = {"type": "adaptive"}
        request_kwargs["output_config"]["effort"] = "medium"
    try:
        response = client.messages.create(**request_kwargs)
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
        # During shutdown, an in-flight response may be truncated mid-stream —
        # that's expected. Log as WARN so it doesn't pollute the error feed.
        level = "WARN" if _shutdown else "ERROR"
        log_event("error", {"where": "verdict_json_decode", "err": str(e),
                            "text": text_block[:500],
                            "shutdown_in_progress": _shutdown}, level=level)
        return None

    # Compute cost
    usage = response.usage
    pricing = PRICING.get(DEFAULT_MODEL, PRICING["claude-haiku-4-5"])
    cost = (
        usage.input_tokens * pricing["input"] / 1_000_000 +
        usage.output_tokens * pricing["output"] / 1_000_000 +
        (usage.cache_read_input_tokens or 0) * pricing["cache_read"] / 1_000_000
    )

    # Capture any thinking/reasoning blocks for the audit trail.
    thinking_blocks = []
    for b in response.content:
        if getattr(b, "type", None) == "thinking":
            thinking_blocks.append(getattr(b, "thinking", "")
                                    or getattr(b, "text", ""))

    parsed["_meta"] = {
        "tokens_in": usage.input_tokens,
        "tokens_out": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_input_tokens or 0,
        "cost_usd": cost,
        "duration_ms": duration_ms,
        "model": DEFAULT_MODEL,
        # v6: persist the full context for hallucination diagnosis.
        "input_context_json": user_content,
        "raw_response_json": json.dumps({
            "structured_output": parsed,
            "thinking_blocks": thinking_blocks,
            "stop_reason": response.stop_reason,
        }, ensure_ascii=False),
        # v10: ship the prompt text up so apply_verdict can dedup it
        # into judge_prompts and record only the sha256 on the review.
        "system_prompt_text": system_prompt,
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
        # v15.3: DWD MOSMIX via Bright Sky — the independent corroboration
        # the judge lacks OUTSIDE the US (NWS 404s abroad). Reuses the coords
        # already resolved; no country branch (returns None where MOSMIX has
        # no nearby station, so US cities simply don't get this key).
        if target_date:
            bs = get_brightsky_forecast(coords[0], coords[1], target_date)
            if bs:
                evidence["dwd_mosmix"] = bs
            # v16: met.no — MOS-quality independent source in the Nordics
            # (raw ECMWF elsewhere; the prompt tells the LLM how to weight it).
            mn = get_metno_forecast(coords[0], coords[1], target_date)
            if mn:
                evidence["metno"] = mn

    # v16: IPMA (Portugal national MOS) — independent corroboration for
    # Lisbon/Porto, where Bright Sky may be thin and met.no is raw ECMWF.
    if city and target_date:
        ipma = get_ipma_forecast(city, target_date)
        if ipma:
            evidence["ipma"] = ipma

    # Visual Crossing
    vc = get_visual_crossing(city, target_date) if city else None
    if vc:
        evidence["visual_crossing"] = vc

    # v17 Africa pilot: flag high-risk so the LLM applies extra scrutiny. These
    # cities have NO regional forecast model and resolve off real METAR (the
    # ERA5 archive is too biased to settle a 1°C tick), so the ensemble's
    # confidence is worth less here — demand independent corroboration.
    if _entry_is_pilot(entry):
        evidence["africa_pilot"] = {
            "high_risk": True,
            "note": ("Africa desert/subtropical pilot: no regional model, "
                     "resolution via real station METAR. Require independent "
                     "corroboration; prefer ADJUST (reduced size) over a bare "
                     "APPROVE when sources are thin."),
        }

    if not evidence:
        log_event("judge_no_evidence", {"entry_id": entry["entry_id"],
                                         "city": city}, level="WARN")
        # Proceed anyway; Claude can fall back to OpenWeather data alone.
        evidence["note"] = "All external sources unavailable"

    return call_claude(entry, evidence, system_prompt)


# ---------------------------------------------------------------------------
# Pre-judge edge re-check (v10) — cut LLM cost when edge decayed during wait
# ---------------------------------------------------------------------------


def _fetch_orderbook(token_id: str) -> Optional[dict]:
    """Lightweight orderbook fetch for pre-judge recheck.
    Duplicates weather_edge_bot.fetch_orderbook deliberately — keeping the
    judge daemon module-independent from the bot. Returns dict with sorted
    asks (ascending) or None on failure.
    """
    try:
        r = requests.get("https://clob.polymarket.com/book",
                         params={"token_id": token_id}, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        asks = sorted(
            [{"price": float(a["price"]), "size": float(a["size"])}
             for a in (data.get("asks") or [])],
            key=lambda a: a["price"],
        )
        return {"asks": asks}
    except Exception:
        return None


def _recheck_edge_prejudge(row: dict) -> Optional[dict]:
    """Re-check current top-of-book edge for a PROPOSED entry.

    Returns None if the entry should proceed to the LLM judge (edge still
    above threshold, orderbook unavailable, or any data missing — fail-open).
    Returns a payload dict (loggable) if the entry should be skipped
    upstream with skip_reason='edge_decay_prejudge'.

    NOTE the check is an ABSOLUTE FLOOR on the current edge, NOT a
    comparison against the original edge — a leg whose edge IMPROVED but
    remains below the floor is still skipped. The 'edge_decay_prejudge'
    reason string predates this clarification and is kept for analytics
    continuity (analyzer/dashboard group by it).

    v15.1: the floor is LEG-AWARE. Ladder legs use the lower
    PREJUDGE_MIN_EDGE_PP_LADDER (default 10, mirroring the bot's
    --ladder-min-leg-edge-pp discovery floor); single-bin entries keep
    PREJUDGE_MIN_EDGE_PP (15). The old single 15pp floor made every ladder
    leg born in [10, 15) dead on arrival — 93% of all edge_decay skips in
    the 2026-07-07 incident data — dooming its whole group under strict
    atomicity (only 11/167 groups executed).

    Uses top-of-book ask as a STRICT UPPER BOUND on the edge — the executor
    walks volume-weighted into the book so its actual fill price is at or
    above the best ask. If even the best ask leaves edge below the floor,
    the trade has no chance of executing.

    DB convention: forecast_prob_at_entry stores P(side), not P(YES). See
    weather_edge_bot.py:959-960 (insert_entry). The recheck therefore uses
    fp directly without any side-conditional flip.
    """
    side = row.get("side")
    if side not in ("YES", "NO"):
        return None
    token_id = row.get("token_id_yes") if side == "YES" else row.get("token_id_no")
    if not token_id:
        return None
    forecast_prob = row.get("forecast_prob_at_entry")
    if forecast_prob is None:
        return None
    book = _fetch_orderbook(str(token_id))
    if not book or not book.get("asks"):
        log_event("prejudge_fetch_failed", {
            "entry_id": row.get("entry_id"),
            "token_id_prefix": str(token_id)[:12],
        }, level="DEBUG")
        return None
    best_ask = float(book["asks"][0]["price"])
    fp = float(forecast_prob)
    edge_pp = round((fp - best_ask) * 100.0, 4)
    # v15.1: leg-aware floor (see docstring).
    floor = (PREJUDGE_MIN_EDGE_PP_LADDER if row.get("ladder_group_id")
             else PREJUDGE_MIN_EDGE_PP)
    if edge_pp >= floor:
        return None
    return {
        "entry_id": row.get("entry_id"),
        "reason": "edge_decay_prejudge",
        "original_edge_pp": row.get("edge_pp_at_entry"),
        "current_edge_pp": edge_pp,
        "best_ask": round(best_ask, 4),
        "threshold_pp": floor,
        "side": side,
    }


def _sibling_failed_precheck(row: dict) -> Optional[dict]:
    """v15 F1: free ladder-viability gate, run BEFORE any HTTP or LLM spend.

    Post-mortem 2026-07-07: the judge had no group-awareness — it fully
    reviewed (27 APPROVE + 31 ADJUST, $2.24 = 18.9% of all judge spend)
    legs whose sibling was already REJECTED/SKIPPED, i.e. legs the
    executor's atomic gate was guaranteed to discard as
    ladder_sibling_failed. This check uses the SAME dead-group predicate
    as the executor (db.ladder_group_is_dead) so the two daemons agree.

    Returns None to proceed (non-ladder row, group still viable, or DB
    read error — FAIL-OPEN: never trade availability for economy).
    Returns a loggable payload when the group is already dead."""
    gid = row.get("ladder_group_id")
    if not gid:
        return None
    try:
        with db.connect() as conn:
            statuses = db.query_ladder_group_statuses(conn, gid)
    except Exception:
        return None
    if not db.ladder_group_is_dead(statuses):
        return None
    return {"entry_id": row.get("entry_id"), "ladder_group_id": gid,
            "reason": "ladder_sibling_failed_prejudge",
            "sibling_statuses": sorted(statuses)}


def _skip_sibling_failed(entry_id: int) -> bool:
    """Mark a leg SKIPPED because its group is dead — race-guarded: only
    writes while the row is still PROPOSED (never clobbers a status the
    executor or an earlier same-batch action wrote meanwhile).

    Returns True when it actually wrote; False when the guard held (row
    already terminal — e.g. the KILLER leg the sweep just marked). v15.2:
    callers gate their logging/bookkeeping on the return value, so killer
    legs no longer emit a misleading duplicate judge_skipped_sibling_failed
    event (observed for entries 13/16 on 2026-07-07; DB was never corrupted,
    the event was just noise).

    skip_reason reuses 'ladder_sibling_failed' (analyzer/dashboard keep
    counting all group-death casualties in one bucket);
    judge_skipped_reason='ladder_sibling_failed_prejudge' marks that THIS
    kill happened pre-spend (no judge_reviews row) — the saved-vs-wasted
    split for cost dashboards, same convention as edge_decay_prejudge."""
    with db.connect() as conn:
        cur = conn.execute("SELECT status FROM entries WHERE entry_id = ?",
                           (entry_id,)).fetchone()
        if cur and cur["status"] == "PROPOSED":
            db.update_entry_status(
                conn, entry_id, "SKIPPED",
                judge_skipped_reason="ladder_sibling_failed_prejudge",
                skip_reason="ladder_sibling_failed")
            return True
    return False


def _group_free_guard_sweep(gid: str, current_entry_id: int) -> dict:
    """v15 F2: when the judge first meets ladder group `gid` in a batch, run
    the FREE guards over ALL its PROPOSED legs (central/below/above order)
    BEFORE paying for any leg:

      1. _recheck_edge_prejudge — orderbook HTTP only, $0 LLM
      2. _judge_route with action=='auto_reject' — deterministic, $0

    auto_approve routes are NOT acted on here (they would need the paid
    anomaly scan) — they merely count as "passed". The sweep can therefore
    only kill EARLIER what the per-leg pipeline would kill anyway; it never
    grants an approval. This closes the dominant waste pattern from the
    2026-07-07 post-mortem: 50/58 sibling-failed victims were fully judged
    while their killer died a FREE edge_decay_prejudge death later.

    On the first failing leg (the killer):
      - the killer gets its NATURAL outcome, byte-identical to its own
        turn (prejudge → SKIPPED/edge_decay_prejudge; route →
        apply_verdict(REJECT) with a $0 judge_reviews row);
      - every other PROPOSED leg is marked via _skip_sibling_failed;
      - a ladder_group_dead_prejudge event is emitted.

    Returns {"dead", "killer_entry_id", "killer_reason",
    "prejudge_passed_ids"} — the passed-ids let the caller skip re-fetching
    the current row's orderbook in the same poll. Fail-open per leg: any
    guard error counts as "passed" (never kill a group on a read failure).
    """
    try:
        with db.connect() as conn:
            legs = [dict(l) for l in db.query_ladder_group(conn, gid)]
    except Exception:
        return {"dead": False, "killer_entry_id": None,
                "killer_reason": None, "prejudge_passed_ids": set()}
    pending = [l for l in legs if l.get("status") == "PROPOSED"]
    passed: set = set()
    killer = None
    killer_reason = None

    for leg in pending:
        # (1) free prejudge edge re-check — same write as the leg's own turn
        try:
            stale = _recheck_edge_prejudge(leg)
        except Exception:
            stale = None
        if stale is not None:
            log_event("judge_skipped_prejudge", {**stale, "swept_group": gid})
            with db.connect() as conn:
                cur = conn.execute(
                    "SELECT status FROM entries WHERE entry_id = ?",
                    (leg["entry_id"],)).fetchone()
                if cur and cur["status"] == "PROPOSED":
                    db.update_entry_status(
                        conn, leg["entry_id"], "SKIPPED",
                        judge_skipped_reason="edge_decay_prejudge",
                        skip_reason="edge_decay_prejudge")
            killer, killer_reason = leg, "edge_decay_prejudge"
            break
        # (2) deterministic auto-reject routes ($0). auto_approve/None fall
        # through untouched — the leg will be judged at its own turn.
        try:
            route = _judge_route(leg)
        except Exception:
            route = None
        if route is not None and route.get("action") == "auto_reject":
            verdict = _synthesize_route_verdict(leg, route)
            with db.connect() as conn:
                apply_verdict(conn, leg, verdict)
            _ev = ("judge_autoreject_nontemp"
                   if route.get("kind") == "non_temperature"
                   else "judge_autoreject_proximity")
            log_event(_ev, {
                "entry_id": leg["entry_id"],
                "final_verdict": verdict["verdict"],
                "reason": route["reason"],
                "bot_prob": float(leg.get("forecast_prob_at_entry") or 0),
                "cost_usd": 0.0, "llm_skipped": True,
                "swept_group": gid,
            })
            killer = leg
            killer_reason = f"auto_reject:{route.get('kind') or 'proximity'}"
            break
        passed.add(leg["entry_id"])

    if killer is None:
        return {"dead": False, "killer_entry_id": None,
                "killer_reason": None, "prejudge_passed_ids": passed}

    marked = []
    for leg in pending:
        if leg["entry_id"] == killer["entry_id"]:
            continue
        # v15.2: only count legs actually written (race guard may no-op).
        if _skip_sibling_failed(leg["entry_id"]):
            marked.append(leg["entry_id"])
    log_event("ladder_group_dead_prejudge", {
        "ladder_group_id": gid,
        "killer_entry_id": killer["entry_id"],
        "killer_reason": killer_reason,
        "marked_skipped": marked,
        "est_llm_reviews_saved": len(marked),
    })
    return {"dead": True, "killer_entry_id": killer["entry_id"],
            "killer_reason": killer_reason, "prejudge_passed_ids": passed}


def _threshold_proximity_reason(entry_row) -> Optional[str]:
    """v11 (post-mortem): return an override reason if the bot's forecast
    sits within ~1°C of the threshold — or, for a range market, within ~1°C
    of entering the bracket. At that distance the temperature is a coin flip
    and the trade has no real edge. 91% of the -$771 run's losing range NO
    bets had the forecast within 1°C of the bracket, yet the judge APPROVE'd
    them. Returns None when not too close / not a temp market / unparseable.
    """
    try:
        from weather_edge_helpers import (parse_market, forecast_ref_value,
                                          load_cities)
    except Exception:
        return None
    try:
        spec = parse_market(entry_row["market_question"], entry_row["end_date"],
                            load_cities())
    except Exception:
        return None
    if not spec or spec.metric != "temp":
        return None
    try:
        forecast = json.loads(entry_row["forecast_snapshot_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    ref = forecast_ref_value(spec, forecast)
    if ref is None:
        return None
    # 1°C tolerance; for F markets that's ~1.8°F.
    margin = 1.0 if (spec.threshold_unit or "").upper() == "C" else 1.8
    if spec.comparison == "range" and spec.threshold_value_high is not None:
        lo, hi = spec.threshold_value, spec.threshold_value_high
        # Distance from forecast to the bracket: 0 if already inside.
        if lo - margin <= ref <= hi + margin:
            return (f"forecast {ref:.2f} within {margin:.1f}° of range "
                    f"[{lo:.1f}, {hi:.1f}] — too close to call")
        return None
    dist = abs(ref - spec.threshold_value)
    if dist < margin:
        return (f"forecast {ref:.2f} within {margin:.1f}° of threshold "
                f"{spec.threshold_value:.1f} (|Δ|={dist:.2f}) — too close to call")
    return None


def _range_calibration(entry_row) -> Optional[dict]:
    """v12 (advisor sug_003/004): for a 'range' temp market, return the
    calibration constraint dict, else None.

    Returns {"cap": float, "near": bool, "dist": float, "mae": float,
             "lo": float, "hi": float, "ref": float} where:
      - `cap`  = max allowed judge_prob (RANGE_PROB_CAP_NEAR if the forecast
                 is within RANGE_NEAR_MAE_MULT × MAE of the nearest bin edge,
                 else RANGE_PROB_CAP).
      - `near` = whether the forecast is inside that 2×MAE band (these get
                 downgraded to ADJUST with a conservative size).
    None when not a range temp market or unparseable (fail-open).
    """
    try:
        from weather_edge_helpers import (parse_market, forecast_ref_value,
                                          load_cities, MAE_TEMP_C, MAE_TEMP_F,
                                          ENSEMBLE_PROB_CAP)
    except Exception:
        return None
    try:
        spec = parse_market(entry_row["market_question"], entry_row["end_date"],
                            load_cities())
    except Exception:
        return None
    if (not spec or spec.metric != "temp" or spec.comparison != "range"
            or spec.threshold_value_high is None):
        return None
    try:
        forecast = json.loads(entry_row["forecast_snapshot_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    ref = forecast_ref_value(spec, forecast)
    if ref is None:
        return None
    lo, hi = float(spec.threshold_value), float(spec.threshold_value_high)

    # v13.1 (C): when the bot sized with a CALIBRATED ENSEMBLE, use the
    # ensemble sigma (not the static MAE) for the near-edge band, and relax
    # the far cap to ENSEMBLE_PROB_CAP. A tight 3-model ensemble far from the
    # bin is genuinely confident — clamping it to 0.70 out of generic range
    # skepticism is what froze the pipeline (2026-06-15). Legacy MAE-path
    # range markets keep the conservative 0.70/0.65 caps.
    ensemble_sigma = None
    try:
        dm = json.loads(entry_row["discovery_meta_json"] or "{}")
        if dm.get("ensemble_calibrated") and dm.get("mae_dynamic"):
            ensemble_sigma = float(dm["mae_dynamic"])
    except (TypeError, json.JSONDecodeError):
        pass
    if ensemble_sigma is not None and ensemble_sigma > 0:
        mae = ensemble_sigma
        cap_far = ENSEMBLE_PROB_CAP            # 0.80, matches bot's sizing cap
    else:
        mae = MAE_TEMP_C if (spec.threshold_unit or "").upper() == "C" else MAE_TEMP_F
        cap_far = RANGE_PROB_CAP               # 0.70 (legacy)

    # Distance from the forecast to the nearest edge of [lo, hi] (0 if inside).
    if ref < lo:
        dist = lo - ref
    elif ref > hi:
        dist = ref - hi
    else:
        dist = 0.0
    near = dist < RANGE_NEAR_MAE_MULT * mae
    return {"cap": RANGE_PROB_CAP_NEAR if near else cap_far,
            "near": near, "dist": dist, "mae": mae,
            "lo": lo, "hi": hi, "ref": ref,
            "ensemble": ensemble_sigma is not None}


def _entry_is_pilot(entry_row) -> bool:
    """v17: True when discovery tagged this proposal as an Africa desert/
    subtropical PILOT entry (discovery_meta_json.pilot). Pilot entries always
    get the full LLM cross-check — never a deterministic auto-approve — because
    the continent has no regional model and the ERA5 resolution truth is
    unreliable (see weather-cities.json _africa_pilot_doc). Fail-safe: any
    missing/corrupt field → False (normal routing)."""
    try:
        dm = json.loads(entry_row["discovery_meta_json"] or "{}")
        return bool(dm.get("pilot"))
    except Exception:
        return False


def _judge_route(entry_row) -> Optional[dict]:
    """v13.2 (option B): decide whether this proposal needs the LLM judge at
    all, or can be resolved by the deterministic code guards alone — saving
    the ~$0.04-0.08 LLM spend on cases where the cheap calibrated ensemble is
    already decisive.

    Returns:
      {"action": "auto_reject", "kind": "non_temperature", "reason": str}
          v14 temperature-only policy: non-temp proposal (rain/snow) →
          deterministic REJECT, no LLM, even with JUDGE_AUTOROUTE=0.
      {"action": "auto_reject", "reason": str}
          deterministic threshold/range proximity coin-flip → REJECT, no LLM.
      {"action": "auto_approve", "reason": str, "dist": float, "sigma": float}
          calibrated ensemble, bin ≥ AUTOAPPROVE_MAE_MULT × sigma away → the
          bot's high P(side) is well-founded → APPROVE, no LLM.
      None
          send to the LLM: non-ensemble fallback, or ensemble with the bin
          NEAR the forecast (models effectively disagree relative to the
          bin) — the cases where the LLM earns its cost.

    Routing decides ONLY whether to spend the LLM. Both actions are still
    funneled through apply_verdict(), so every code guard (Rule 6,
    threshold-proximity, range calibration) still runs. Fail-open: any parse
    failure returns None (→ LLM), never a silent auto-approve. The one
    fail-CLOSED case is the v14 non-temp reject above — a market the bot
    cannot price must never reach a gate that could approve it.
    """
    # (0) v14 (2026-07-05): temperature-only policy — FAIL-CLOSED. A non-temp
    # proposal (rain/snow binaries, numeric precip) must never reach the LLM
    # (which can APPROVE it): deterministic REJECT. Runs even with
    # JUDGE_AUTOROUTE=0 because this is policy, not a routing optimization.
    # An unparseable spec (None) keeps the legacy behavior (fall through →
    # LLM). Import stays lazy so _test_autoroute's monkey-patching of
    # helpers.parse_market/load_cities is picked up at call time.
    try:
        from weather_edge_helpers import (parse_market, load_cities,
                                          is_tradeable_spec)
        spec0 = parse_market(entry_row["market_question"],
                             entry_row["end_date"], load_cities())
    except Exception:
        spec0 = None
    if spec0 is not None and not is_tradeable_spec(spec0):
        return {"action": "auto_reject", "kind": "non_temperature",
                "reason": (f"non_temperature_market: metric={spec0.metric} — "
                           f"bot is temperature-only (policy 2026-07-05)")}

    # (0.3) v17 Africa pilot: pilot entries NEVER take a deterministic route —
    # neither auto-approve nor cheap_convexity — they always go to the full LLM
    # judge (None). The deterministic ensemble guards are less trustworthy in
    # Africa (no regional model; ERA5-based calibration/truth biased), so the
    # LLM cross-check is mandatory. Runs AFTER the fail-closed non-temp reject
    # (a non-temp African market is still rejected) and BEFORE every approve
    # path, independent of JUDGE_AUTOROUTE.
    if _entry_is_pilot(entry_row):
        return None

    # (0.5) v11 cheap_convexity: skip the LLM entirely (operator decision).
    # These entries already cleared (i) the tail-calibration gate at
    # discovery, (ii) raw fair > price, (iii) an exit-liquidity gate. The LLM
    # judge is calibrated for normal temperature near the forecast and adds
    # nothing to a pure-convexity tail bet, so it is auto-approved on the
    # deterministic guards. Fail-safe: any missing/corrupt field falls through
    # to None → LLM (never a blind approve). Runs before JUDGE_AUTOROUTE so
    # policy is independent of the routing kill-switch.
    try:
        strat = entry_row["strategy"] if "strategy" in entry_row.keys() else None
    except Exception:
        strat = None
    if strat == "cheap_convexity":
        try:
            dm = json.loads(entry_row["discovery_meta_json"] or "{}")
            price = float(entry_row["entry_price"] or 0)
            if float(dm.get("fair_target", 0)) <= price:
                return {"action": "auto_reject", "kind": "cheap_convexity",
                        "reason": "cc_fair_not_above_price"}
            if float(dm.get("exit_liquidity_shares", 0)) <= 0:
                return {"action": "auto_reject", "kind": "cheap_convexity",
                        "reason": "cc_no_exit_liquidity"}
            return {"action": "auto_approve", "kind": "cheap_convexity",
                    "reason": (f"cheap_convexity guards ok: fair "
                               f"{dm.get('fair_target')} > price {price}, "
                               f"exit liquidity {dm.get('exit_liquidity_shares')}")}
        except Exception:
            return None  # fail-safe → LLM

    if not JUDGE_AUTOROUTE:
        return None

    # (1) Deterministic proximity coin-flip → auto-reject regardless of
    # source. This is a hard code guard in apply_verdict anyway; here we just
    # avoid paying the LLM to reach the same REJECT.
    prox = _threshold_proximity_reason(entry_row)
    if prox:
        return {"action": "auto_reject", "reason": prox}

    # (2) Auto-approve only tight-ensemble, far-from-bin temp bets.
    try:
        from weather_edge_helpers import (parse_market, forecast_ref_value,
                                          load_cities)
    except Exception:
        return None
    try:
        spec = parse_market(entry_row["market_question"], entry_row["end_date"],
                            load_cities())
    except Exception:
        return None
    if not spec or spec.metric != "temp":
        # Unreachable for non-temp since the v14 step (0) fail-closed reject;
        # kept as a belt so auto-approve can never fire on a non-temp spec.
        return None

    # Ensemble sigma is the confidence scale; without a calibrated ensemble
    # (single-source fallback) the LLM cross-check earns its cost.
    ensemble_sigma = None
    try:
        dm = json.loads(entry_row["discovery_meta_json"] or "{}")
        if dm.get("ensemble_calibrated") and dm.get("mae_dynamic"):
            ensemble_sigma = float(dm["mae_dynamic"])
    except (TypeError, json.JSONDecodeError):
        pass
    if not ensemble_sigma or ensemble_sigma <= 0:
        return None

    try:
        forecast = json.loads(entry_row["forecast_snapshot_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    ref = forecast_ref_value(spec, forecast)
    if ref is None:
        return None

    # Distance from the forecast to the bin/threshold it must clear.
    if spec.comparison == "range" and spec.threshold_value_high is not None:
        lo, hi = float(spec.threshold_value), float(spec.threshold_value_high)
        if ref < lo:
            dist = lo - ref
        elif ref > hi:
            dist = ref - hi
        else:
            dist = 0.0  # forecast inside the bin → risky YES → send to LLM.
    else:
        dist = abs(ref - float(spec.threshold_value))

    if dist >= AUTOAPPROVE_MAE_MULT * ensemble_sigma:
        return {"action": "auto_approve",
                "reason": (f"tight ensemble (σ={ensemble_sigma:.2f}), bin "
                           f"{dist:.2f}° away ≥ {AUTOAPPROVE_MAE_MULT:.1f}σ — "
                           f"deterministic guards decisive, LLM skipped"),
                "dist": dist, "sigma": ensemble_sigma}
    # Bin is within the band: models effectively disagree relative to the bin
    # → this is exactly where the LLM's cross-check adds value.
    return None


def _synthesize_route_verdict(entry_row, route: dict) -> dict:
    """Build a verdict dict (matching the LLM's shape) for an auto-routed
    proposal so it can flow through apply_verdict() unchanged. No LLM was
    called, so _meta carries zero cost/tokens."""
    bot_prob = float(entry_row["forecast_prob_at_entry"] or 0.0)
    if route["action"] == "auto_reject":
        return {
            "verdict": "REJECT",
            "confidence": 0.9,
            # judge_prob == bot_prob → no spurious Rule-6 divergence; the
            # REJECT stands on the proximity reason, not a prob disagreement.
            "judge_prob": bot_prob,
            "rationale": f"[AUTO-ROUTE REJECT: {route['reason']}]",
            "evidence_summary": json.dumps({"auto_route": "reject",
                                            "reason": route["reason"]}),
            "adjusted_side": None,
            "adjusted_size_usd": None,
            "_meta": {},
        }
    # auto_approve: judge_prob = bot_prob so divergence = 0 (Rule 6 passes);
    # apply_verdict's range-calibration then caps the stored prob if needed.
    return {
        "verdict": "APPROVE",
        "confidence": 0.8,
        "judge_prob": bot_prob,
        "rationale": f"[AUTO-ROUTE APPROVE: {route['reason']}]",
        "evidence_summary": json.dumps({
            "auto_route": "approve", "reason": route["reason"],
            "dist_to_bin": round(route.get("dist", 0.0), 3),
            "ensemble_sigma": round(route.get("sigma", 0.0), 3),
        }),
        "adjusted_side": None,
        "adjusted_size_usd": None,
        "_meta": {},
    }


_ANOMALY_SCAN_SYSTEM = (
    "You are a weather-market anomaly scanner. A calibrated 3-model temperature "
    "ensemble (ICON+GFS+ECMWF) already covers routine synoptic weather, so you "
    "are NOT re-forecasting the temperature. Your ONLY job is to web-search for "
    "an ACTIVE, SPECIFIC catalyst for the given city and date that the ensemble "
    "could miss — an incoming heat dome / cold front / atmospheric river / storm "
    "/ hurricane, wildfire smoke, or a resolution-relevant news/event story "
    "(e.g. a station change, a data correction, an event-day factor). "
    "Do 1-3 focused searches, then answer with a SINGLE JSON object and nothing "
    "else, in a ```json fenced block:\n"
    '{"catalyst_found": <true|false>, "summary": "<one sentence>"}\n'
    "Set catalyst_found=true ONLY when you find a specific, active catalyst that "
    "could plausibly move the outcome or affect resolution. Routine seasonal "
    "weather with no incoming system is catalyst_found=false."
)


def _parse_scan_json(text: str) -> Optional[dict]:
    """Extract the {catalyst_found, summary} object from the scan's final text.
    Tolerates ```json fences and surrounding prose. Returns None if unparseable.
    """
    if not text:
        return None
    import re
    # Prefer a fenced block; fall back to the last {...} span.
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    blob = m.group(1) if m else None
    if blob is None:
        m = re.search(r"(\{[^{}]*\"catalyst_found\"[^{}]*\})", text, re.DOTALL)
        blob = m.group(1) if m else None
    if blob is None:
        return None
    try:
        obj = json.loads(blob)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or "catalyst_found" not in obj:
        return None
    return {"catalyst_found": bool(obj.get("catalyst_found")),
            "summary": str(obj.get("summary") or "")[:500]}


def _anomaly_scan(entry_row) -> Optional[dict]:
    """v13.2 option-1: a CHEAP web-search-only catalyst check for a proposal the
    auto-router would otherwise approve without any LLM. See ANOMALY_SCAN notes
    above.

    Returns {"catalyst_found": bool, "summary": str, "_meta": {...}} on success,
    or None on ANY failure (missing key/SDK, API error, pause, unparseable) —
    the caller escalates to the full judge on None (fail-safe).
    """
    try:
        import anthropic
    except ImportError:
        return None
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None

    user_content = json.dumps({
        "city": entry_row.get("city_resolved"),
        "date": (entry_row.get("end_date") or "")[:10],
        "threshold_value": entry_row.get("threshold_value"),
        "threshold_unit": entry_row.get("threshold_unit"),
        "comparison": entry_row.get("comparison"),
        "bot_side": entry_row.get("side"),
        "note": ("The ensemble is confident the temperature will NOT land in "
                 "the bin (bot is betting accordingly). Look only for a catalyst "
                 "that would upset that, or affect how the market resolves."),
    }, ensure_ascii=False)

    # Basic web-search variant — supported on every model (incl. Haiku 4.5);
    # the _20260209 dynamic-filtering variant is Opus/Sonnet-4.6+ only.
    tools = [{"type": "web_search_20250305", "name": "web_search",
              "max_uses": ANOMALY_SCAN_MAX_USES}]
    is_haiku = "haiku" in ANOMALY_SCAN_MODEL.lower()
    request_kwargs = {
        "model": ANOMALY_SCAN_MODEL,
        "max_tokens": ANOMALY_SCAN_MAX_TOKENS,
        "system": _ANOMALY_SCAN_SYSTEM,
        "messages": [{"role": "user", "content": user_content}],
        "tools": tools,
    }
    if not is_haiku:
        request_kwargs["thinking"] = {"type": "adaptive"}
        request_kwargs["output_config"] = {"effort": "low"}

    client = anthropic.Anthropic()
    t0 = time.monotonic()
    try:
        response = client.messages.create(**request_kwargs)
    except Exception as e:
        log_event("anomaly_scan_error", {"entry_id": entry_row.get("entry_id"),
                                         "err": str(e),
                                         "type": type(e).__name__}, level="WARN")
        return None
    duration_ms = int((time.monotonic() - t0) * 1000)

    # A server-tool loop that ran out of iterations returns pause_turn — treat
    # as inconclusive and escalate (don't try to resume in the cheap path).
    if getattr(response, "stop_reason", None) == "pause_turn":
        log_event("anomaly_scan_paused", {"entry_id": entry_row.get("entry_id")},
                  level="WARN")
        return None

    text = "".join(b.text for b in response.content
                   if getattr(b, "type", None) == "text")
    parsed = _parse_scan_json(text)
    if parsed is None:
        log_event("anomaly_scan_unparseable",
                  {"entry_id": entry_row.get("entry_id"), "text": text[:300]},
                  level="WARN")
        return None

    usage = response.usage
    pricing = PRICING.get(ANOMALY_SCAN_MODEL, PRICING["claude-haiku-4-5"])
    # Token-based cost only (web-search per-query billing is extra and not
    # captured here — a small, deliberate undercount for budget tracking).
    cost = (usage.input_tokens * pricing["input"] / 1_000_000 +
            usage.output_tokens * pricing["output"] / 1_000_000 +
            (usage.cache_read_input_tokens or 0) * pricing["cache_read"] / 1_000_000)
    parsed["_meta"] = {"cost_usd": cost, "duration_ms": duration_ms,
                       "model": ANOMALY_SCAN_MODEL, "tokens_in": usage.input_tokens,
                       "tokens_out": usage.output_tokens}
    return parsed


def _scan_outcome(scan: Optional[dict]) -> str:
    """Decide the auto-approve path from an _anomaly_scan result:
      'approve'    — scan clean (no catalyst) → auto-approve, LLM judge skipped
      'escalate'   — catalyst found → send to the full LLM judge
      'unavailable'— scan failed (None) → escalate to the full judge (fail-safe)
    """
    if scan is None:
        return "unavailable"
    return "escalate" if scan.get("catalyst_found") else "approve"


def apply_verdict(conn, entry_row, verdict: dict) -> None:
    """Persist the verdict to judge_reviews and update entry status."""
    meta = verdict.pop("_meta", {})
    bot_prob = float(entry_row["forecast_prob_at_entry"] or 0)
    judge_prob = float(verdict["judge_prob"])

    # v9: hard-enforce two rules that the prompt asks the LLM to follow
    # but which it ignores ~30% of the time (loss analysis 2026-05-16:
    # 116/380 APPROVE+ADJUST verdicts violated Rule 6, including the
    # London #318 trade that lost $8.65). Override in code so the bot
    # never executes a verdict that violates these guardrails.
    original_verdict = verdict["verdict"]
    confidence = float(verdict.get("confidence") or 0.0)
    divergence = abs(judge_prob - bot_prob)

    override_reason = None
    override_kind = "rule6_override"
    if original_verdict in ("APPROVE", "ADJUST"):
        if confidence == 0.0:
            override_reason = (
                f"confidence=0.0 indicates broken/unparseable LLM output; "
                f"cannot trust APPROVE/ADJUST with no confidence")
        elif divergence > 0.20:
            # v13.1: Rule-6 divergence is handled DIFFERENTLY by verdict.
            #  - APPROVE: hard-REJECT (bot wanted full size, judge
            #    fundamentally disagrees — unchanged behavior).
            #  - ADJUST: the judge already wants to size down, not kill.
            #    Keep ADJUST but cap the stake to RULE6_DOWNSIZE_USD so the
            #    judge's (lower) conviction governs exposure — UNLESS the
            #    judge itself sees no edge (judge_prob <= the side's price),
            #    in which case REJECT (never trade without an edge, §1.1).
            if original_verdict == "APPROVE":
                override_reason = (
                    f"Rule 6 violation on APPROVE: |judge_prob {judge_prob:.2f} "
                    f"- bot_prob {bot_prob:.2f}| = {divergence*100:.0f}pp > 20pp")
            else:  # ADJUST
                entry_price = float(entry_row["entry_price"] or 0.0)
                if judge_prob <= entry_price:
                    override_reason = (
                        f"Rule 6 on ADJUST + no edge at judge_prob: "
                        f"judge_prob {judge_prob:.2f} <= side price "
                        f"{entry_price:.2f} (bot {bot_prob:.2f})")
                    override_kind = "rule6_adjust_no_edge"
                else:
                    cur_cap = verdict.get("adjusted_size_usd")
                    new_cap = (RULE6_DOWNSIZE_USD if not cur_cap
                               else min(float(cur_cap), RULE6_DOWNSIZE_USD))
                    verdict["adjusted_size_usd"] = new_cap
                    log_event("rule6_adjust_downsize", {
                        "entry_id": entry_row["entry_id"],
                        "judge_prob": judge_prob, "bot_prob": bot_prob,
                        "divergence_pp": round(divergence * 100, 1),
                        "entry_price": entry_price,
                        "size_cap_usd": new_cap,
                    }, level="WARN")
        else:
            # v11: threshold-proximity hard-enforce (forecast ~1°C from the
            # threshold / range edge → coin flip → REJECT). Checked only when
            # Rule 6 didn't already fire.
            prox = _threshold_proximity_reason(entry_row)
            if prox:
                override_reason = prox
                override_kind = "threshold_proximity_override"

    if override_reason:
        log_event(override_kind, {
            "entry_id": entry_row["entry_id"],
            "original_verdict": original_verdict,
            "confidence": confidence,
            "judge_prob": judge_prob,
            "bot_prob": bot_prob,
            "divergence_pp": round(divergence * 100, 1),
            "reason": override_reason,
        }, level="WARN")
        verdict["verdict"] = "REJECT"
        verdict["rationale"] = (
            f"[SYSTEM OVERRIDE: {override_reason}]\n\n"
            f"Original LLM verdict was {original_verdict}. Rationale below:\n\n"
            f"{verdict.get('rationale', '')}")
        # ADJUST fields no longer apply
        verdict["adjusted_side"] = None
        verdict["adjusted_size_usd"] = None

    # v12 (advisor sug_003/004): range-market calibration. Applies only when
    # the verdict still stands as APPROVE/ADJUST (a REJECT above skips this).
    # Caps the stored judge_prob and, for forecasts within 2×MAE of a bin
    # edge, downgrades APPROVE→ADJUST with a conservative size so per-loss
    # exposure on these miscalibrated range bets is bounded.
    if verdict["verdict"] in ("APPROVE", "ADJUST"):
        rc = _range_calibration(entry_row)
        if rc is not None:
            if judge_prob > rc["cap"]:
                log_event("range_calibration_cap", {
                    "entry_id": entry_row["entry_id"],
                    "judge_prob": judge_prob, "capped_to": rc["cap"],
                    "near_edge": rc["near"], "dist_to_bin": round(rc["dist"], 2),
                    "mae": rc["mae"],
                }, level="INFO")
                judge_prob = rc["cap"]
                verdict["judge_prob"] = rc["cap"]
            if rc["near"]:
                prev_verdict = verdict["verdict"]
                verdict["verdict"] = "ADJUST"
                cur_cap = verdict.get("adjusted_size_usd")
                new_cap = (RANGE_ADJUST_SIZE_USD if not cur_cap
                           else min(float(cur_cap), RANGE_ADJUST_SIZE_USD))
                verdict["adjusted_size_usd"] = new_cap
                log_event("range_calibration_adjust", {
                    "entry_id": entry_row["entry_id"],
                    "original_verdict": prev_verdict,
                    "size_cap_usd": new_cap,
                    "dist_to_bin": round(rc["dist"], 2),
                    "bin": [rc["lo"], rc["hi"]], "forecast": round(rc["ref"], 2),
                }, level="WARN")

    # v10: persist the system prompt by content-hash so this review
    # remains reproducible even after the prompt file is edited.
    system_prompt_sha256 = None
    sp_text = meta.get("system_prompt_text")
    if sp_text:
        try:
            system_prompt_sha256 = db.upsert_judge_prompt(conn, sp_text)
        except Exception as e:
            log_event("judge_prompt_persist_failed",
                      {"err": str(e), "entry_id": entry_row["entry_id"]},
                      level="WARN")

    db.insert_judge_review(
        conn,
        entry_id=entry_row["entry_id"],
        ts=_now_iso(),
        verdict=verdict["verdict"],
        confidence=verdict["confidence"],
        judge_prob=judge_prob,
        bot_prob=bot_prob,
        prob_delta=judge_prob - bot_prob,
        # v6: store full rationale (was truncated to 1500 chars). The
        # advisor needs the full text to diagnose hallucination patterns.
        rationale=verdict["rationale"],
        evidence_json=verdict.get("evidence_summary", {}),
        adjusted_side=verdict.get("adjusted_side"),
        adjusted_size_usd=verdict.get("adjusted_size_usd"),
        llm_model=meta.get("model"),
        tokens_in=meta.get("tokens_in"),
        tokens_out=meta.get("tokens_out"),
        cache_read_tokens=meta.get("cache_read_tokens"),
        cost_usd=meta.get("cost_usd"),
        duration_ms=meta.get("duration_ms"),
        # v6: full input + raw response for accuracy / hallucination audit.
        input_context_json=meta.get("input_context_json"),
        raw_response_json=meta.get("raw_response_json"),
        # v10: hash of the system prompt active at the time of this call.
        # Resolve text via: SELECT text FROM judge_prompts WHERE sha256=?
        system_prompt_sha256=system_prompt_sha256,
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
    # PID file for dashboard-driven restart.
    from pathlib import Path as _P
    pid_file = _P.home() / ".polymarket-paper" / "judge.pid.json"
    try:
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = pid_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "pid": os.getpid(),
            "argv": [sys.executable] + sys.argv,
            "cwd": str(_P.cwd()),
            "started_at": _now_iso(),
        }), encoding="utf-8")
        tmp.replace(pid_file)
    except OSError as e:
        log_event("warn", {"where": "judge_pidfile_write", "err": str(e)},
                  level="WARN")
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

            # v15 F3: per-batch group state. Siblings arrive contiguous
            # (query_pending_proposals orders by ladder_group_id within ttr
            # ties), so a leg killed in this batch short-circuits its
            # siblings in memory, without re-querying the DB per row.
            dead_groups: set = set()
            swept_groups: set = set()   # v15 F2: grupos já varridos no batch

            for row in rows:
                if _shutdown:
                    break
                row_dict = dict(row)
                gid = row_dict.get("ladder_group_id")

                # v15 F1+F3: ladder-viability gate — if the group is already
                # dead (a sibling REJECTED/SKIPPED), the executor will kill
                # this leg regardless of our verdict; skip BEFORE any HTTP
                # or LLM spend. Batch dead-set first (free), live DB check
                # as the cross-poll backstop.
                if gid and gid in dead_groups:
                    sib = {"entry_id": row_dict["entry_id"],
                           "ladder_group_id": gid,
                           "reason": "ladder_sibling_failed_prejudge",
                           "via": "batch_dead_set"}
                else:
                    sib = _sibling_failed_precheck(row_dict)
                if sib is not None:
                    # v15.2: log only when we actually marked the row —
                    # a killer leg already terminal stays silent (the
                    # guard no-ops) instead of emitting a misleading
                    # duplicate sibling_failed event.
                    if _skip_sibling_failed(row_dict["entry_id"]):
                        log_event("judge_skipped_sibling_failed", sib)
                    if gid:
                        dead_groups.add(gid)
                    continue

                # v15 F2: first leg of a group in this batch → sweep the
                # FREE guards of ALL siblings before paying for any leg.
                # 86% of the 2026-07-07 waste was a sibling dying a free
                # edge_decay death AFTER the victim's paid review.
                prejudge_fresh = False
                if gid and gid not in swept_groups:
                    swept_groups.add(gid)
                    sweep = _group_free_guard_sweep(gid, row_dict["entry_id"])
                    if sweep["dead"]:
                        dead_groups.add(gid)
                        continue   # current row was marked inside the sweep
                    prejudge_fresh = (row_dict["entry_id"]
                                      in sweep["prejudge_passed_ids"])

                # Pre-judge edge re-check (v10): if the orderbook has moved
                # such that current edge is below the floor, skip the LLM
                # call entirely. ~$0.04 saved per upstream skip.
                # (v15 F2: skip the duplicate fetch when this row's recheck
                # already ran clean inside this poll's group sweep.)
                stale = (None if prejudge_fresh
                         else _recheck_edge_prejudge(row_dict))
                if stale is not None:
                    log_event("judge_skipped_prejudge", stale)
                    with db.connect() as conn:
                        db.update_entry_status(
                            conn, row_dict["entry_id"], "SKIPPED",
                            judge_skipped_reason="edge_decay_prejudge",
                            skip_reason="edge_decay_prejudge")
                    if gid:
                        dead_groups.add(gid)   # v15 F3: grupo morreu agora
                    continue

                # v13.2 (option B): conditional gate. Resolve the decisive
                # cases (deterministic proximity coin-flip → REJECT; tight
                # ensemble far from bin → APPROVE) WITHOUT spending the full
                # LLM judge. Only genuinely uncertain / non-ensemble / non-temp
                # cases fall through to review_proposal(). Synthesized verdicts
                # still flow through apply_verdict() → all code guards run.
                route = _judge_route(row_dict)

                # Deterministic proximity coin-flip → REJECT, no LLM at all.
                if route is not None and route["action"] == "auto_reject":
                    verdict = _synthesize_route_verdict(row_dict, route)
                    with db.connect() as conn:
                        apply_verdict(conn, row_dict, verdict)
                    # v14: distinguish the temperature-only policy reject from
                    # the proximity coin-flip reject in the event stream.
                    _ev = ("judge_autoreject_nontemp"
                           if route.get("kind") == "non_temperature"
                           else "judge_autoreject_proximity")
                    log_event(_ev, {
                        "entry_id": row_dict["entry_id"],
                        "final_verdict": verdict["verdict"],
                        "reason": route["reason"],
                        "bot_prob": float(row_dict["forecast_prob_at_entry"] or 0),
                        "cost_usd": 0.0, "llm_skipped": True,
                    })
                    if gid:
                        dead_groups.add(gid)   # v15 F3: REJECT mata o grupo
                    continue

                # Tight-ensemble-far-from-bin: run the CHEAP web-search-only
                # anomaly scan (option-1). Clean → auto-approve (full judge
                # skipped); catalyst found or scan unavailable → fall through to
                # the full judge. This closes the auto-approve blind spot for
                # non-meteorological catalysts the ensemble can't see.
                if route is not None and route["action"] == "auto_approve":
                    # v11 cheap_convexity: skip the web-search anomaly scan too
                    # (operator chose "no LLM" for this strategy). Approve
                    # directly on the deterministic guards.
                    if route.get("kind") == "cheap_convexity":
                        verdict = _synthesize_route_verdict(row_dict, route)
                        with db.connect() as conn:
                            apply_verdict(conn, row_dict, verdict)
                        log_event("judge_autoapprove_cheap_convexity", {
                            "entry_id": row_dict["entry_id"],
                            "final_verdict": verdict["verdict"],
                            "reason": route["reason"],
                            "cost_usd": 0.0, "llm_skipped": True,
                        })
                        continue
                    scan = _anomaly_scan(row_dict) if JUDGE_ANOMALY_SCAN else {
                        "catalyst_found": False, "summary": "scan disabled",
                        "_meta": {}}
                    scan_cost = (scan or {}).get("_meta", {}).get("cost_usd", 0.0)
                    if scan_cost:
                        _record_spend(scan_cost)
                    outcome = _scan_outcome(scan)
                    if outcome == "approve":
                        verdict = _synthesize_route_verdict(row_dict, route)
                        verdict["rationale"] += (
                            f"\n[anomaly scan: no catalyst — "
                            f"{scan.get('summary', '')}]")
                        with db.connect() as conn:
                            apply_verdict(conn, row_dict, verdict)
                        log_event("judge_autoapprove_ensemble", {
                            "entry_id": row_dict["entry_id"],
                            "final_verdict": verdict["verdict"],
                            "reason": route["reason"],
                            "scan_summary": scan.get("summary", ""),
                            "bot_prob": float(
                                row_dict["forecast_prob_at_entry"] or 0),
                            "cost_usd": round(scan_cost, 4),
                            "llm_skipped": True,
                        })
                        continue
                    # escalate or unavailable → full judge (fall through)
                    log_event("judge_autoapprove_escalated", {
                        "entry_id": row_dict["entry_id"],
                        "outcome": outcome,
                        "reason": route["reason"],
                        "scan_summary": (scan or {}).get("summary", ""),
                        "scan_cost_usd": round(scan_cost, 4),
                    }, level="WARN")

                t0 = time.monotonic()
                verdict = review_proposal(row_dict, system_prompt)
                duration_s = time.monotonic() - t0

                if not verdict:
                    log_event("judge_failed", {"entry_id": row_dict["entry_id"]},
                              level="WARN")
                    # Skip the DB update during shutdown — the verdict will
                    # be retried on next startup. Trying to write here races
                    # with the bot and can fail with 'database is locked'.
                    if _shutdown:
                        break
                    with db.connect() as conn:
                        db.update_entry_status(conn, row_dict["entry_id"], "SKIPPED",
                                                judge_skipped_reason="judge_unavailable",
                                                skip_reason="judge_unavailable")
                    if gid:
                        dead_groups.add(gid)   # v15 F3: SKIP mata o grupo
                    continue

                cost = verdict.get("_meta", {}).get("cost_usd", 0)
                _record_spend(cost)

                with db.connect() as conn:
                    apply_verdict(conn, row_dict, verdict)

                # v15 F3: apply_verdict muta o dict in place (overrides Rule 6
                # etc.) — o verdict FINAL decide se o grupo morreu.
                if gid and verdict.get("verdict") == "REJECT":
                    dead_groups.add(gid)

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
    try:
        pid_file.unlink(missing_ok=True)
    except (NameError, OSError):
        pass


# ---------------------------------------------------------------------------
# Inline tests for the Rule 6 enforcement in apply_verdict (v9)
# Run: python weather_edge_judge.py --test-rule6
# ---------------------------------------------------------------------------

def _test_rule6_enforce():
    """Standalone tests of the override logic in apply_verdict.
    We don't actually call apply_verdict (it writes to DB); we re-implement
    the decision tree here so the test is hermetic and fast."""

    def _decide(verdict_dict: dict, bot_prob: float, entry_price: float = 0.0):
        """Mirror of the override block in apply_verdict() (v13.1)."""
        v = dict(verdict_dict)  # copy
        original_verdict = v["verdict"]
        confidence = float(v.get("confidence") or 0.0)
        judge_prob = float(v["judge_prob"])
        divergence = abs(judge_prob - bot_prob)
        override_reason = None
        if original_verdict in ("APPROVE", "ADJUST"):
            if confidence == 0.0:
                override_reason = "confidence=0.0"
            elif divergence > 0.20:
                if original_verdict == "APPROVE":
                    override_reason = f"Rule 6 violation on APPROVE ({divergence*100:.0f}pp)"
                else:  # ADJUST: downsize unless the judge sees no edge
                    if judge_prob <= entry_price:
                        override_reason = "Rule 6 on ADJUST + no edge at judge_prob"
                    else:
                        cur = v.get("adjusted_size_usd")
                        v["adjusted_size_usd"] = (RULE6_DOWNSIZE_USD if not cur
                                                  else min(float(cur), RULE6_DOWNSIZE_USD))
                        return v  # stays ADJUST, downsized
        if override_reason:
            v["verdict"] = "REJECT"
            v["rationale"] = (f"[SYSTEM OVERRIDE: {override_reason}]\n\n"
                               f"Original LLM verdict was {original_verdict}. "
                               f"Rationale below:\n\n{v.get('rationale', '')}")
            v["adjusted_side"] = None
            v["adjusted_size_usd"] = None
        return v

    # Test 1: APPROVE with small Δ passes through
    r = _decide({"verdict": "APPROVE", "confidence": 0.80,
                  "judge_prob": 0.85, "rationale": "looks good"},
                 bot_prob=0.80)
    assert r["verdict"] == "APPROVE", r
    assert "OVERRIDE" not in r["rationale"]
    print(f"Test 1 PASS: APPROVE Δ=5pp not overridden")

    # Test 2: APPROVE with Δ > 20pp gets overridden to REJECT
    r = _decide({"verdict": "APPROVE", "confidence": 0.82,
                  "judge_prob": 0.10, "rationale": "I think it's still fine"},
                 bot_prob=0.90)
    assert r["verdict"] == "REJECT", r
    assert "Rule 6 violation on APPROVE (80pp)" in r["rationale"]
    assert "I think it's still fine" in r["rationale"]  # original preserved
    print(f"Test 2 PASS: APPROVE Δ=80pp → REJECT (rationale preserved)")

    # Test 3: ADJUST with confidence=0.0 gets overridden
    r = _decide({"verdict": "ADJUST", "confidence": 0.0,
                  "judge_prob": 0.10, "rationale": "data contradicts bot",
                  "adjusted_side": "YES", "adjusted_size_usd": 5.0},
                 bot_prob=0.90)
    assert r["verdict"] == "REJECT", r
    assert "confidence=0.0" in r["rationale"]
    assert r["adjusted_side"] is None
    assert r["adjusted_size_usd"] is None
    print(f"Test 3 PASS: ADJUST conf=0.0 → REJECT + adjusted fields cleared")

    # Test 4: REJECT unchanged even with Δ=80pp (one-way override)
    r = _decide({"verdict": "REJECT", "confidence": 0.85,
                  "judge_prob": 0.10, "rationale": "clearly wrong"},
                 bot_prob=0.90)
    assert r["verdict"] == "REJECT", r
    assert "OVERRIDE" not in r["rationale"]
    print(f"Test 4 PASS: REJECT Δ=80pp NOT promoted (one-way)")

    # Test 5: edge case Δ exactly 20pp does NOT trigger (> not >=)
    r = _decide({"verdict": "APPROVE", "confidence": 0.80,
                  "judge_prob": 0.30, "rationale": "borderline"},
                 bot_prob=0.50)
    assert r["verdict"] == "APPROVE", f"exactly 20pp should pass: {r}"
    print(f"Test 5 PASS: Δ=20pp exactly is not a violation (> 0.20 threshold)")

    # Test 6 (v13.1): ADJUST Δ=21pp with edge at judge_prob → DOWNSIZE, keep
    # ADJUST (was REJECT pre-v13.1). judge_prob 0.69 > side price 0.55.
    r = _decide({"verdict": "ADJUST", "confidence": 0.65,
                  "judge_prob": 0.69, "rationale": "moderately confident",
                  "adjusted_size_usd": 8.0},
                 bot_prob=0.90, entry_price=0.55)
    assert r["verdict"] == "ADJUST", r
    assert "OVERRIDE" not in r["rationale"]
    assert r["adjusted_size_usd"] == min(8.0, RULE6_DOWNSIZE_USD), r
    print(f"Test 6 PASS: ADJUST Δ=21pp + edge → DOWNSIZE (stays ADJUST, cap ${r['adjusted_size_usd']})")

    # Test 7 (v13.1): ADJUST Δ>20pp but judge sees NO edge (judge_prob <=
    # side price) → REJECT (never trade a -EV bet, §1.1).
    r = _decide({"verdict": "ADJUST", "confidence": 0.6,
                  "judge_prob": 0.65, "rationale": "thin",
                  "adjusted_size_usd": 8.0},
                 bot_prob=0.94, entry_price=0.66)
    assert r["verdict"] == "REJECT", r
    assert "no edge at judge_prob" in r["rationale"]
    print(f"Test 7 PASS: ADJUST Δ>20pp + judge_prob<=price → REJECT (no edge)")

    # Test 8 (v13.1): ADJUST with bare downsize when no prior adjusted_size.
    r = _decide({"verdict": "ADJUST", "confidence": 0.55,
                  "judge_prob": 0.65, "rationale": "size down"},
                 bot_prob=0.95, entry_price=0.50)
    assert r["verdict"] == "ADJUST" and r["adjusted_size_usd"] == RULE6_DOWNSIZE_USD, r
    print(f"Test 8 PASS: ADJUST downsize with no prior cap → ${RULE6_DOWNSIZE_USD}")

    print("\nAll Rule 6 enforce tests PASS (6/6)")


# ---------------------------------------------------------------------------
# Inline tests for pre-judge edge re-check (v10)
# Run: python weather_edge_judge.py --test-prejudge
# ---------------------------------------------------------------------------

def _test_sibling_gate():
    """v15 F1: hermetic tests for _sibling_failed_precheck + the race-guarded
    _skip_sibling_failed write. No network, no LLM."""
    import tempfile
    import weather_edge_judge as mod

    calls = {"statuses": 0, "fetch": 0}
    saved_statuses = db.query_ladder_group_statuses
    saved_connect = db.connect
    saved_fetch = mod._fetch_orderbook
    mod._fetch_orderbook = lambda tid: calls.__setitem__(
        "fetch", calls["fetch"] + 1) or {}

    class FakeConn:
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def stub_statuses(result):
        def f(conn, gid):
            calls["statuses"] += 1
            return result
        return f

    try:
        db.connect = lambda path=None: FakeConn()

        # 1. Non-ladder row → None, sem query de statuses.
        db.query_ladder_group_statuses = stub_statuses(["REJECTED"])
        assert mod._sibling_failed_precheck(
            {"entry_id": 1, "ladder_group_id": None}) is None
        assert mod._sibling_failed_precheck({"entry_id": 1}) is None
        assert calls["statuses"] == 0, calls
        print("Test G1 PASS: linha não-ladder → None sem consultar o grupo")

        # 2. Grupo vivo → None (procede ao julgamento normal).
        db.query_ladder_group_statuses = stub_statuses(
            ["PROPOSED", "PROPOSED"])
        assert mod._sibling_failed_precheck(
            {"entry_id": 2, "ladder_group_id": "g1"}) is None
        db.query_ladder_group_statuses = stub_statuses(
            ["APPROVED", "PROPOSED"])
        assert mod._sibling_failed_precheck(
            {"entry_id": 2, "ladder_group_id": "g1"}) is None
        print("Test G2 PASS: grupo vivo → None (julga normalmente)")

        # 3. Irmã REJECTED (14% dos killers) → payload.
        db.query_ladder_group_statuses = stub_statuses(
            ["PROPOSED", "REJECTED"])
        r = mod._sibling_failed_precheck(
            {"entry_id": 3, "ladder_group_id": "g2"})
        assert r is not None and r["reason"] == "ladder_sibling_failed_prejudge"
        assert r["sibling_statuses"] == ["PROPOSED", "REJECTED"], r

        # 4. Irmã SKIPPED (86% dos killers: edge_decay_prejudge — e todos os
        #    vetores latentes: budget_exceeded, ladder_aborted, unavailable).
        db.query_ladder_group_statuses = stub_statuses(
            ["SKIPPED", "PROPOSED"])
        r = mod._sibling_failed_precheck(
            {"entry_id": 4, "ladder_group_id": "g3"})
        assert r is not None and r["ladder_group_id"] == "g3"
        print("Test G3 PASS: irmã REJECTED/SKIPPED → payload de skip pré-gasto")

        # 5. Erro de DB → fail-open (None): nunca bloquear julgamento.
        def boom(path=None):
            raise RuntimeError("db locked")
        db.connect = boom
        assert mod._sibling_failed_precheck(
            {"entry_id": 5, "ladder_group_id": "g4"}) is None
        print("Test G4 PASS: erro de DB → fail-open (segue para o judge)")

        # 6. O gate não faz NENHUM fetch de orderbook (roda pré-HTTP).
        assert calls["fetch"] == 0, calls
        print("Test G5 PASS: zero fetches de orderbook no gate")
    finally:
        db.query_ladder_group_statuses = saved_statuses
        db.connect = saved_connect
        mod._fetch_orderbook = saved_fetch

    # 7. _skip_sibling_failed com DB real: PROPOSED → SKIPPED com os dois
    #    reasons; REJECTED (killer) → intocado (race guard).
    tmp = Path(tempfile.mkdtemp()) / "sibling_gate.db"
    db.init_db(tmp)
    _orig_connect = db.connect
    try:
        db.connect = lambda path=None: _orig_connect(tmp)
        ts = "2026-07-07T00:00:00+00:00"
        with db.connect() as conn:
            for status in ("PROPOSED", "REJECTED"):
                conn.execute(
                    "INSERT INTO entries (ts, market_slug, market_question, "
                    "side, status, ladder_group_id, entry_price, strategy) "
                    "VALUES (?, 's', 'q', 'NO', ?, 'g5', 0.3, 'weather_edge')",
                    (ts, status))
            conn.commit()
        wrote1 = _skip_sibling_failed(1)
        wrote2 = _skip_sibling_failed(2)   # REJECTED — não pode ser sobrescrito
        with db.connect() as conn:
            r1 = conn.execute("SELECT status, skip_reason, "
                              "judge_skipped_reason FROM entries "
                              "WHERE entry_id=1").fetchone()
            r2 = conn.execute("SELECT status FROM entries "
                              "WHERE entry_id=2").fetchone()
        assert wrote1 is True and wrote2 is False, (wrote1, wrote2)
        assert r1["status"] == "SKIPPED"
        assert r1["skip_reason"] == "ladder_sibling_failed"
        assert r1["judge_skipped_reason"] == "ladder_sibling_failed_prejudge"
        assert r2["status"] == "REJECTED", r2["status"]
        # v15.2: re-marcar a já-SKIPPED também é no-op silencioso (False)
        assert _skip_sibling_failed(1) is False
        print("Test G6 PASS: write race-guarded + retorno bool (True só "
              "quando escreve; killer REJECTED/re-mark → False)")
    finally:
        db.connect = _orig_connect

    print("\nAll --test-sibling-gate PASS")


def _test_group_sweep():
    """v15 F2: hermetic tests for _group_free_guard_sweep — temp DB, stubbed
    _recheck_edge_prejudge/_judge_route, real apply_verdict. No network."""
    import tempfile
    import weather_edge_judge as mod

    tmp = Path(tempfile.mkdtemp()) / "group_sweep.db"
    db.init_db(tmp)
    _orig_connect = db.connect
    saved_recheck = mod._recheck_edge_prejudge
    saved_route = mod._judge_route
    ts = "2026-07-07T00:00:00+00:00"

    def seed(gid, pos, status="PROPOSED", prob=0.8):
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO entries (ts, market_slug, market_question, side, "
                "status, ladder_group_id, ladder_position, entry_price, "
                "forecast_prob_at_entry, strategy) VALUES "
                "(?, 's', 'q', 'NO', ?, ?, ?, 0.3, ?, 'weather_edge')",
                (ts, status, gid, pos, prob))
            eid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.commit()
        return eid

    def status_of(eid):
        with db.connect() as conn:
            r = conn.execute("SELECT status, skip_reason, "
                             "judge_skipped_reason FROM entries "
                             "WHERE entry_id=?", (eid,)).fetchone()
        return dict(r)

    def n_reviews():
        with db.connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM judge_reviews").fetchone()[0]

    recheck_calls = []

    try:
        db.connect = lambda path=None: _orig_connect(tmp)

        # S1: irmã decaída (o mecanismo dos 86%) — killer natural
        # edge_decay_prejudge; a outra perna vira sibling_failed; ZERO
        # judge_reviews (nenhum gasto).
        k1 = seed("g1", "central")
        v1 = seed("g1", "below")
        mod._recheck_edge_prejudge = lambda leg: (
            {"entry_id": leg["entry_id"], "reason": "edge_decay_prejudge",
             "current_edge_pp": 1.0} if leg["entry_id"] == k1 else None)
        mod._judge_route = lambda leg: None
        r = mod._group_free_guard_sweep("g1", v1)
        assert r["dead"] is True and r["killer_entry_id"] == k1, r
        assert r["killer_reason"] == "edge_decay_prejudge", r
        sk = status_of(k1)
        assert sk["status"] == "SKIPPED" and \
            sk["skip_reason"] == "edge_decay_prejudge", sk
        sv = status_of(v1)
        assert sv["status"] == "SKIPPED" and \
            sv["skip_reason"] == "ladder_sibling_failed" and \
            sv["judge_skipped_reason"] == "ladder_sibling_failed_prejudge", sv
        assert n_reviews() == 0
        print("Test W1 PASS: irmã decaída → killer natural + vítima marcada, "
              "$0 (zero judge_reviews)")

        # S2: irmã auto_reject por proximidade — killer REJECTED via
        # apply_verdict real (linha em judge_reviews com custo $0), vítima
        # skipped.
        k2 = seed("g2", "central")
        v2 = seed("g2", "below")
        mod._recheck_edge_prejudge = lambda leg: None
        mod._judge_route = lambda leg: (
            {"action": "auto_reject",
             "reason": "forecast within 1.0 of threshold"}
            if leg["entry_id"] == k2 else None)
        r = mod._group_free_guard_sweep("g2", v2)
        assert r["dead"] is True and \
            r["killer_reason"] == "auto_reject:proximity", r
        assert status_of(k2)["status"] == "REJECTED", status_of(k2)
        assert status_of(v2)["skip_reason"] == "ladder_sibling_failed"
        assert n_reviews() == 1   # a review $0 do killer — desfecho natural
        with db.connect() as conn:
            cost = conn.execute("SELECT COALESCE(cost_usd, 0) FROM "
                                "judge_reviews").fetchone()[0]
        assert float(cost or 0) == 0.0, cost
        print("Test W2 PASS: irmã auto_reject → killer REJECTED ($0), "
              "vítima marcada")

        # S3: grupo de 3 pernas todas passando → dead=False, ids em
        # prejudge_passed_ids, nenhum write.
        a = seed("g3", "central")
        b = seed("g3", "below")
        c = seed("g3", "above")
        mod._recheck_edge_prejudge = lambda leg: None
        mod._judge_route = lambda leg: None
        before = n_reviews()
        r = mod._group_free_guard_sweep("g3", a)
        assert r["dead"] is False
        assert r["prejudge_passed_ids"] == {a, b, c}, r
        assert all(status_of(x)["status"] == "PROPOSED" for x in (a, b, c))
        assert n_reviews() == before
        print("Test W3 PASS: grupo saudável → dead=False, 3 ids passados, "
              "zero writes")

        # S4: irmã roteia auto_approve → NÃO agido na sweep (sem status
        # change, sem judge_reviews) — nenhum gate de qualidade enfraquecido.
        d = seed("g4", "central")
        e = seed("g4", "below")
        mod._judge_route = lambda leg: {"action": "auto_approve",
                                        "reason": "tight ensemble"}
        before = n_reviews()
        r = mod._group_free_guard_sweep("g4", d)
        assert r["dead"] is False
        assert status_of(d)["status"] == "PROPOSED"
        assert status_of(e)["status"] == "PROPOSED"
        assert n_reviews() == before
        print("Test W4 PASS: auto_approve NÃO é concedido na sweep "
              "(perna julga no próprio turno)")

        # S5: irmã já APPROVED → sweep só checa as PROPOSED.
        f = seed("g5", "central", status="APPROVED")
        g = seed("g5", "below")
        recheck_calls.clear()
        mod._recheck_edge_prejudge = lambda leg: (
            recheck_calls.append(leg["entry_id"]) or None)
        mod._judge_route = lambda leg: None
        r = mod._group_free_guard_sweep("g5", g)
        assert r["dead"] is False and recheck_calls == [g], recheck_calls
        assert status_of(f)["status"] == "APPROVED"
        print("Test W5 PASS: perna APPROVED ignorada (sweep só PROPOSED)")

        # S6: guard lançando exceção → fail-open (conta como passou).
        h = seed("g6", "central")
        i = seed("g6", "below")
        def boom(leg):
            raise RuntimeError("orderbook fetch failed")
        mod._recheck_edge_prejudge = boom
        mod._judge_route = lambda leg: None
        r = mod._group_free_guard_sweep("g6", h)
        assert r["dead"] is False, r
        assert status_of(h)["status"] == "PROPOSED"
        assert status_of(i)["status"] == "PROPOSED"
        print("Test W6 PASS: erro no guard → fail-open (grupo vivo)")

        print("\nAll --test-group-sweep PASS (6/6)")
    finally:
        db.connect = _orig_connect
        mod._recheck_edge_prejudge = saved_recheck
        mod._judge_route = saved_route


def _test_prejudge_recheck():
    """Hermetic tests for _recheck_edge_prejudge by stubbing _fetch_orderbook."""
    import weather_edge_judge as mod  # self-import for monkey-patching the module-level helper

    saved_fetch = mod._fetch_orderbook
    saved_threshold = mod.PREJUDGE_MIN_EDGE_PP
    mod.PREJUDGE_MIN_EDGE_PP = 15.0
    try:
        # DB convention: forecast_prob_at_entry is P(side), and the recheck's
        # best_ask is the ask of the chosen side's token (= implied P(side)).
        # Edge math is therefore P(side) - best_ask, with NO side-flip.

        # Test 1: YES side, fresh edge well above 15pp threshold → proceed
        # fp=0.90 (P(YES)=0.90), ask=0.55 → edge = 35pp
        mod._fetch_orderbook = lambda tid: {"asks": [{"price": 0.55, "size": 100}]}
        r = mod._recheck_edge_prejudge({
            "entry_id": 1, "side": "YES",
            "token_id_yes": "tokY", "token_id_no": "tokN",
            "forecast_prob_at_entry": 0.90, "edge_pp_at_entry": 35.0,
        })
        assert r is None, f"expected None (edge=35pp >= 15pp), got {r}"
        print("Test 1 PASS: YES side fresh edge 35pp proceeds to judge")

        # Test 2: YES side, decayed below threshold → skip
        # fp=0.90, ask=0.80 → edge = 10pp < 15pp
        mod._fetch_orderbook = lambda tid: {"asks": [{"price": 0.80, "size": 100}]}
        r = mod._recheck_edge_prejudge({
            "entry_id": 2, "side": "YES",
            "token_id_yes": "tokY", "token_id_no": "tokN",
            "forecast_prob_at_entry": 0.90, "edge_pp_at_entry": 35.0,
        })
        assert r is not None, "expected skip payload"
        assert r["reason"] == "edge_decay_prejudge"
        assert abs(r["current_edge_pp"] - 10.0) < 0.01, r
        assert r["best_ask"] == 0.80
        assert r["side"] == "YES"
        print("Test 2 PASS: YES side decayed edge 10pp → skip")

        # Test 3: NO side uses fp directly (P(side) convention).
        # Discovery stored fp=0.90 meaning P(NO)=0.90. ask of NO token=0.69.
        # Edge = 0.90 - 0.69 = 21pp >= 15pp → proceed.
        # This matches the snapshot pattern (#716: fp=0.90 NO, entry=0.69, edge=21pp).
        mod._fetch_orderbook = lambda tid: {"asks": [{"price": 0.69, "size": 100}]}
        r = mod._recheck_edge_prejudge({
            "entry_id": 3, "side": "NO",
            "token_id_yes": "tokY", "token_id_no": "tokN",
            "forecast_prob_at_entry": 0.90, "edge_pp_at_entry": 21.0,
        })
        assert r is None, f"NO side edge=21pp should proceed (got {r})"
        print("Test 3 PASS: NO side P(side) convention — edge 21pp proceeds")

        # Test 4: NO side decayed → skip. ask moved 0.69 → 0.80.
        mod._fetch_orderbook = lambda tid: {"asks": [{"price": 0.80, "size": 100}]}
        r = mod._recheck_edge_prejudge({
            "entry_id": 4, "side": "NO",
            "token_id_yes": "tokY", "token_id_no": "tokN",
            "forecast_prob_at_entry": 0.90, "edge_pp_at_entry": 21.0,
        })
        assert r is not None, "expected skip"
        assert abs(r["current_edge_pp"] - 10.0) < 0.01, r
        print("Test 4 PASS: NO side decay (21pp -> 10pp) → skip")

        # Test 5: Empty / missing orderbook → fail-open (proceed to judge)
        mod._fetch_orderbook = lambda tid: {"asks": []}
        r = mod._recheck_edge_prejudge({
            "entry_id": 5, "side": "YES",
            "token_id_yes": "tokY", "token_id_no": "tokN",
            "forecast_prob_at_entry": 0.90, "edge_pp_at_entry": 35.0,
        })
        assert r is None, "empty orderbook should fail-open"
        mod._fetch_orderbook = lambda tid: None
        r = mod._recheck_edge_prejudge({
            "entry_id": 5, "side": "YES",
            "token_id_yes": "tokY", "token_id_no": "tokN",
            "forecast_prob_at_entry": 0.90, "edge_pp_at_entry": 35.0,
        })
        assert r is None, "fetch failure should fail-open"
        print("Test 5 PASS: missing orderbook → fail-open")

        # Test 6: Missing forecast_prob_at_entry → fail-open
        mod._fetch_orderbook = lambda tid: {"asks": [{"price": 0.55, "size": 100}]}
        r = mod._recheck_edge_prejudge({
            "entry_id": 6, "side": "YES",
            "token_id_yes": "tokY", "token_id_no": "tokN",
            "forecast_prob_at_entry": None, "edge_pp_at_entry": 35.0,
        })
        assert r is None, "missing forecast should fail-open"
        print("Test 6 PASS: missing forecast_prob_at_entry → fail-open")

        # Test 7: Threshold boundary — exactly at threshold passes (>= comparison)
        mod._fetch_orderbook = lambda tid: {"asks": [{"price": 0.75, "size": 100}]}
        r = mod._recheck_edge_prejudge({
            "entry_id": 7, "side": "YES",
            "token_id_yes": "tokY", "token_id_no": "tokN",
            "forecast_prob_at_entry": 0.90, "edge_pp_at_entry": 20.0,
        })
        # edge = 0.90 - 0.75 = 15pp, threshold = 15pp → proceeds (>=)
        assert r is None, f"edge exactly at threshold should proceed: {r}"
        print("Test 7 PASS: edge = threshold proceeds (>=)")

        # --- v15.1: piso leg-aware -----------------------------------------
        saved_ladder_floor = mod.PREJUDGE_MIN_EDGE_PP_LADDER
        mod.PREJUDGE_MIN_EDGE_PP_LADDER = 10.0
        try:
            # Test 8: perna de LADDER com edge 12pp — abaixo do piso single
            # (15) mas acima do piso de ladder (10) → PROCEDE. Era o caso
            # DOA do post-mortem (93% dos skips): proposta a 10-15pp e
            # pulada para sempre, mesmo com o edge melhorando.
            mod._fetch_orderbook = lambda tid: {
                "asks": [{"price": 0.78, "size": 100}]}
            ladder_row = {
                "entry_id": 8, "side": "YES",
                "token_id_yes": "tokY", "token_id_no": "tokN",
                "forecast_prob_at_entry": 0.90, "edge_pp_at_entry": 13.0,
                "ladder_group_id": "g-ladder",
            }
            r = mod._recheck_edge_prejudge(ladder_row)   # edge = 12pp
            assert r is None, f"ladder leg 12pp >= floor 10 must proceed: {r}"
            # mesma linha SEM ladder_group_id → piso 15 → skip (regressão)
            single_row = {k: v for k, v in ladder_row.items()
                          if k != "ladder_group_id"}
            r = mod._recheck_edge_prejudge(single_row)
            assert r is not None and r["threshold_pp"] == 15.0, r
            print("Test 8 PASS: 12pp — perna de ladder procede (piso 10); "
                  "single-bin pula (piso 15)")

            # Test 9: perna de ladder com edge 8pp → skip com o piso de
            # ladder reportado no payload.
            mod._fetch_orderbook = lambda tid: {
                "asks": [{"price": 0.82, "size": 100}]}
            r = mod._recheck_edge_prejudge(ladder_row)   # edge = 8pp
            assert r is not None, "8pp < 10 deve pular"
            assert r["threshold_pp"] == 10.0, r
            assert abs(r["current_edge_pp"] - 8.0) < 0.01, r
            print("Test 9 PASS: perna de ladder 8pp < 10 → skip, "
                  "threshold_pp=10 no payload")

            # Test 10: override por env (constante) respeitado.
            mod.PREJUDGE_MIN_EDGE_PP_LADDER = 5.0
            r = mod._recheck_edge_prejudge(ladder_row)   # edge 8pp >= 5
            assert r is None, f"floor 5: 8pp deve proceder: {r}"
            print("Test 10 PASS: piso de ladder configurável respeitado")
        finally:
            mod.PREJUDGE_MIN_EDGE_PP_LADDER = saved_ladder_floor

        print("\nAll pre-judge recheck tests PASS (10/10)")
    finally:
        mod._fetch_orderbook = saved_fetch
        mod.PREJUDGE_MIN_EDGE_PP = saved_threshold


# ---------------------------------------------------------------------------
# Inline tests for the v13.2 conditional-gate routing (_judge_route)
# Run: python weather_edge_judge.py --test-autoroute
# ---------------------------------------------------------------------------

def _test_autoroute():
    """Hermetic tests for _judge_route by monkey-patching the helper imports
    (parse_market/forecast_ref_value/load_cities) and the module-level
    _threshold_proximity_reason. Verifies the routing decision only — the
    downstream apply_verdict guards have their own tests."""
    import types
    import weather_edge_judge as mod
    import weather_edge_helpers as helpers

    saved = {
        "prox": mod._threshold_proximity_reason,
        "parse": helpers.parse_market,
        "ref": helpers.forecast_ref_value,
        "cities": helpers.load_cities,
        "autoroute": mod.JUDGE_AUTOROUTE,
        "mult": mod.AUTOAPPROVE_MAE_MULT,
    }

    def _spec(metric="temp", comparison="range", lo=20.0, hi=21.0, unit="C"):
        return types.SimpleNamespace(
            metric=metric, comparison=comparison,
            threshold_value=lo, threshold_value_high=hi, threshold_unit=unit)

    def _row(ref, sigma, ensemble=True, question="temp?", side="NO"):
        dm = {"ensemble_calibrated": ensemble, "mae_dynamic": sigma} if ensemble else {}
        return {
            "entry_id": 1, "market_question": question, "end_date": "2026-07-02",
            "side": side, "forecast_prob_at_entry": 0.85,
            "forecast_snapshot_json": json.dumps({"ref": ref}),
            "discovery_meta_json": json.dumps(dm),
        }

    try:
        mod.JUDGE_AUTOROUTE = True
        mod.AUTOAPPROVE_MAE_MULT = 2.0
        helpers.load_cities = lambda: {}
        # ref is read straight out of the fake snapshot for determinism.
        helpers.forecast_ref_value = lambda spec, fc: fc.get("ref")

        # Test 1: proximity fires → auto_reject, no LLM (regardless of ensemble)
        mod._threshold_proximity_reason = lambda row: "within 1° of range"
        helpers.parse_market = lambda q, e, c: _spec()
        r = mod._judge_route(_row(ref=25.0, sigma=1.0))
        assert r and r["action"] == "auto_reject", r
        print("Test 1 PASS: proximity → auto_reject (LLM skipped)")

        # From here on, proximity never fires.
        mod._threshold_proximity_reason = lambda row: None

        # Test 2: tight ensemble, bin far (dist=4°, σ=1 → 4σ ≥ 2σ) → auto_approve
        helpers.parse_market = lambda q, e, c: _spec(lo=20.0, hi=21.0)
        r = mod._judge_route(_row(ref=25.0, sigma=1.0))  # ref 25 > hi 21 → dist=4
        assert r and r["action"] == "auto_approve", r
        assert abs(r["dist"] - 4.0) < 1e-9, r
        print("Test 2 PASS: tight ensemble far from bin → auto_approve (LLM skipped)")

        # Test 3: ensemble but bin NEAR (dist=1.5°, σ=1 → 1.5σ < 2σ) → LLM
        r = mod._judge_route(_row(ref=22.5, sigma=1.0))  # dist = 22.5-21 = 1.5
        assert r is None, r
        print("Test 3 PASS: ensemble near-edge → None (routed to LLM)")

        # Test 4: forecast INSIDE the bin (risky YES) → dist 0 → LLM
        r = mod._judge_route(_row(ref=20.5, sigma=1.0))
        assert r is None, r
        print("Test 4 PASS: forecast inside bin → None (routed to LLM)")

        # Test 5: non-ensemble (single-source fallback) far from bin → LLM
        r = mod._judge_route(_row(ref=25.0, sigma=1.0, ensemble=False))
        assert r is None, r
        print("Test 5 PASS: non-ensemble fallback → None (LLM earns its cost)")

        # Test 6 (v14): non-temp market → fail-closed deterministic REJECT
        # (never reaches the LLM, which could approve it)
        helpers.parse_market = lambda q, e, c: _spec(metric="rain")
        r = mod._judge_route(_row(ref=25.0, sigma=1.0))
        assert r and r["action"] == "auto_reject", r
        assert r.get("kind") == "non_temperature", r
        print("Test 6 PASS: non-temp market → auto_reject (fail-closed)")

        # Test 7: boundary dist == 2σ exactly → auto_approve (>= threshold)
        helpers.parse_market = lambda q, e, c: _spec(lo=20.0, hi=21.0)
        r = mod._judge_route(_row(ref=23.0, sigma=1.0))  # dist = 2.0 = 2σ
        assert r and r["action"] == "auto_approve", f"dist==2σ should approve: {r}"
        print("Test 7 PASS: dist == 2σ exactly → auto_approve (>=)")

        # Test 8: threshold (non-range) market far → auto_approve
        helpers.parse_market = lambda q, e, c: _spec(
            comparison="above", lo=30.0, hi=None)
        r = mod._judge_route(_row(ref=25.0, sigma=1.0))  # |25-30| = 5 ≥ 2
        assert r and r["action"] == "auto_approve", r
        print("Test 8 PASS: threshold market far from cutoff → auto_approve")

        # Test 9: JUDGE_AUTOROUTE off → always None (universal-LLM restored)
        mod.JUDGE_AUTOROUTE = False
        helpers.parse_market = lambda q, e, c: _spec(lo=20.0, hi=21.0)
        r = mod._judge_route(_row(ref=25.0, sigma=1.0))
        assert r is None, r
        print("Test 9 PASS: JUDGE_AUTOROUTE=0 disables routing (always LLM)")

        # Test 10 (v14): temperature-only policy survives JUDGE_AUTOROUTE=0 —
        # a non-temp proposal is still deterministically rejected (fail-closed
        # policy, not a routing optimization). JUDGE_AUTOROUTE is still False
        # here from Test 9.
        helpers.parse_market = lambda q, e, c: _spec(metric="precip")
        r = mod._judge_route(_row(ref=25.0, sigma=1.0))
        assert r and r["action"] == "auto_reject", r
        assert r.get("kind") == "non_temperature", r
        print("Test 10 PASS: JUDGE_AUTOROUTE=0 still auto-rejects non-temp")

        print("\nAll auto-route tests PASS (10/10)")
    finally:
        mod._threshold_proximity_reason = saved["prox"]
        helpers.parse_market = saved["parse"]
        helpers.forecast_ref_value = saved["ref"]
        helpers.load_cities = saved["cities"]
        mod.JUDGE_AUTOROUTE = saved["autoroute"]
        mod.AUTOAPPROVE_MAE_MULT = saved["mult"]


# ---------------------------------------------------------------------------
# Inline tests for the v13.2 option-1 anomaly scan (parse + outcome)
# Run: python weather_edge_judge.py --test-anomaly
# ---------------------------------------------------------------------------

def _test_anomaly():
    """Hermetic tests for _parse_scan_json and _scan_outcome — no API calls."""

    # --- _parse_scan_json ---
    r = _parse_scan_json('```json\n{"catalyst_found": true, "summary": "heat dome inbound"}\n```')
    assert r == {"catalyst_found": True, "summary": "heat dome inbound"}, r
    print("Test 1 PASS: fenced json block parsed")

    r = _parse_scan_json('Here is my finding:\n{"catalyst_found": false, "summary": "routine"}')
    assert r == {"catalyst_found": False, "summary": "routine"}, r
    print("Test 2 PASS: bare object after prose parsed")

    r = _parse_scan_json('```\n{"catalyst_found": false, "summary": "no fence lang"}\n```')
    assert r == {"catalyst_found": False, "summary": "no fence lang"}, r
    print("Test 3 PASS: unlabelled fence parsed")

    assert _parse_scan_json("") is None
    assert _parse_scan_json("no json here at all") is None
    assert _parse_scan_json('{"summary": "missing the key"}') is None
    print("Test 4 PASS: empty / no-json / missing-key → None")

    # truthiness coercion (string "true" is truthy → True; but json bool stays)
    r = _parse_scan_json('{"catalyst_found": 1, "summary": "coerced"}')
    assert r["catalyst_found"] is True, r
    print("Test 5 PASS: non-bool catalyst_found coerced to bool")

    # --- _scan_outcome ---
    assert _scan_outcome(None) == "unavailable"
    assert _scan_outcome({"catalyst_found": True, "summary": "x"}) == "escalate"
    assert _scan_outcome({"catalyst_found": False, "summary": "x"}) == "approve"
    print("Test 6 PASS: outcome None→unavailable, True→escalate, False→approve")

    print("\nAll anomaly-scan tests PASS (6/6)")


def _test_brightsky():
    """v15.3: hermetic tests for get_brightsky_forecast + its wiring into
    review_proposal. Monkeypatches requests.get (no network)."""
    import weather_edge_judge as mod

    class FakeResp:
        def __init__(self, status, payload):
            self.status_code = status
            self._p = payload
        def json(self):
            return self._p

    saved_get = mod.requests.get
    try:
        # 1. Série horária válida → max/min extraídos (°C).
        payload = {
            "weather": [
                {"timestamp": "2026-07-09T00:00", "temperature": 14.0},
                {"timestamp": "2026-07-09T12:00", "temperature": 22.5},
                {"timestamp": "2026-07-09T15:00", "temperature": 23.1},
                {"timestamp": "2026-07-09T22:00", "temperature": None},
            ],
            "sources": [{"station_name": "Berlin-Brandenburg", "distance": 3200}],
        }
        mod.requests.get = lambda *a, **k: FakeResp(200, payload)
        r = mod.get_brightsky_forecast(52.36, 13.50, "2026-07-09")
        assert r is not None, "esperava dict"
        assert r["max_c"] == 23.1 and r["min_c"] == 14.0, r
        assert r["n_hours"] == 3, r          # None ignorado
        assert r["provider"] == "dwd_mosmix"
        assert r["source_station"] == "Berlin-Brandenburg"
        print("Test BS1 PASS: série horária → max 23.1 / min 14.0 (None ignorado)")

        # 2. Não-200 → None.
        mod.requests.get = lambda *a, **k: FakeResp(404, {})
        assert mod.get_brightsky_forecast(0, 0, "2026-07-09") is None
        print("Test BS2 PASS: HTTP não-200 → None")

        # 3. Série vazia / sem temperaturas → None.
        mod.requests.get = lambda *a, **k: FakeResp(200, {"weather": [], "sources": []})
        assert mod.get_brightsky_forecast(0, 0, "2026-07-09") is None
        mod.requests.get = lambda *a, **k: FakeResp(
            200, {"weather": [{"temperature": None}], "sources": []})
        assert mod.get_brightsky_forecast(0, 0, "2026-07-09") is None
        print("Test BS3 PASS: sem temperaturas → None")

        # 4. Exceção de rede → None (fail-open, nunca lança).
        def boom(*a, **k):
            raise RuntimeError("network down")
        mod.requests.get = boom
        assert mod.get_brightsky_forecast(0, 0, "2026-07-09") is None
        print("Test BS4 PASS: exceção → None (fail-open)")

        # 5. Wiring em review_proposal: coords resolvem, Bright Sky OK →
        #    evidence["dwd_mosmix"] preenchido; sem chamar o LLM (stub call_claude).
        saved_geo = mod._geocode_city
        saved_nws = mod.get_nws_forecast
        saved_vc = mod.get_visual_crossing
        saved_call = mod.call_claude
        saved_bs = mod.get_brightsky_forecast
        try:
            mod._geocode_city = lambda city: (48.97, 2.44)   # Paris
            mod.get_nws_forecast = lambda lat, lon: None       # US-only → None na EU
            mod.get_visual_crossing = lambda city, date: None
            mod.get_brightsky_forecast = lambda lat, lon, d: {
                "provider": "dwd_mosmix", "max_c": 25.0, "min_c": 15.0,
                "n_hours": 24, "source_station": "Paris"}
            captured = {}
            mod.call_claude = lambda entry, evidence, sp: captured.update(
                evidence=evidence) or {"verdict": "APPROVE", "_meta": {}}
            entry = {"entry_id": 1, "city_resolved": "Paris",
                     "end_date": "2026-07-09T12:00:00Z"}
            mod.review_proposal(entry, "sys")
            assert "dwd_mosmix" in captured["evidence"], captured
            assert "nws" not in captured["evidence"]
            assert captured["evidence"]["dwd_mosmix"]["max_c"] == 25.0
            print("Test BS5 PASS: review_proposal injeta evidence['dwd_mosmix'] "
                  "na Europa (NWS ausente)")
        finally:
            mod._geocode_city = saved_geo
            mod.get_nws_forecast = saved_nws
            mod.get_visual_crossing = saved_vc
            mod.call_claude = saved_call
            mod.get_brightsky_forecast = saved_bs

        print("\nAll --test-brightsky PASS (5/5)")
    finally:
        mod.requests.get = saved_get


def _test_ipma():
    """v16: hermetic tests for get_ipma_forecast (monkeypatch requests.get)."""
    import weather_edge_judge as mod

    class FakeResp:
        def __init__(self, status, payload):
            self.status_code = status
            self._p = payload
        def json(self):
            return self._p

    saved = mod.requests.get
    try:
        payload = {"data": [
            {"forecastDate": "2026-07-09", "tMax": "31.4", "tMin": "18.2"},
            {"forecastDate": "2026-07-10", "tMax": "30.0", "tMin": "17.5"}]}
        mod.requests.get = lambda *a, **k: FakeResp(200, payload)
        r = mod.get_ipma_forecast("Lisbon", "2026-07-09")
        assert r and r["provider"] == "ipma", r
        assert r["max_c"] == 31.4 and r["min_c"] == 18.2, r
        print("Test IPMA1 PASS: Lisboa 2026-07-09 → max 31.4 / min 18.2")

        # cidade sem globalIdLocal → None, sem chamar HTTP
        called = {"n": 0}
        mod.requests.get = lambda *a, **k: called.__setitem__("n", called["n"]+1) or FakeResp(200, payload)
        assert mod.get_ipma_forecast("Madrid", "2026-07-09") is None
        assert called["n"] == 0
        print("Test IPMA2 PASS: cidade fora de Portugal → None sem HTTP")

        # data ausente no payload → None
        mod.requests.get = lambda *a, **k: FakeResp(200, payload)
        assert mod.get_ipma_forecast("Porto", "2026-12-31") is None
        # não-200 e exceção → None
        mod.requests.get = lambda *a, **k: FakeResp(500, {})
        assert mod.get_ipma_forecast("Lisbon", "2026-07-09") is None
        def boom(*a, **k): raise RuntimeError("net")
        mod.requests.get = boom
        assert mod.get_ipma_forecast("Lisbon", "2026-07-09") is None
        print("Test IPMA3 PASS: data ausente / não-200 / exceção → None")
        print("\nAll --test-ipma PASS")
    finally:
        mod.requests.get = saved


def _test_metno():
    """v16: hermetic tests for get_metno_forecast (monkeypatch requests.get)."""
    import weather_edge_judge as mod

    class FakeResp:
        def __init__(self, status, payload):
            self.status_code = status
            self._p = payload
        def json(self):
            return self._p

    saved = mod.requests.get
    try:
        payload = {"properties": {"timeseries": [
            {"time": "2026-07-09T00:00:00Z", "data": {"instant": {"details": {"air_temperature": 12.0}}}},
            {"time": "2026-07-09T14:00:00Z", "data": {"instant": {"details": {"air_temperature": 22.4}}}},
            {"time": "2026-07-10T14:00:00Z", "data": {"instant": {"details": {"air_temperature": 99.0}}}}]}}
        mod.requests.get = lambda *a, **k: FakeResp(200, payload)
        r = mod.get_metno_forecast(59.91, 10.75, "2026-07-09")
        assert r and r["provider"] == "metno", r
        assert r["max_c"] == 22.4 and r["min_c"] == 12.0, r   # 10-jul (99) excluído
        assert r["n_hours"] == 2, r
        print("Test METNO1 PASS: só horas do dia-alvo (max 22.4 / min 12.0)")

        mod.requests.get = lambda *a, **k: FakeResp(403, {})   # sem UA → 403
        assert mod.get_metno_forecast(0, 0, "2026-07-09") is None
        mod.requests.get = lambda *a, **k: FakeResp(200, {"properties": {"timeseries": []}})
        assert mod.get_metno_forecast(0, 0, "2026-07-09") is None
        def boom(*a, **k): raise RuntimeError("net")
        mod.requests.get = boom
        assert mod.get_metno_forecast(0, 0, "2026-07-09") is None
        print("Test METNO2 PASS: 403 / vazio / exceção → None (fail-open)")
        print("\nAll --test-metno PASS")
    finally:
        mod.requests.get = saved


def _test_cc_route():
    """v11: _judge_route must auto-approve cheap_convexity on deterministic
    guards WITHOUT the LLM, auto-reject bad ones, fail-safe on corrupt meta,
    and leave legacy (non-cc) rows on their normal routing path. The policy
    runs even with JUDGE_AUTOROUTE=0."""
    import types
    import weather_edge_judge as mod
    import weather_edge_helpers as helpers

    saved = (helpers.parse_market, helpers.load_cities,
             helpers.is_tradeable_spec, mod.JUDGE_AUTOROUTE)

    def _temp_spec():
        return types.SimpleNamespace(
            metric="temp", comparison="range", threshold_value=14.0,
            threshold_value_high=15.0, threshold_unit="C")

    def _row(strategy, meta, entry_price=0.02):
        return {
            "entry_id": 1, "market_question": "temp?", "end_date": "2026-07-09",
            "side": "YES", "entry_price": entry_price,
            "forecast_prob_at_entry": 0.07,
            "forecast_snapshot_json": json.dumps({}),
            "discovery_meta_json": meta,
            "strategy": strategy,
        }

    try:
        # (0) temp-only step must pass through so we reach (0.5).
        helpers.parse_market = lambda q, e, c: _temp_spec()
        helpers.load_cities = lambda: {}
        helpers.is_tradeable_spec = lambda s: True

        # CC-R1: valid cheap_convexity → auto_approve, no LLM.
        r = mod._judge_route(_row("cheap_convexity", json.dumps(
            {"fair_target": 0.07, "exit_liquidity_shares": 500})))
        assert r and r["action"] == "auto_approve", r
        assert r["kind"] == "cheap_convexity", r
        print("Test CC-R1 PASS: valid cheap_convexity → auto_approve (no LLM)")

        # CC-R2: fair <= price → auto_reject.
        r = mod._judge_route(_row("cheap_convexity", json.dumps(
            {"fair_target": 0.02, "exit_liquidity_shares": 500})))
        assert r and r["action"] == "auto_reject" and "price" in r["reason"], r
        print("Test CC-R2 PASS: fair <= price → auto_reject")

        # CC-R3: no exit liquidity → auto_reject.
        r = mod._judge_route(_row("cheap_convexity", json.dumps(
            {"fair_target": 0.07, "exit_liquidity_shares": 0})))
        assert r and r["action"] == "auto_reject" and "liquidity" in r["reason"], r
        print("Test CC-R3 PASS: no exit liquidity → auto_reject")

        # CC-R4: corrupt discovery_meta_json → None (fail-safe → LLM).
        r = mod._judge_route(_row("cheap_convexity", "{bad json"))
        assert r is None, r
        print("Test CC-R4 PASS: corrupt meta → None (fail-safe to LLM)")

        # CC-R5: legacy (strategy None) does NOT enter cc branch. With a temp
        # spec, no proximity, non-ensemble → normal routing returns None (LLM).
        mod._threshold_proximity_reason = lambda row: None
        r = mod._judge_route(_row(None, json.dumps({})))
        assert r is None or r.get("kind") != "cheap_convexity", r
        print("Test CC-R5 PASS: legacy row not routed as cheap_convexity")

        # CC-R6: policy survives JUDGE_AUTOROUTE=0 (runs before that gate).
        mod.JUDGE_AUTOROUTE = False
        r = mod._judge_route(_row("cheap_convexity", json.dumps(
            {"fair_target": 0.07, "exit_liquidity_shares": 500})))
        assert r and r["action"] == "auto_approve", r
        print("Test CC-R6 PASS: cheap_convexity routes even with AUTOROUTE=0")

        print("\nAll cheap_convexity route tests PASS")
    finally:
        (helpers.parse_market, helpers.load_cities,
         helpers.is_tradeable_spec, mod.JUDGE_AUTOROUTE) = saved


if __name__ == "__main__":
    import sys
    if "--test-rule6" in sys.argv:
        _test_rule6_enforce()
    elif "--test-sibling-gate" in sys.argv:
        _test_sibling_gate()
    elif "--test-group-sweep" in sys.argv:
        _test_group_sweep()
    elif "--test-prejudge" in sys.argv:
        _test_prejudge_recheck()
    elif "--test-autoroute" in sys.argv:
        _test_autoroute()
    elif "--test-anomaly" in sys.argv:
        _test_anomaly()
    elif "--test-cc-route" in sys.argv:
        _test_cc_route()
    elif "--test-ipma" in sys.argv:
        _test_ipma()
    elif "--test-metno" in sys.argv:
        _test_metno()
    elif "--test-brightsky" in sys.argv:
        _test_brightsky()
    else:
        main()
