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
    evaluate_cashout_triggers, forecast_ref_value, prob_yes_for_sizing,
    # v7: dynamic MAE + multi-source consensus
    compute_dynamic_mae, fetch_visual_crossing,
    MAE_TEMP_F, MAE_TEMP_C,
    # v8: resolution stations + Open-Meteo ensemble
    resolve_station, fetch_open_meteo_ensemble,
    # v9: auto-extract station from market description
    auto_extract_station,
    # v11: realized weather for resolutions.observed_value
    fetch_open_meteo_archive,
    # v13: NGR/EMOS-style ensemble calibration
    compute_ensemble_calibration,
    # v14: temperature-only policy
    is_tradeable_spec,
    # v11 cheap_convexity: raw (unclipped) fair for 1-20c tail bins
    _forecast_probability_raw,
)
import weather_edge_db as db  # noqa: E402

LOG_DIR = Path.home() / ".polymarket-paper"
LOG_FILE = LOG_DIR / "weather_edge.jsonl"  # default; overridable via --log-file

GAMMA_API = "https://gamma-api.polymarket.com"
WEATHER_TAG = "weather"
FORECAST_SCRIPT = REPO_ROOT / "polymarket-forecast-skill" / "scripts" / "get_weather.py"

# Keywords used to client-side filter markets to weather-related ones.
# (Gamma's tag_slug=weather param is silently ignored, so we filter ourselves.)
import re as _re
_WEATHER_KEYWORDS = _re.compile(
    r"\b(weather|temperature|temp|rain|rainfall|snow|snowfall|"
    r"precipitation|precip|hurricane|storm|wind|fahrenheit|celsius|"
    r"hottest|coldest|warmest|coolest|degrees|°[fc]|inches\s+of|"
    r"mm\s+of|cm\s+of)\b",
    _re.IGNORECASE,
)

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
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"[log-error] {e}", file=sys.stderr, flush=True)
    print(line, flush=True)


# ---------------------------------------------------------------------------
# Gamma + forecast helpers
# ---------------------------------------------------------------------------


def _fetch_weather_events(now: datetime, max_pages: int = 3) -> list[dict]:
    """Fetch weather-tagged events from /events and flatten to their markets.

    Some Polymarket weather markets only appear via the /events endpoint
    (multi-outcome events). Each event contains a `markets` array of binary
    sub-markets (one per bracket).
    """
    now_iso = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    out: list[dict] = []
    # Try a few param variations
    for params_try in [
        {"active": "true", "closed": "false", "end_date_min": now_iso,
         "tag_slug": "weather", "limit": 100},
        {"active": "true", "closed": "false", "end_date_min": now_iso,
         "tag": "weather", "limit": 100},
        # Search by query (events with weather/temperature in title)
        {"active": "true", "closed": "false", "end_date_min": now_iso,
         "q": "temperature", "limit": 100},
    ]:
        for page in range(max_pages):
            p = {**params_try, "offset": page * 100}
            try:
                r = requests.get(f"{GAMMA_API}/events", params=p, timeout=30)
                r.raise_for_status()
                events = r.json()
            except requests.exceptions.RequestException as e:
                log_event("error", {"where": "_fetch_weather_events",
                                     "params": list(params_try.keys()),
                                     "page": page, "err": str(e)}, level="WARN")
                break
            if not isinstance(events, list) or not events:
                break
            for ev in events:
                ev_title = ev.get("title", "")
                ev_slug = ev.get("slug", "")
                # Each event has a `markets` array of binary sub-markets
                sub_markets = ev.get("markets") or []
                for sm in sub_markets:
                    if not isinstance(sm, dict):
                        continue
                    # Inject parent event context so the parser can resolve city
                    sm["events"] = sm.get("events") or [{"title": ev_title,
                                                          "slug": ev_slug}]
                    # v9: preserve event slug directly so the ladder builder
                    # can group sibling brackets after parsing
                    sm["event_slug"] = ev_slug
                    out.append(sm)
            if len(events) < 100:
                break
        if out:
            log_event("events_endpoint_hit", {"variant": list(params_try.keys()),
                                                "events_returned": len(out)})
            return out
    return out


def _fetch_with_params(params: dict, max_pages: int = 5) -> list[dict]:
    """Paginate Gamma /markets with given params. Returns flat list."""
    out: list[dict] = []
    for page in range(max_pages):
        p = {**params, "limit": 100, "offset": page * 100}
        try:
            r = requests.get(f"{GAMMA_API}/markets", params=p, timeout=30)
            r.raise_for_status()
            batch = r.json()
        except requests.exceptions.RequestException as e:
            log_event("error", {"where": "_fetch_with_params", "page": page,
                                "err": str(e)}, level="WARN")
            break
        if not isinstance(batch, list) or not batch:
            break
        out.extend(batch)
        if len(batch) < 100:
            break
    return out


def fetch_weather_markets(min_volume: float = 5000, max_pages: int = 5) -> list[dict]:
    """Fetch markets ending in next ~7 days, filter client-side to weather.

    Strategy: server-side filter `end_date_min=now` so we skip the pile of
    stale 2025 markets that Gamma still returns with closed=false. Try a few
    param-name variants because Gamma's parameter naming changes between
    versions (snake_case vs camelCase).
    """
    now = datetime.now(timezone.utc)
    now_iso = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    # Try multiple known variants of the date-filter param name. First one
    # that returns mostly future-ending markets wins.
    candidate_params = [
        {"active": "true", "closed": "false",
         "order": "endDate", "ascending": "true",
         "end_date_min": now_iso},
        {"active": "true", "closed": "false",
         "order": "endDate", "ascending": "true",
         "endDateMin": now_iso},
        {"active": "true", "closed": "false",
         "order": "endDate", "ascending": "true",
         "start_date_max": now_iso, "end_date_min": now_iso},
        # Last resort: no date filter, hope newer markets come back
        {"active": "true", "closed": "false",
         "order": "endDate", "ascending": "false"},  # newest endDate first
    ]

    all_markets: list[dict] = []
    chosen_param_set = None
    for idx, params in enumerate(candidate_params):
        batch = _fetch_with_params(params, max_pages=max_pages)
        if not batch:
            continue
        # Validate: count how many have endDate > now in the first 50
        future_count = 0
        for m in batch[:50]:
            try:
                ed = datetime.fromisoformat((m.get("endDate") or "").replace("Z", "+00:00"))
                if ed >= now:
                    future_count += 1
            except (ValueError, TypeError):
                pass
        log_event("fetch_attempt", {"variant": idx, "params": list(params.keys()),
                                    "returned": len(batch),
                                    "future_in_first_50": future_count})
        if future_count >= 30:  # accept if most are future
            all_markets = batch
            chosen_param_set = idx
            break

    if not all_markets:
        log_event("fetch_failed", {"reason": "no variant returned mostly future markets"},
                  level="WARN")
        return []
    log_event("fetch_using_variant", {"variant": chosen_param_set,
                                       "total": len(all_markets)})

    # Dump first 3 markets in compact form so we can see what fields exist.
    for i, m in enumerate(all_markets[:3]):
        events = m.get("events") or []
        ev_titles = [e.get("title") for e in events if isinstance(e, dict)]
        log_event("fetch_sample", {
            "i": i,
            "question": (m.get("question") or "")[:120],
            "slug": (m.get("slug") or "")[:80],
            "endDate": m.get("endDate"),
            "volumeNum": m.get("volumeNum"),
            "event_titles": ev_titles[:2],
            "event_slugs": [e.get("slug") for e in events if isinstance(e, dict)][:2],
            "all_keys": sorted(m.keys())[:30],
        })

    # Also try /events endpoint and merge any weather-tagged markets we find
    events_extras = _fetch_weather_events(now)
    if events_extras:
        log_event("events_endpoint_added", {"count": len(events_extras)})
        all_markets.extend(events_extras)

    out = []
    n_keyword_match = 0
    n_past_end = 0
    n_low_vol = 0
    for m in all_markets:
        question = m.get("question", "")
        events = m.get("events") or []
        event_text = " ".join(
            (e.get("title", "") + " " + e.get("slug", ""))
            for e in events if isinstance(e, dict))
        combined = f"{event_text} {question}"
        if not _WEATHER_KEYWORDS.search(combined):
            continue
        n_keyword_match += 1
        # Stash combined text on the market dict for the discovery stage.
        m["_combined_text"] = combined.strip()
        # Skip clearly stale markets (endDate already past)
        try:
            end_date = datetime.fromisoformat(
                (m.get("endDate") or "").replace("Z", "+00:00"))
            if end_date < now:
                n_past_end += 1
                continue
        except (ValueError, TypeError):
            continue
        try:
            vol = float(m.get("volumeNum", 0) or 0)
            if vol < min_volume:
                n_low_vol += 1
                continue
            out.append(m)
        except (ValueError, TypeError):
            continue

    log_event("fetch_summary", {
        "total_fetched": len(all_markets),
        "keyword_matches": n_keyword_match,
        "past_end_skipped": n_past_end,
        "low_volume_skipped": n_low_vol,
        "passed": len(out),
    })
    return out


def fetch_orderbook(token_id: str) -> Optional[dict]:
    """Fetch orderbook directly from CLOB HTTP endpoint (no SDK dependency).

    Endpoint returns: {market, asset_id, bids: [...], asks: [...]} where each
    side has {price, size} entries (as strings).
    """
    try:
        r = requests.get("https://clob.polymarket.com/book",
                         params={"token_id": token_id}, timeout=15)
        if r.status_code != 200:
            log_event("error", {"where": "fetch_orderbook",
                                 "token_id": token_id[:12],
                                 "status": r.status_code}, level="WARN")
            return None
        data = r.json()
        bids = sorted(
            [{"price": float(b["price"]), "size": float(b["size"])}
             for b in (data.get("bids") or [])],
            key=lambda b: b["price"], reverse=True,
        )
        asks = sorted(
            [{"price": float(a["price"]), "size": float(a["size"])}
             for a in (data.get("asks") or [])],
            key=lambda a: a["price"],
        )
        return {"bids": bids, "asks": asks}
    except (requests.exceptions.RequestException, ValueError, KeyError) as e:
        log_event("error", {"where": "fetch_orderbook", "token_id": token_id[:12],
                            "err": str(e)}, level="WARN")
        return None


_FORECAST_KEY_WARNED = False
# Per-process cache of cities that OpenWeather doesn't recognize.
# Avoids re-querying every discovery cycle for cities the parser extracted
# but OpenWeather can't geocode (typos, very small towns, ambiguous names).
_FORECAST_UNKNOWN_CITIES: set[str] = set()


def fetch_forecast(city: str, days: int = 5,
                    lat: Optional[float] = None,
                    lon: Optional[float] = None) -> Optional[dict]:
    """Subprocess get_weather.py forecast and return parsed JSON, or None.

    v8: when `lat`/`lon` are provided, passes --lat/--lon flags to
    get_weather.py so it skips geocoding by city name and pulls
    forecast for those exact coordinates (used for resolution-station
    lookups).

    Caches the set of cities OpenWeather couldn't resolve, so repeat
    discovery cycles don't waste API calls on the same bad names.
    """
    global _FORECAST_KEY_WARNED
    if not os.environ.get("OPENWEATHER_API_KEY"):
        if not _FORECAST_KEY_WARNED:
            log_event("config_missing", {"key": "OPENWEATHER_API_KEY",
                                          "impact": "all forecast lookups will fail"},
                      level="ERROR")
            _FORECAST_KEY_WARNED = True
        return None

    # Lat/lon overrides skip the "unknown city" cache (coords always work
    # regardless of whether OW knows the city name).
    if lat is None and city in _FORECAST_UNKNOWN_CITIES:
        return None

    if not FORECAST_SCRIPT.exists():
        log_event("error", {"where": "fetch_forecast",
                            "err": f"forecast script not found: {FORECAST_SCRIPT}"},
                  level="ERROR")
        return None

    cmd = [sys.executable, str(FORECAST_SCRIPT), "forecast", city, str(days)]
    if lat is not None and lon is not None:
        cmd += ["--lat", str(lat), "--lon", str(lon)]

    try:
        env = {**os.environ}  # ensure subprocess inherits OPENWEATHER_API_KEY
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, env=env,
        )
        if result.returncode != 0:
            # If OpenWeather doesn't recognize the city, cache it so we don't
            # retry every discovery cycle. Heuristic: stderr mentions 404 / not
            # found, or non-zero exit on first try.
            stderr = (result.stderr or "")[:500]
            if any(s in stderr.lower() for s in ("404", "not found",
                                                  "city not found")):
                _FORECAST_UNKNOWN_CITIES.add(city)
            log_event("error", {"where": "fetch_forecast", "city": city,
                                "exit_code": result.returncode,
                                "stderr": stderr,
                                "stdout": result.stdout[:200],
                                "cached_as_unknown": city in _FORECAST_UNKNOWN_CITIES},
                      level="WARN")
            return None
        out = result.stdout.strip()
        if not out:
            log_event("error", {"where": "fetch_forecast", "city": city,
                                "err": "empty stdout from get_weather.py"}, level="WARN")
            return None
        return json.loads(out)
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        log_event("error", {"where": "fetch_forecast", "city": city,
                            "err": str(e), "type": type(e).__name__}, level="WARN")
        return None


# ---------------------------------------------------------------------------
# Discovery cycle: scan + propose entries
# ---------------------------------------------------------------------------


def _compute_mae_for_market(spec: MarketSpec, ow_forecast: dict,
                              args,
                              station: Optional[dict] = None
                              ) -> tuple[Optional[float], Optional[float],
                                         Optional[float], dict]:
    """v7+v8+v13: returns (mae_override, bias_override, mu_override, meta).

    Side effects: writes OpenWeather / Visual Crossing / Open-Meteo
    forecast snapshots to forecast_history. Reads recent history to
    compute volatility-based MAE.

    Two paths, in order of preference:
      v13 (NEW, preferred when ≥2 Open-Meteo ensemble members present):
        mae_override = NGR-calibrated σ from ensemble spread
        mu_override  = ensemble mean (replaces OpenWeather single value)
        meta["ensemble_calibrated"] = True
        The legacy multiplier penalties (om_spread_mult, range_penalty_mult)
        are SKIPPED — the ensemble itself captures regime-specific σ, and
        stacking multipliers on top double-counts uncertainty.

      v7+v8 legacy (fallback when ensemble unavailable):
        mae_override = static MAE × (history-volatility, OW/VC disagreement,
                                      om_spread_mult, range_penalty_mult)
        mu_override  = None  (caller uses OpenWeather temp_high directly)
        meta["ensemble_calibrated"] = False

    Bias override (per-city temp_bias_f from cities JSON) is applied in
    BOTH paths additively on top of the chosen μ. In threshold_unit
    (F if market is F, C if C).

    Returns (None, None, None, {}) on any failure path so caller can
    fall back cleanly.
    """
    if not spec.target_date:
        return None, None, None, {}

    # v13.4: resolve which daily extreme this market settles on. Lowest-
    # temperature markets use the daily MIN everywhere below (OW reference,
    # VC cross-check, ensemble members, calibration). The metric label also
    # carries the kind so forecast_history keeps low/high series separate.
    temp_kind = getattr(spec, "temp_kind", "high")
    _ext = "low" if temp_kind == "low" else "high"
    metric = f"temp_{_ext}_{'f' if spec.threshold_unit == 'F' else 'c'}"
    target_iso = spec.target_date.isoformat()

    # Extract OW high temp from forecast snapshot
    days = ow_forecast.get("daily_forecast") or ow_forecast.get("forecasts") or []
    ow_day = next((d for d in days if d.get("date") == target_iso), None)
    if ow_day is None:
        return None, None, None, {}
    ow_value = ow_day.get(metric)
    if ow_value is None:
        return None, None, None, {}

    now_iso = _now_iso()
    try:
        with db.connect() as conn:
            db.insert_forecast_history(
                conn, city=spec.city, target_date=target_iso,
                metric=metric, source="openweather",
                predicted_value=float(ow_value), ts=now_iso,
            )
    except Exception as e:
        log_event("warn", {"where": "fh_insert_ow", "err": str(e)},
                   level="WARN")

    vc_value = None
    multi_source = getattr(args, "multi_source", False)
    if multi_source:
        try:
            vc = fetch_visual_crossing(spec.city, target_iso)
        except Exception as e:
            log_event("warn", {"where": "fetch_vc", "city": spec.city,
                                "err": str(e)}, level="WARN")
            vc = None
        if vc and vc.get("days"):
            vc_day = vc["days"][0]
            # VC always returns Fahrenheit when unitGroup="us"
            vc_value_f = vc_day.get("tempmin" if temp_kind == "low"
                                     else "tempmax")
            if vc_value_f is not None:
                # Convert to spec.threshold_unit if needed
                vc_value = (float(vc_value_f) if spec.threshold_unit == "F"
                            else (float(vc_value_f) - 32) * 5.0 / 9.0)
                try:
                    with db.connect() as conn:
                        db.insert_forecast_history(
                            conn, city=spec.city, target_date=target_iso,
                            metric=metric, source="visual_crossing",
                            predicted_value=vc_value, ts=now_iso,
                        )
                except Exception as e:
                    log_event("warn", {"where": "fh_insert_vc",
                                        "err": str(e)}, level="WARN")

    # v8: Open-Meteo ensemble (ICON+GFS+ECMWF) — requires station lat/lon
    om_data = None
    om_enabled = getattr(args, "open_meteo", False) and station and (
        station.get("lat") is not None and station.get("lon") is not None)
    if om_enabled:
        try:
            om_data = fetch_open_meteo_ensemble(
                station["lat"], station["lon"], spec.target_date)
        except Exception as e:
            log_event("warn", {"where": "fetch_open_meteo",
                                "city": spec.city, "err": str(e)},
                       level="WARN")
            om_data = None
        if om_data:
            om_suffix = "_min_c" if temp_kind == "low" else "_max_c"
            for model in ("icon", "gfs", "ecmwf"):
                val_c = om_data.get(f"{model}{om_suffix}")
                if val_c is None:
                    continue
                # Convert to spec.threshold_unit if market is F
                val = (float(val_c) if spec.threshold_unit == "C"
                       else float(val_c) * 9.0 / 5.0 + 32.0)
                try:
                    with db.connect() as conn:
                        db.insert_forecast_history(
                            conn, city=spec.city, target_date=target_iso,
                            metric=metric, source=f"open_meteo_{model}",
                            predicted_value=val, ts=now_iso,
                        )
                except Exception as e:
                    log_event("warn", {"where": "fh_insert_om",
                                        "err": str(e)}, level="WARN")

    # Read history for dynamic MAE (now includes OW+VC+OM models)
    try:
        with db.connect() as conn:
            rows = db.query_forecast_history(
                conn, spec.city, target_iso, metric, limit=5)
    except Exception:
        rows = []

    base_mae = MAE_TEMP_F if spec.threshold_unit == "F" else MAE_TEMP_C
    history_values = [float(r["predicted_value"]) for r in rows]
    mae_dyn = compute_dynamic_mae(history_values, base_mae=base_mae)

    # OW vs VC disagreement signal (used as a sanity flag in both paths).
    disagreement = 0.0
    if vc_value is not None:
        disagreement = abs(float(ow_value) - vc_value)

    # v8: per-city temperature bias (in spec.threshold_unit). Applies in
    # both paths additively on top of μ.
    bias = None
    if station and station.get("temp_bias_f") is not None:
        bias_f = float(station["temp_bias_f"])
        if bias_f != 0.0:
            bias = (bias_f if spec.threshold_unit == "F"
                    else bias_f * 5.0 / 9.0)  # F delta -> C delta

    # v13 (preferred): NGR-calibrated ensemble σ + ensemble mean μ.
    # This replaces the multiplier-on-MAE pattern when ≥2 Open-Meteo
    # members are present. Rationale: stacking om_spread_mult × range_
    # penalty_mult × disagreement on a constant base MAE was producing
    # σ values 2-5x the documented operational σ (1.5K @ 24h) and made
    # every range market look hopelessly overconfident, which the v12.1
    # 0.70 cap then ate down to zero edge (the 2026-06-14 zero-entry
    # run). Ensemble spread is the right uncertainty estimator — NGR
    # only inflates and floors it (Gneiting 2005 MWR 133).
    cal = compute_ensemble_calibration(om_data, spec.threshold_unit,
                                       temp_kind=temp_kind)
    if cal is not None:
        mae_out = float(cal["sigma"])
        mu_out = float(cal["mu"])
        ensemble_calibrated = True
        # Still flag OW vs VC mismatch — if ensemble disagrees with VC by
        # >2°C, the ensemble might still be biased; let the judge see it.
        # We DON'T multiply σ here — that double-counts uncertainty
        # already encoded in the ensemble spread.
    else:
        # Legacy path: history-volatility MAE × disagreement × OM band
        # multiplier × range penalty. Falls through when no ensemble or
        # only 1 model present (rare cities, archive lag, etc.).
        if vc_value is not None:
            threshold = 3.6 if spec.threshold_unit == "F" else 2.0  # ~2C
            if disagreement > threshold:
                mae_dyn *= 1.5
        # om_spread_mult: graduated 1.0/1.3/2.0/3.0 (v13.4: use the spread
        # of the extreme the market resolves on; falls back to max-spread
        # when min-spread is unavailable in older cached om_data)
        _spread_key = "spread_min_c" if temp_kind == "low" else "spread_c"
        if om_data and (om_spread_c := (om_data.get(_spread_key)
                                        or om_data.get("spread_c"))) is not None:
            spread = float(om_spread_c)
            if spread > 5.0:
                mae_dyn *= 3.0
            elif spread > 3.0:
                mae_dyn *= 2.0
            elif spread > 2.0:
                mae_dyn *= 1.3
        # range-bracket MAE penalty (legacy only — v13 ensemble path
        # naturally captures range uncertainty via spread).
        if spec and spec.comparison == "range":
            mae_dyn *= 1.5
        mae_out = mae_dyn
        mu_out = None
        ensemble_calibrated = False

    meta = {
        "ow_value": float(ow_value),
        "vc_value": vc_value,
        "om_spread_c": (om_data or {}).get("spread_c"),
        "om_n_models": (om_data or {}).get("n_models"),
        "comparison": spec.comparison if spec else None,
        "disagreement": round(disagreement, 2),
        "history_n": len(history_values),
        "base_mae": base_mae,
        "mae_dynamic": round(mae_out, 3),
        "mu": (round(mu_out, 3) if mu_out is not None else None),
        "ensemble_calibrated": ensemble_calibrated,  # v13
        "ensemble_members": (cal or {}).get("members") if cal else None,
        "temp_kind": temp_kind,  # v13.4: which extreme priced this market
        "bias": bias,
        "multi_source": multi_source,
        "open_meteo": om_enabled,
        "station": (station or {}).get("station"),
    }
    return mae_out, bias, mu_out, meta


def _build_ladder_candidates(candidates: list[dict], args) -> list[dict]:
    """v9: transform single-bin candidates into coordinated 3-bin ladders.

    Inputs `candidates` are the per-bracket survivors of all discovery
    filters (price band, ttr, edge, etc.). We group them by event_slug.
    Groups of >=2 surviving brackets become a ladder; groups of 1 remain
    single-bin.

    For each ladder:
      - call select_ladder_brackets() to pick central/below/above by
        forecast_prob ordering
      - compute Kelly proportional stake split across the picked legs
        using max_market_exposure_usd as the ladder budget
      - tag each leg with shared ladder_group_id (uuid4), distinct
        ladder_position, and ladder_event_slug

    Single-bin candidates (no event_slug or alone in their event) pass
    through untouched.
    """
    from weather_edge_helpers import select_ladder_brackets, compute_kelly_split
    import uuid

    # Group by event_slug. Candidates with empty event_slug remain
    # single-bin (event_slug "" gets its own pseudo-group treated as
    # single-bin below).
    groups: dict[str, list[dict]] = {}
    singles: list[dict] = []
    for c in candidates:
        es = c.get("event_slug") or ""
        if not es:
            singles.append(c)
            continue
        groups.setdefault(es, []).append(c)

    out: list[dict] = []

    # v9.1: orphan single-bin legs (no event_slug at all) must pass the
    # higher --min-entry-price single-bin floor. Discovery uses the lower
    # --ladder-min-leg-price floor in ladder mode; here we enforce the
    # stricter check for legs that won't benefit from Kelly-capped ladder
    # coordination.
    # v9.2: same logic for edge — orphans must pass --min-edge-pp, while
    # multi-leg ladder legs only need --ladder-min-leg-edge-pp.
    # v9.3: same logic for TTR — orphans must pass --min-ttr-hours, while
    # multi-leg ladder legs only need --ladder-min-ttr-hours.
    single_floor = float(args.min_entry_price or 0.0)
    single_edge_floor = float(args.min_edge_pp or 0.0)
    single_ttr_floor = float(args.min_ttr_hours or 0.0)

    def _orphan_passes(s: dict, reason_label: str,
                        event_slug: str = None) -> bool:
        if single_floor > 0 and float(s["entry_price"]) < single_floor:
            log_event("market_skipped", {
                "slug": s.get("slug"),
                "reason": f"{reason_label}_too_cheap",
                "entry_price": s["entry_price"],
                "min_required": single_floor,
                **({"event_slug": event_slug} if event_slug else {})})
            return False
        if (single_edge_floor > 0
                and float(s.get("edge_pp") or 0.0) < single_edge_floor):
            log_event("market_skipped", {
                "slug": s.get("slug"),
                "reason": f"{reason_label}_edge_too_low",
                "edge_pp": s.get("edge_pp"),
                "min_required": single_edge_floor,
                **({"event_slug": event_slug} if event_slug else {})})
            return False
        if (single_ttr_floor > 0
                and float(s.get("ttr_hours") or 0.0) < single_ttr_floor):
            log_event("market_skipped", {
                "slug": s.get("slug"),
                "reason": f"{reason_label}_ttr_too_short",
                "ttr_h": s.get("ttr_hours"),
                "min_required": single_ttr_floor,
                **({"event_slug": event_slug} if event_slug else {})})
            return False
        return True

    for s in singles:
        if _orphan_passes(s, "single_bin"):
            out.append(s)

    total_ladder_budget = float(args.max_market_exposure_usd)
    split_mode = getattr(args, "ladder_stake_split", "kelly")

    for event_slug, group in groups.items():
        if len(group) < 2:
            # Lone bracket in an event — treat as single-bin and enforce
            # the higher single-bin floors (cheap orphans are the
            # historical loser cohort, not protected by ladder Kelly).
            for s in group:
                if _orphan_passes(s, "ladder_orphan", event_slug):
                    out.append(s)
            continue

        # Pick central/below/above
        picked = select_ladder_brackets(group)
        if picked is None:
            out.extend(group)
            continue
        legs = [picked["central"]]
        if picked["below"] is not None:
            legs.append(picked["below"])
        if picked["above"] is not None:
            legs.append(picked["above"])

        # Compute stake split
        if split_mode == "equal":
            per = round(total_ladder_budget / len(legs), 4)
            split = [{**l, "stake_usd": per,
                      "kelly_frac": 1.0 / len(legs)} for l in legs]
        else:
            split = compute_kelly_split(legs, total_ladder_budget)
            if split is None:
                # No positive-EV legs — drop the whole ladder. Log and
                # continue rather than fall back to single-bin (the
                # individual legs already failed their own edge filter
                # would have been caught upstream; reaching here with
                # all-negative kelly means we'd be skipping for the
                # same reason).
                log_event("ladder_dropped", {
                    "event_slug": event_slug,
                    "reason": "all_kelly_negative",
                    "n_legs": len(legs),
                })
                continue

        # Tag with shared ladder_group_id and position
        group_id = str(uuid.uuid4())
        positions = ["central", "below", "above"][:len(split)]
        for pos, leg in zip(positions, split):
            leg["ladder_group_id"] = group_id
            leg["ladder_position"] = pos
            leg["ladder_event_slug"] = event_slug
            # Override the executor's slippage-based sizing with the
            # ladder's pre-computed stake. The executor reads
            # "ladder_stake_usd" if present.
            leg["ladder_stake_usd"] = leg.get("stake_usd")
            out.append(leg)

        log_event("ladder_built", {
            "event_slug": event_slug,
            "ladder_group_id": group_id,
            "n_legs": len(split),
            "positions": positions,
            "stakes_usd": [round(l["stake_usd"], 2) for l in split],
            "split_mode": split_mode,
        })

    return out


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
        "duplicate_pending": 0, "opposite_side_held": 0,
        "market_exposure_full": 0,
        "ttr_below_min": 0,           # v8: min-TTR filter
        "entry_too_cheap": 0,         # v9: cheap long-shot guard
        "extreme_disagreement": 0,    # v9: adverse selection guard
        "range_bin_gap_too_small": 0,  # v12: sug_005 source fix
        "range_edge_after_cap": 0,    # v12.1: edge gone after sizing-cap
        "not_temperature": 0,         # v14: temperature-only policy
        "entry_too_expensive": 0,     # v10 counter was never initialized → KeyError aborted discovery
    }

    # Phase 1: build candidate proposals using HTTP-only work (no DB lock held).
    # Phase 2: snapshot already-proposed (slug, side) pairs with a brief read.
    # Phase 3: batch-insert proposals, committing per-row so the judge can
    # interleave its writes between iterations.

    candidates: list[dict] = []
    for m in raw_markets:
        slug = m.get("slug", "")
        question = m.get("question", "")
        end_date_str = m.get("endDate", "")
        try:
            end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            skipped["no_end_date"] += 1
            continue

        if end_date < now or end_date > cutoff:
            skipped["outside_window"] += 1
            if args.debug:
                log_event("market_skipped", {"slug": slug,
                                              "reason": "outside_window",
                                              "end_date": end_date_str,
                                              "ttr_h": round((end_date - now).total_seconds() / 3600, 1)})
            continue
        ttr_hours = (end_date - now).total_seconds() / 3600.0

        # v8: skip markets with TTR below the operator-configured minimum.
        # v9.3: in ladder mode use the lower --ladder-min-ttr-hours floor
        # at discovery (forecast accuracy peaks in the 6-12h band; ladder
        # coverage of ±5°F captures that quality without depending on
        # point precision). _build_ladder_candidates enforces the higher
        # --min-ttr-hours on orphan lone-bracket events.
        in_ladder_mode = getattr(args, "ladder_mode", "3bin") != "off"
        ttr_floor = (args.ladder_min_ttr_hours if in_ladder_mode
                      else args.min_ttr_hours)
        if ttr_floor > 0 and ttr_hours < ttr_floor:
            skipped["ttr_below_min"] += 1
            if args.debug:
                log_event("market_skipped", {"slug": slug,
                    "reason": "ttr_below_min",
                    "ttr_h": round(ttr_hours, 1),
                    "min_required": ttr_floor,
                    "mode": "ladder" if in_ladder_mode else "single_bin"})
            # v8 observability: persist per-skip detail so the advisor
            # can analyze the time-to-resolution distribution that we
            # filter (e.g. "are we filtering too aggressively?").
            try:
                with db.connect() as _conn:
                    db.insert_discovery_skip(_conn,
                        ts=_now_iso(), slug=slug, reason="ttr_below_min",
                        meta_json={"ttr_h": round(ttr_hours, 2),
                                    "min_required": ttr_floor,
                                    "end_date": end_date_str})
                    _conn.commit()
            except Exception:
                pass  # never block discovery on observability write
            continue

        if m.get("acceptingOrders") is False:
            skipped["orderbook_unavailable"] = skipped.get("orderbook_unavailable", 0) + 1
            continue

        text_for_parser = m.get("_combined_text") or question
        spec = parse_market(text_for_parser, end_date_str, cities)
        if not spec:
            skipped["parser_failed"] += 1
            log_event("market_skipped", {"slug": slug, "reason": "parser_failed",
                                          "question": question[:100]})
            continue

        # v14 (2026-07-05): temperature-only policy. Never open positions on
        # non-temp markets (rain/snow binaries, numeric precip) — their prob
        # comes from an uncalibrated clipped POP (see is_tradeable_spec).
        # parse_market still returns these specs so _do_monitor_check keeps
        # managing positions opened before v14. Placed before any per-market
        # HTTP (orderbook/forecast) so skipping costs nothing.
        if not is_tradeable_spec(spec):
            skipped["not_temperature"] += 1
            log_event("market_skipped", {"slug": slug,
                                          "reason": "not_temperature",
                                          "metric": spec.metric,
                                          "question": question[:100]})
            try:
                with db.connect() as _conn:
                    db.insert_discovery_skip(_conn,
                        ts=_now_iso(), slug=slug, city=spec.city,
                        reason="not_temperature",
                        meta_json={"metric": spec.metric})
                    _conn.commit()
            except Exception:
                pass  # never block discovery on observability write
            continue

        if spec.confidence < 0.5:
            skipped["parser_low_confidence"] += 1
            log_event("market_skipped", {"slug": slug,
                                          "reason": "parser_low_confidence",
                                          "confidence": spec.confidence})
            continue

        try:
            token_ids = json.loads(m.get("clobTokenIds", "[]"))
            if len(token_ids) < 2:
                skipped["no_token_ids"] += 1
                continue
            token_id_yes, token_id_no = str(token_ids[0]), str(token_ids[1])
        except (json.JSONDecodeError, TypeError):
            skipped["no_token_ids"] += 1
            continue

        # HTTP — no DB lock held
        book_yes = fetch_orderbook(token_id_yes)
        book_no = fetch_orderbook(token_id_no)
        if not book_yes or not book_no:
            skipped["orderbook_unavailable"] += 1
            continue
        implied = implied_probabilities(book_yes, book_no)
        if implied["yes_ask"] is None or implied["no_ask"] is None:
            skipped["no_implied_prices"] += 1
            continue

        if not (args.min_price <= implied["yes_ask"] <= args.max_price or
                args.min_price <= implied["no_ask"] <= args.max_price):
            skipped["price_band_miss"] += 1
            if args.debug:
                log_event("market_skipped", {"slug": slug, "reason": "price_band_miss",
                                              "yes_ask": implied["yes_ask"],
                                              "no_ask": implied["no_ask"]})
            continue

        # v8: resolve city -> resolution station coords (Polymarket
        # markets resolve at a specific weather station, not the city
        # center). v9: if no curated entry, try parsing the market
        # description for a station phrase. Falls back to OpenWeather
        # geocoding when both miss.
        station = resolve_station(spec.city, cities)
        if not station:
            description = m.get("description")
            if description:
                station = auto_extract_station(spec.city, cities, description)
                if station:
                    log_event("station_auto_resolved", {
                        "slug": slug, "city": spec.city,
                        "station": station["station"],
                        "lat": station["lat"], "lon": station["lon"],
                    })
        if station:
            forecast = fetch_forecast(spec.city, days=5,
                                       lat=station["lat"], lon=station["lon"])
        else:
            forecast = fetch_forecast(spec.city, days=5)
        if not forecast:
            skipped["forecast_unavailable"] += 1
            log_event("market_skipped", {"slug": slug, "reason": "forecast_unavailable",
                                          "city": spec.city,
                                          "station": (station or {}).get("station")})
            continue

        # v7+v8+v13: Persist OW + (optionally) VC + Open-Meteo ensemble for
        # multi-source consensus + (NGR-calibrated σ + ensemble μ) when
        # available; extract per-city bias.
        mae_dynamic, bias, mu_override, mae_meta = _compute_mae_for_market(
            spec, forecast, args, station=station)
        ensemble_calibrated = bool(mae_meta.get("ensemble_calibrated"))

        forecast_prob = forecast_probability(spec, forecast,
                                              mae_override=mae_dynamic,
                                              bias_override=bias,
                                              mu_override=mu_override)
        if forecast_prob is None:
            skipped["no_forecast_for_target_date"] += 1
            log_event("market_skipped", {"slug": slug, "reason": "no_forecast_for_target_date"})
            continue

        edge = compute_edge(forecast_prob, implied)
        # v9.2: in ladder mode, use the lower --ladder-min-leg-edge-pp
        # floor at discovery — adjacent legs (below/above) structurally
        # have smaller edges than the central, and they're admitted as
        # hedges (Kelly proportional sizing caps stake on small edges).
        # _build_ladder_candidates enforces --min-edge-pp on orphans.
        in_ladder_mode = getattr(args, "ladder_mode", "3bin") != "off"
        edge_floor = (args.ladder_min_leg_edge_pp if in_ladder_mode
                       else args.min_edge_pp)
        if edge["best_side"] is None or edge["edge_pp_at_best"] < edge_floor:
            skipped["low_edge"] += 1
            log_event("market_evaluated", {
                "slug": slug, "side": edge["best_side"],
                "edge_pp": edge["edge_pp_at_best"],
                "decision": "skipped_low_edge",
                "forecast_prob": forecast_prob,
                "yes_ask": implied["yes_ask"],
                "no_ask": implied["no_ask"],
                "mae_meta": mae_meta,
                "edge_floor": edge_floor,
                "mode": "ladder" if in_ladder_mode else "single_bin",
            })
            continue

        side = edge["best_side"]
        entry_price = implied["yes_ask"] if side == "YES" else implied["no_ask"]

        # v12.1: range markets are NOT clipped in forecast_probability (so the
        # LOW clip can't invert side selection and manufacture a phantom YES
        # edge). Cap the chosen side's confidence here for storage/sizing, and
        # recompute the edge on the capped value — a range bet that only had
        # edge via inflated confidence is dropped now, before the judge.
        # v13: when the ensemble_calibrated path is active, the cap relaxes
        # from 0.70 → 0.95 because the (μ, σ) come from a calibrated NGR
        # ensemble model rather than a constant MAE — the model's own
        # uncertainty is the cap; we only bound pathological agreement.
        forecast_prob_sized = prob_yes_for_sizing(
            forecast_prob, side, spec.comparison,
            ensemble_calibrated=ensemble_calibrated)
        p_side = (forecast_prob_sized if side == "YES"
                  else 1.0 - forecast_prob_sized)
        if spec.comparison == "range":
            eff_edge_pp = round((p_side - entry_price) * 100.0, 4)
            if eff_edge_pp < edge_floor:
                skipped["range_edge_after_cap"] += 1
                log_event("market_skipped", {
                    "slug": slug, "reason": "range_edge_after_cap",
                    "side": side, "p_side_capped": round(p_side, 3),
                    "entry_price": entry_price, "edge_pp": eff_edge_pp,
                    "edge_floor": edge_floor,
                    "ensemble_calibrated": ensemble_calibrated})
                try:
                    with db.connect() as _conn:
                        db.insert_discovery_skip(_conn, ts=_now_iso(),
                            slug=slug, city=spec.city,
                            reason="range_edge_after_cap",
                            meta_json={"side": side, "edge_pp": eff_edge_pp,
                                       "p_side_capped": round(p_side, 3)})
                        _conn.commit()
                except Exception:
                    pass
                continue
        else:
            eff_edge_pp = edge["edge_pp_at_best"]

        # v12 (advisor sug_005 source fix): on a range/bracket market, a NO
        # bet means "the temp will NOT land in this ~1°C bin". When the
        # forecast sits within range_min_bin_gap_mae × MAE of the nearest
        # bin edge, the Gaussian model still reports a high P(NO) — but that
        # confidence is illusory: the bin is well inside the forecast's own
        # uncertainty, so the realized temp lands in it far more often than
        # the model implies (these lost 92% in the -$740 week). Skip here so
        # the bot never proposes them and the judge LLM cost is saved. The
        # judge's ≤1°C REJECT and ≤2×MAE ADJUST remain as backstops for the
        # 1×–2×MAE band that this filter lets through.
        if (side == "NO" and spec.comparison == "range"
                and spec.threshold_value_high is not None):
            gap_mult = float(getattr(args, "range_min_bin_gap_mae", 1.0) or 0.0)
            if gap_mult > 0:
                ref = forecast_ref_value(spec, forecast)
                if ref is not None:
                    lo, hi = spec.threshold_value, spec.threshold_value_high
                    dist = (lo - ref) if ref < lo else (ref - hi) if ref > hi else 0.0
                    min_gap = gap_mult * mae_dynamic
                    if dist < min_gap:
                        skipped["range_bin_gap_too_small"] += 1
                        log_event("market_skipped", {
                            "slug": slug, "reason": "range_bin_gap_too_small",
                            "forecast": round(ref, 2), "bin": [lo, hi],
                            "dist_to_bin": round(dist, 2),
                            "mae": round(mae_dynamic, 2),
                            "min_gap_required": round(min_gap, 2),
                        })
                        try:
                            with db.connect() as _conn:
                                db.insert_discovery_skip(
                                    _conn, ts=_now_iso(), slug=slug,
                                    city=spec.city,
                                    reason="range_bin_gap_too_small",
                                    meta_json=json.dumps({
                                        "forecast": round(ref, 2),
                                        "bin": [lo, hi],
                                        "dist_to_bin": round(dist, 2),
                                        "mae": round(mae_dynamic, 2),
                                    }))
                        except Exception:
                            pass
                        continue

        if not (args.min_price <= entry_price <= args.max_price):
            skipped["price_band_miss"] += 1
            continue

        # v9 (loss analysis 2026-05-15 + ladder strategy 2026-05-17):
        # In ladder mode, use the lower --ladder-min-leg-price floor at
        # discovery — cheap legs that end up in multi-leg ladders are
        # protected by Kelly proportional sizing (auto-caps stake at 0
        # for negative-EV cheap legs). In single-bin / off mode, the
        # higher --min-entry-price floor applies uniformly.
        # _build_ladder_candidates enforces --min-entry-price on orphan
        # legs (would-be single-bin) after grouping.
        in_ladder_mode = getattr(args, "ladder_mode", "3bin") != "off"
        discovery_floor = (args.ladder_min_leg_price if in_ladder_mode
                            else args.min_entry_price)
        if discovery_floor > 0 and entry_price < discovery_floor:
            skipped["entry_too_cheap"] += 1
            if args.debug:
                log_event("market_skipped", {"slug": slug,
                    "reason": "entry_too_cheap",
                    "entry_price": entry_price, "side": side,
                    "min_required": discovery_floor,
                    "mode": "ladder" if in_ladder_mode else "single_bin"})
            try:
                with db.connect() as _conn:
                    db.insert_discovery_skip(_conn,
                        ts=_now_iso(), slug=slug, city=spec.city,
                        reason="entry_too_cheap",
                        meta_json={"entry_price": entry_price, "side": side,
                                    "min_required": discovery_floor,
                                    "edge_pp": edge["edge_pp_at_best"]})
                    _conn.commit()
            except Exception:
                pass
            continue

        # v10 (strategic pivot 2026-05-16): block extreme-high-price bets
        # where payoff asymmetry collapses. Above 0.85 a winning ticket
        # only pays out 18% even after a perfect read; combined with the
        # bot's typical MAE this is structurally negative-EV.
        if args.max_entry_price > 0 and entry_price > args.max_entry_price:
            skipped["entry_too_expensive"] += 1
            if args.debug:
                log_event("market_skipped", {"slug": slug,
                    "reason": "entry_too_expensive",
                    "entry_price": entry_price, "side": side,
                    "max_allowed": args.max_entry_price})
            try:
                with db.connect() as _conn:
                    db.insert_discovery_skip(_conn,
                        ts=_now_iso(), slug=slug, city=spec.city,
                        reason="entry_too_expensive",
                        meta_json={"entry_price": entry_price, "side": side,
                                    "max_allowed": args.max_entry_price,
                                    "edge_pp": edge["edge_pp_at_best"]})
                    _conn.commit()
            except Exception:
                pass
            continue

        # v9: adverse selection guard. When bot's forecast and market
        # implied probability disagree by more than X percentage points,
        # the bot is statistically more likely to be wrong than the
        # market. Loss analysis 2026-05-14: trades with 80pp+ bot-vs-
        # market disagreement had a 0% win rate. The bigger the gap,
        # the more likely we are buying tickets the market knows are
        # losers.
        implied_on_side = (implied["yes_ask"] if side == "YES"
                            else implied["no_ask"])
        bot_prob_on_side = p_side  # v12.1: capped P(side) for range
        disagreement_pp = abs(bot_prob_on_side - implied_on_side) * 100
        if args.max_disagreement_pp > 0 and disagreement_pp > args.max_disagreement_pp:
            skipped["extreme_disagreement"] += 1
            if args.debug:
                log_event("market_skipped", {"slug": slug,
                    "reason": "extreme_disagreement",
                    "disagreement_pp": round(disagreement_pp, 1),
                    "bot_prob": round(bot_prob_on_side, 3),
                    "implied": round(implied_on_side, 3),
                    "side": side,
                    "max_allowed": args.max_disagreement_pp})
            try:
                with db.connect() as _conn:
                    db.insert_discovery_skip(_conn,
                        ts=_now_iso(), slug=slug, city=spec.city,
                        reason="extreme_disagreement",
                        meta_json={"disagreement_pp": round(disagreement_pp, 1),
                                    "bot_prob": round(bot_prob_on_side, 3),
                                    "implied": round(implied_on_side, 3),
                                    "side": side,
                                    "max_allowed": args.max_disagreement_pp})
                    _conn.commit()
            except Exception:
                pass
            continue

        candidates.append({
            "slug": slug, "question": question,
            "end_date_str": end_date_str, "side": side,
            "entry_price": entry_price, "forecast_prob": forecast_prob_sized,
            "edge_pp": eff_edge_pp,
            "forecast": forecast, "spec": spec, "ttr_hours": ttr_hours,
            "token_id_yes": token_id_yes, "token_id_no": token_id_no,
            "implied": implied,
            "condition_id": m.get("conditionId", ""),
            "discovery_meta": mae_meta,  # v8 observability
            # v9: parent-event grouping for laddering
            "event_slug": m.get("event_slug") or "",
        })

    # v9: 3-bin laddering — group surviving candidates by event_slug and
    # transform groups with >=2 siblings into coordinated ladders with
    # shared ladder_group_id. Candidates without event_slug or with only
    # one sibling remain single-bin (legacy path).
    if getattr(args, "ladder_mode", "3bin") != "off":
        candidates = _build_ladder_candidates(candidates, args)

    # Phase 2 + 3: snapshot existing (slug, side) pairs, then insert candidates
    # one-by-one with per-row commits. Each commit releases the writer lock
    # immediately so the judge can interleave its updates between proposals.
    market_cap_usd = float(args.max_market_exposure_usd)

    with db.connect() as conn:
        # Sets of currently-relevant (slug, side) pairs and per-slug $ exposure.
        # pending  = proposed/approved/adjusted, not yet executed (wait — no
        #            duplicate while one is in flight)
        # open     = executed and NOT cashed out (counts toward exposure cap)
        pending: set[tuple[str, str]] = set()
        open_sides: set[tuple[str, str]] = set()
        exposure: dict[str, float] = {}
        for r in conn.execute(
            "SELECT e.market_slug, e.side, e.status, "
            "       COALESCE(e.size_usd, 0) AS size_usd, "
            "       c.cashout_id AS cashout_id "
            "FROM entries e "
            "LEFT JOIN cashouts c ON c.entry_id = e.entry_id"
        ):
            slug_, side_, status_, size_, co = (
                r["market_slug"], r["side"], r["status"],
                float(r["size_usd"] or 0), r["cashout_id"])
            if status_ in ("PROPOSED", "APPROVED", "ADJUSTED"):
                pending.add((slug_, side_))
            elif status_ in ("EXECUTED", "FAST_PATH") and co is None:
                open_sides.add((slug_, side_))
                exposure[slug_] = exposure.get(slug_, 0.0) + size_

        for c in candidates:
            slug = c["slug"]
            side = c["side"]
            opposite = "NO" if side == "YES" else "YES"

            # Block opposite-side entries — destructive unless true arb (not
            # detected by compute_edge). Counts open + pending opposite.
            if (slug, opposite) in open_sides or (slug, opposite) in pending:
                skipped["opposite_side_held"] += 1
                log_event("market_skipped", {
                    "slug": slug, "reason": "opposite_side_held",
                    "would_propose": side, "already_have": opposite,
                })
                continue

            # Block duplicate while a previous proposal on (slug, side) is
            # still in flight (not yet executed) — avoids wasting judge cycles
            # on a trade that will hit the exposure cap at execute time anyway.
            if (slug, side) in pending:
                skipped["duplicate_pending"] += 1
                continue

            # Allow re-entry on same (slug, side) AFTER execution as long as
            # there's room under the per-market exposure cap.
            current_exposure = exposure.get(slug, 0.0)
            if current_exposure >= market_cap_usd:
                skipped["market_exposure_full"] += 1
                log_event("market_skipped", {
                    "slug": slug, "reason": "market_exposure_full",
                    "current_exposure_usd": round(current_exposure, 2),
                    "cap_usd": market_cap_usd,
                })
                continue

            spec = c["spec"]
            entry_id = db.insert_entry(
                conn,
                ts=_now_iso(),
                market_slug=c["slug"],
                market_question=c["question"],
                condition_id=c["condition_id"],
                token_id_yes=c["token_id_yes"],
                token_id_no=c["token_id_no"],
                end_date=c["end_date_str"],
                side=c["side"],
                entry_price=c["entry_price"],
                forecast_prob_at_entry=(c["forecast_prob"] if c["side"] == "YES"
                                        else 1.0 - c["forecast_prob"]),
                implied_prob_at_entry=c["entry_price"],
                edge_pp_at_entry=c["edge_pp"],
                forecast_snapshot_json=c["forecast"],
                parser_confidence=spec.confidence,
                city_resolved=spec.city,
                threshold_value=spec.threshold_value,
                threshold_unit=spec.threshold_unit,
                comparison=spec.comparison,
                ttr_hours_at_entry=c["ttr_hours"],
                status="PROPOSED",
                # v11: tag the tuned pipeline explicitly so cheap_convexity
                # entries (which use their own discovery path) stay isolated
                # in every KPI query. NULL would also be treated as legacy,
                # but tagging new rows avoids relying on the backfill.
                strategy="weather_edge",
                # v8 observability: stash mae_meta dict (mae_dynamic,
                # bias, station, OW/VC/Open-Meteo values, penalties)
                # so the advisor can cohort-analyze trades.
                discovery_meta_json=c.get("discovery_meta") or {},
                # v9: 3-bin laddering metadata. NULL on single-bin entries.
                ladder_group_id=c.get("ladder_group_id"),
                ladder_position=c.get("ladder_position"),
                ladder_event_slug=c.get("ladder_event_slug"),
                ladder_stake_usd=c.get("ladder_stake_usd"),
            )
            conn.commit()  # release writer lock so judge can interleave
            # Update in-memory state so subsequent iterations in this same
            # discovery cycle see the new proposal as already-pending.
            pending.add((slug, side))
            proposed += 1
            log_event("entry_proposed", {
                "entry_id": entry_id, "slug": c["slug"], "side": c["side"],
                "entry_price": c["entry_price"],
                "forecast_prob": c["forecast_prob"],
                "edge_pp": c["edge_pp"],
                "city": spec.city, "ttr_h": round(c["ttr_hours"], 1),
            })

    log_event("discovery_end", {"proposed": proposed,
                                 "fetched": len(raw_markets),
                                 "candidates_after_filters": len(candidates),
                                 "skipped_breakdown": skipped})
    return proposed


# ---------------------------------------------------------------------------
# Execute cycle: pick up APPROVED entries from judge, execute paper trade
# ---------------------------------------------------------------------------


def _ladder_atomic_gate(group_id: str, current_entry_id: int) -> str:
    """v9: decide whether a ladder group is READY for atomic execution,
    needs to DEFER (sibling still pending judge), or is DEAD (some
    sibling failed). Returns one of 'READY', 'DEFER', 'DEAD'."""
    with db.connect() as conn:
        legs = db.query_ladder_group(conn, group_id)
    if not legs:
        return "DEAD"
    statuses = {leg["status"] for leg in legs}
    if statuses & {"REJECTED", "SKIPPED"}:
        return "DEAD"
    if "PROPOSED" in statuses:
        return "DEFER"
    if statuses <= {"APPROVED", "ADJUSTED", "EXECUTED"}:
        # EXECUTED in a sibling means we already partially executed (e.g.
        # earlier crash). Treat as DEAD to avoid double-execution; the
        # operator can manually inspect.
        if "EXECUTED" in statuses:
            return "DEAD"
        return "READY"
    return "DEFER"  # mixed unknown — wait


def _ladder_mark_dead(group_id: str, reason: str) -> None:
    """Mark all non-terminal legs of a ladder group as SKIPPED."""
    with db.connect() as conn:
        legs = db.query_ladder_group(conn, group_id)
        marked = []
        for leg in legs:
            if leg["status"] in ("APPROVED", "ADJUSTED", "PROPOSED"):
                db.update_entry_status(conn, leg["entry_id"], "SKIPPED",
                                        skip_reason=reason)
                marked.append(leg["entry_id"])
        conn.commit()
    log_event("ladder_group_dead", {
        "ladder_group_id": group_id,
        "reason": reason,
        "marked_skipped": marked,
    })


def _execute_ladder_group_atomic(group_id: str, args, engine,
                                  default_fee_rate: float) -> int:
    """v9: atomic execution of all legs in a ladder group. Pre-checks
    all legs (orderbook fetch, slippage compute, edge re-check, market
    exposure). If ANY pre-check fails, marks all legs SKIPPED with
    'ladder_partial_failure' and returns 0. Otherwise executes all legs
    sequentially via paper_engine and returns count of executed legs.
    """
    with db.connect() as conn:
        legs = db.query_ladder_group(conn, group_id)
    if not legs:
        return 0

    # Phase 1: pre-check every leg without touching the engine
    pre_checks = []
    for leg in legs:
        entry_id = leg["entry_id"]
        status = leg["status"]
        adjusted_side = (leg["judge_adjusted_side"]
                          if status == "ADJUSTED" else None)
        side = adjusted_side or leg["side"]
        token_id = leg["token_id_yes"] if side == "YES" else leg["token_id_no"]
        book = fetch_orderbook(token_id)
        if not book or not book.get("asks"):
            return _ladder_abort(group_id, "no_orderbook", entry_id)

        sizing = compute_max_size_for_slippage(
            book, "BUY", max_slippage=args.max_slippage)
        if sizing["max_shares"] == 0:
            return _ladder_abort(group_id, "zero_max_size", entry_id)

        fill_price = float(sizing["avg_fill"])
        forecast_prob = leg["forecast_prob_at_entry"]
        if forecast_prob is not None:
            current_edge_pp = round(
                (float(forecast_prob) - fill_price) * 100.0, 4)
            # v9.2: ladder legs use the lower per-leg execute floor.
            # Single-bin entries use the standard --execute-min-edge-pp.
            min_edge = (getattr(args, "ladder_execute_min_leg_edge_pp", None)
                        or getattr(args, "execute_min_edge_pp", None)
                        or args.min_edge_pp)
            if current_edge_pp < min_edge:
                return _ladder_abort(group_id, "edge_stale", entry_id)

        # Honor the stake target stored at discovery (ladder_stake_usd) —
        # the Kelly proportional split target for this leg. Then clamp to
        # slippage-based max and market exposure cap.
        target_usd = float(leg["ladder_stake_usd"] or sizing["max_usd"])
        target_usd = min(target_usd, float(sizing["max_usd"]))

        # v13.5 (2026-07-05): honor the judge's ADJUST size cap — mirrors
        # the single-entry path (see judge_size_cap below in
        # execute_approved). The ladder path only clamped to the Kelly
        # split, so an ADJUSTED leg executed at full Kelly size: Seoul #10
        # (2026-07-01) had judge cap $15 but executed $29.32 and lost 2x
        # what the judge authorized. The judge's conviction, not the Kelly
        # split, governs exposure on ADJUSTED legs.
        judge_size_cap = (leg["judge_adjusted_size_usd"]
                          if status == "ADJUSTED" else None)
        if judge_size_cap is not None and float(judge_size_cap) > 0:
            capped = min(target_usd, float(judge_size_cap))
            if capped < target_usd:
                log_event("ladder_size_judge_capped", {
                    "entry_id": entry_id,
                    "ladder_group_id": group_id,
                    "kelly_target_usd": round(target_usd, 2),
                    "judge_size_cap_usd": float(judge_size_cap),
                    "applied_target_usd": round(capped, 2),
                })
            target_usd = capped

        market_slug = leg["market_slug"]
        with db.connect() as conn2:
            cur_exp = db.current_market_exposure_usd(conn2, market_slug)
        remaining = float(args.max_market_exposure_usd) - cur_exp
        if remaining <= 0:
            return _ladder_abort(group_id, "market_exposure_cap_reached", entry_id)
        target_usd = min(target_usd, remaining)

        if target_usd < 10:
            return _ladder_abort(group_id, "size_below_min_$10", entry_id)

        pre_checks.append({
            "leg": leg,
            "side": side,
            "token_id": token_id,
            "sizing": sizing,
            "target_usd": target_usd,
        })

    # Phase 2: all pre-checks passed — execute every leg atomically
    if args.dry_run:
        with db.connect() as conn:
            for pc in pre_checks:
                db.update_entry_status(conn, pc["leg"]["entry_id"], "EXECUTED",
                                       size_usd=pc["target_usd"],
                                       size_shares=pc["sizing"]["max_shares"],
                                       entry_price=pc["sizing"]["avg_fill"])
            conn.commit()
        log_event("ladder_executed_dry", {
            "ladder_group_id": group_id,
            "n_legs": len(pre_checks),
            "total_usd": sum(pc["target_usd"] for pc in pre_checks),
        })
        return len(pre_checks)

    executed_results = []
    for pc in pre_checks:
        leg = pc["leg"]
        try:
            result = engine.open_position(
                token_id=pc["token_id"],
                side=pc["side"],
                size_usd=pc["target_usd"],
                market_question=leg["market_question"][:200],
                fee_rate=default_fee_rate,
                confidence=0.65,
                reasoning=(f"weather_edge_bot ladder leg "
                            f"entry_id={leg['entry_id']} "
                            f"group={group_id[:8]}"),
            )
        except Exception as e:
            log_event("error", {"where": "ladder_exec_open_position",
                                "entry_id": leg["entry_id"],
                                "ladder_group_id": group_id,
                                "err": str(e)}, level="ERROR")
            result = {"status": "failed", "reason": str(e)}
        executed_results.append((leg, result))
        if result.get("status") != "executed":
            # One leg failed mid-flight. Rest of group already executed
            # legs stay open (paper_engine has no rollback). Mark the
            # remaining un-executed legs as SKIPPED and surface the
            # partial-fill state loudly.
            n_ok = sum(1 for _, r in executed_results
                       if r.get("status") == "executed")
            log_event("ladder_partial_execution", {
                "ladder_group_id": group_id,
                "failed_entry_id": leg["entry_id"],
                "failed_reason": result.get("reason"),
                "n_legs_executed": n_ok,
                "n_legs_total": len(pre_checks),
            }, level="ERROR")
            # Persist statuses for legs we tried, mark untried as SKIPPED
            with db.connect() as conn:
                for tried_leg, tried_result in executed_results:
                    if tried_result.get("status") == "executed":
                        db.update_entry_status(conn, tried_leg["entry_id"], "EXECUTED",
                                                size_usd=tried_result.get("cost_usd"),
                                                size_shares=tried_result.get("shares_filled"),
                                                entry_price=tried_result.get("avg_price"))
                    else:
                        db.update_entry_status(conn, tried_leg["entry_id"], "SKIPPED",
                                                skip_reason=str(tried_result.get("reason"))[:200])
                # Remaining legs we didn't try yet
                tried_ids = {l["entry_id"] for l, _ in executed_results}
                for pc2 in pre_checks:
                    if pc2["leg"]["entry_id"] not in tried_ids:
                        db.update_entry_status(conn, pc2["leg"]["entry_id"], "SKIPPED",
                                                skip_reason="ladder_partial_failure")
                conn.commit()
            return n_ok

    # All legs executed successfully
    with db.connect() as conn:
        for leg, result in executed_results:
            db.update_entry_status(conn, leg["entry_id"], "EXECUTED",
                                    size_usd=result.get("cost_usd"),
                                    size_shares=result.get("shares_filled"),
                                    entry_price=result.get("avg_price"))
        conn.commit()
    log_event("ladder_executed", {
        "ladder_group_id": group_id,
        "n_legs": len(executed_results),
        "total_cost_usd": sum(r.get("cost_usd", 0) for _, r in executed_results),
    })
    return len(executed_results)


def _ladder_abort(group_id: str, reason: str, trigger_entry_id: int) -> int:
    """Helper: mark the whole ladder group as SKIPPED with the abort
    reason and log loudly. Returns 0 (no legs executed)."""
    with db.connect() as conn:
        legs = db.query_ladder_group(conn, group_id)
        for leg in legs:
            if leg["status"] in ("APPROVED", "ADJUSTED", "PROPOSED"):
                db.update_entry_status(conn, leg["entry_id"], "SKIPPED",
                                        skip_reason=f"ladder_aborted:{reason}")
        conn.commit()
    log_event("ladder_aborted", {
        "ladder_group_id": group_id,
        "reason": reason,
        "trigger_entry_id": trigger_entry_id,
    })
    return 0


def _risk_block_reason(engine, args) -> Optional[str]:
    """v11 (2026-05-31 post-mortem): hard-enforced risk gate. Returns a
    human-readable reason string if NEW entries must be blocked this cycle,
    else None.

    Blocks when either:
      - portfolio drawdown from peak >= --max-drawdown-halt-pct (default 20),
        or
      - today's realized loss >= --daily-loss-limit-pct of the starting
        balance (default 5).

    Scope is entry-blocking only — open positions are left to resolve or
    cash out normally (operator decision). The drawdown figure comes from
    PaperEngine.get_portfolio() and reflects both realized and unrealized
    P&L (open losers depress total_value), so it is the primary guard; the
    daily realized-loss check is a secondary same-day circuit breaker.

    Fail-open: a portfolio-read error logs a WARN and does NOT block, so a
    transient price-fetch failure can't wedge the executor.
    """
    try:
        pf = engine.get_portfolio()
    except Exception as e:
        log_event("warn", {"where": "risk_gate", "err": str(e)}, level="WARN")
        return None
    drawdown_pct = float(pf.get("drawdown_pct") or 0.0)
    starting = float(pf.get("starting_balance") or 0.0)

    halt_dd = float(getattr(args, "max_drawdown_halt_pct", 20.0) or 0.0)
    if halt_dd > 0 and drawdown_pct >= halt_dd:
        return f"drawdown {drawdown_pct:.1f}% >= halt threshold {halt_dd:.1f}%"

    daily_limit = float(getattr(args, "daily_loss_limit_pct", 5.0) or 0.0)
    if daily_limit > 0 and starting > 0:
        with db.connect() as conn:
            # Today's realized P&L = cashouts + positions resolved today.
            # The original query summed only cashouts, so the circuit breaker
            # was blind to hold-to-resolution losses — which were 38 of 41
            # losses in the -$740 week. Include resolutions (excluding any
            # entry that also cashed out, to avoid double counting).
            cash_row = conn.execute(
                "SELECT COALESCE(SUM(realized_pnl_usd), 0) "
                "FROM cashouts WHERE DATE(ts) = DATE('now')"
            ).fetchone()
            res_row = conn.execute(
                "SELECT COALESCE(SUM("
                "  (r.payout_per_share - e.entry_price) * e.size_shares), 0) "
                "FROM resolutions r "
                "JOIN entries e ON e.entry_id = r.entry_id "
                "LEFT JOIN cashouts c ON c.entry_id = r.entry_id "
                "WHERE DATE(r.ts_resolved) = DATE('now') "
                "  AND c.cashout_id IS NULL"
            ).fetchone()
        realized_today = float(cash_row[0] or 0.0) + float(res_row[0] or 0.0)
        if realized_today < 0:
            daily_loss_pct = -realized_today / starting * 100.0
            if daily_loss_pct >= daily_limit:
                return (f"daily realized loss {daily_loss_pct:.1f}% >= limit "
                        f"{daily_limit:.1f}% (${realized_today:.2f} today)")
    return None


CC_GATE_PATH = Path.home() / ".polymarket-paper" / "cheap_convexity_gate.json"


def _load_cc_gate() -> dict:
    """Read the tail-calibration gate artifact produced by
    cheap_convexity_calibration.py. Missing/unreadable → {} (treated as not
    passed, so the strategy stays a no-op — fail-closed)."""
    try:
        return json.loads(CC_GATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _cheap_convexity_target_usd(portfolio_value: float, p_side: float,
                                fill_price: float, args) -> float:
    """half-Kelly stake for a cheap_convexity bet, capped at
    --cc-max-size-pct of portfolio (hard ceiling 5%). Tail bets mostly expire
    at zero, so sizing is deliberately small (CLAUDE.md §2: first trade with a
    new strategy = 1%). Returns 0 if the edge is non-positive."""
    if portfolio_value <= 0 or not (0.0 < fill_price < 1.0):
        return 0.0
    p = max(0.0, min(1.0, float(p_side)))
    q = 1.0 - p
    b = (1.0 - fill_price) / fill_price          # net odds for the bought side
    full_kelly = (p * b - q) / b if b > 0 else 0.0
    if full_kelly <= 0:
        return 0.0
    half_kelly_frac = full_kelly / 2.0
    cap_pct = min(float(getattr(args, "cc_max_size_pct", 1.0)), 5.0) / 100.0
    frac = min(half_kelly_frac, cap_pct)
    return portfolio_value * frac


def run_discovery_cheap_convexity(args, cities: dict) -> int:
    """v11 cheap_convexity discovery — a SEPARATE path from run_discovery.

    Buys 1-20c temperature tail bins whose market price sits below the RAW
    (unclipped) model fair, to later exit on cashout at convergence. It never
    calls forecast_probability (whose [0.30,0.70] clip destroys the tail and
    manufactures phantom edge on at_least/at_most bins) — it uses
    _forecast_probability_raw directly. Entries are tagged
    strategy='cheap_convexity', forced single-bin (ladder_group_id=None), and
    only proposed when both an entry-slippage AND an exit-liquidity (bid-side)
    gate pass at <= --max-slippage. PAPER only.

    No-op unless --cheap-convexity is set AND the tail-calibration gate
    (cheap_convexity_gate.json) reports tail_calibration_pass. This is the
    Phase-1 → Phase-2 unlock: the gate is data, not a code branch.
    """
    if not getattr(args, "cheap_convexity", False):
        return 0
    gate = _load_cc_gate()
    if not gate.get("tail_calibration_pass"):
        log_event("cheap_convexity_gated", {
            "reason": "tail_calibration_not_passed",
            "gate_n": gate.get("n"), "gate_ece": gate.get("ece")})
        return 0

    log_event("cheap_convexity_discovery_start", {
        "cc_min_entry_price": args.cc_min_entry_price,
        "cc_max_entry_price": args.cc_max_entry_price,
        "cc_min_edge_pp": args.cc_min_edge_pp})
    raw_markets = fetch_weather_markets(min_volume=args.min_volume)
    if not raw_markets:
        return 0

    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=args.window_hours)
    proposed = 0

    def _cc_skip(slug, city, reason, meta=None):
        log_event("cheap_convexity_skipped",
                  {"slug": slug, "reason": reason, **(meta or {})})
        try:
            with db.connect() as _conn:
                db.insert_discovery_skip(_conn, ts=_now_iso(), slug=slug,
                    city=city, reason=f"cc_{reason}", meta_json=meta or {})
                _conn.commit()
        except Exception:
            pass

    for m in raw_markets:
        slug = m.get("slug", "")
        question = m.get("question", "")
        end_date_str = m.get("endDate", "")
        try:
            end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if end_date < now or end_date > cutoff:
            continue
        ttr_hours = (end_date - now).total_seconds() / 3600.0
        if args.min_ttr_hours > 0 and ttr_hours < args.min_ttr_hours:
            continue
        if m.get("acceptingOrders") is False:
            continue

        spec = parse_market(m.get("_combined_text") or question,
                            end_date_str, cities)
        if not spec or not is_tradeable_spec(spec):
            continue  # temp-only (v14); rain/snow already excluded
        if spec.confidence < 0.5:
            continue

        try:
            token_ids = json.loads(m.get("clobTokenIds", "[]"))
            if len(token_ids) < 2:
                continue
            token_id_yes, token_id_no = str(token_ids[0]), str(token_ids[1])
        except (json.JSONDecodeError, TypeError):
            continue

        book_yes = fetch_orderbook(token_id_yes)
        book_no = fetch_orderbook(token_id_no)
        if not book_yes or not book_no:
            continue
        implied = implied_probabilities(book_yes, book_no)
        if implied["yes_ask"] is None or implied["no_ask"] is None:
            continue

        station = resolve_station(spec.city, cities)
        if not station and m.get("description"):
            station = auto_extract_station(spec.city, cities, m.get("description"))
        if station:
            forecast = fetch_forecast(spec.city, days=5,
                                       lat=station["lat"], lon=station["lon"])
        else:
            forecast = fetch_forecast(spec.city, days=5)
        if not forecast:
            continue

        mae_dynamic, bias, mu_override, mae_meta = _compute_mae_for_market(
            spec, forecast, args, station=station)
        # RAW fair — never the clipped forecast_probability. This is the whole
        # point of the strategy: represent a 1-20% tail fair honestly.
        fair_yes_raw = _forecast_probability_raw(
            spec, forecast, mae_override=mae_dynamic,
            bias_override=bias, mu_override=mu_override)
        if fair_yes_raw is None:
            continue
        fair_yes_raw = max(0.0, min(1.0, float(fair_yes_raw)))

        edge = compute_edge(fair_yes_raw, implied)
        side = edge["best_side"]
        if side is None or edge["edge_pp_at_best"] < args.cc_min_edge_pp:
            continue
        entry_price = (implied["yes_ask"] if side == "YES"
                       else implied["no_ask"])
        if not (args.cc_min_entry_price <= entry_price <= args.cc_max_entry_price):
            continue

        # Gate A: entry-side slippage (BUY) must fit under --max-slippage.
        book_side = book_yes if side == "YES" else book_no
        buy = compute_max_size_for_slippage(book_side, "BUY", args.max_slippage)
        if buy["max_shares"] == 0:
            _cc_skip(slug, spec.city, "entry_slippage_infeasible",
                     {"side": side, "entry_price": entry_price})
            continue
        # Gate B: exit liquidity — the cashout must be feasible on the BID at
        # <= --max-slippage, checked BEFORE proposing. Cheap tail bins are
        # thin; no exit book means no strategy.
        sell = compute_max_size_for_slippage(book_side, "SELL", args.max_slippage)
        if sell["max_shares"] == 0:
            _cc_skip(slug, spec.city, "no_exit_liquidity",
                     {"side": side, "entry_price": entry_price})
            continue

        fair_target = fair_yes_raw if side == "YES" else 1.0 - fair_yes_raw
        best_bid = (book_side.get("bids") or [{}])[0].get("price", 0.0)
        with db.connect() as conn:
            # Duplicate/opposite guard: skip if any live entry on this slug.
            existing = conn.execute(
                "SELECT side, status FROM entries e "
                "LEFT JOIN cashouts c ON c.entry_id = e.entry_id "
                "WHERE e.market_slug = ? AND c.cashout_id IS NULL "
                "  AND e.status IN ('PROPOSED','APPROVED','ADJUSTED',"
                "'EXECUTED','FAST_PATH')",
                (slug,)).fetchall()
            if existing:
                continue
            entry_id = db.insert_entry(
                conn, ts=_now_iso(), market_slug=slug,
                market_question=question,
                condition_id=m.get("conditionId", ""),
                token_id_yes=token_id_yes, token_id_no=token_id_no,
                end_date=end_date_str, side=side, entry_price=entry_price,
                forecast_prob_at_entry=(fair_yes_raw if side == "YES"
                                        else 1.0 - fair_yes_raw),
                implied_prob_at_entry=entry_price,
                edge_pp_at_entry=edge["edge_pp_at_best"],
                forecast_snapshot_json=forecast,
                parser_confidence=spec.confidence, city_resolved=spec.city,
                threshold_value=spec.threshold_value,
                threshold_unit=spec.threshold_unit,
                comparison=spec.comparison, ttr_hours_at_entry=ttr_hours,
                status="PROPOSED", strategy="cheap_convexity",
                ladder_group_id=None, ladder_position=None,
                discovery_meta_json={
                    **mae_meta,
                    "ask_at_entry": entry_price,
                    "bid_at_entry": best_bid,
                    "fair_yes_raw": round(fair_yes_raw, 4),
                    "fair_target": round(fair_target, 4),
                    "entry_slippage_pct": buy.get("slippage_pct"),
                    "exit_liquidity_shares": sell.get("max_shares"),
                })
            conn.commit()
        proposed += 1
        log_event("cheap_convexity_proposed", {
            "entry_id": entry_id, "slug": slug, "side": side,
            "entry_price": entry_price, "fair_target": round(fair_target, 4),
            "edge_pp": edge["edge_pp_at_best"],
            "exit_liquidity_shares": sell.get("max_shares")})

    log_event("cheap_convexity_discovery_end", {"proposed": proposed})
    return proposed


def run_execute(args) -> int:
    """Pick up APPROVED entries and execute them via paper_engine.

    The weather_edge.db connection is opened briefly per-entry rather than
    held across HTTP calls, so the judge daemon can write between executions.
    """
    executed = 0

    # Read approved entries with a short-lived connection
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

    # v11: hard-enforced risk gate. If the portfolio breached the drawdown
    # halt or the daily-loss limit, block ALL new entries this cycle. Open
    # positions are unaffected (they resolve / cash out on their own).
    block_reason = _risk_block_reason(engine, args)
    if block_reason:
        log_event("risk_halt_block",
                  {"reason": block_reason, "n_pending": len(rows)},
                  level="WARN")
        return 0

    # v9: atomic-execution gate. Tracks ladder groups already processed
    # (either executed atomically OR marked dead) in this single
    # run_execute call so a 3-leg ladder doesn't get attempted 3 times.
    groups_handled: set[str] = set()

    for row in rows:
        entry_id = row["entry_id"]
        status = row["status"]

        # v9: gate ladder rows. Single-bin rows (no ladder_group_id)
        # fall through to legacy path unchanged.
        ladder_group_id = row["ladder_group_id"] if "ladder_group_id" in row.keys() else None
        if ladder_group_id:
            if ladder_group_id in groups_handled:
                continue
            gate_decision = _ladder_atomic_gate(ladder_group_id, entry_id)
            if gate_decision == "DEFER":
                # At least one sibling is still PROPOSED (waiting for
                # judge). Skip this leg now; retry next executor cycle.
                continue
            if gate_decision == "DEAD":
                # Some sibling was REJECTED/SKIPPED — mark all surviving
                # APPROVED/ADJUSTED legs as SKIPPED with sibling_failed
                # reason. They will not execute.
                _ladder_mark_dead(ladder_group_id, reason="ladder_sibling_failed")
                groups_handled.add(ladder_group_id)
                continue
            if gate_decision == "READY":
                # All legs APPROVED/ADJUSTED — run atomic execution of
                # the whole group. If any leg's pre-check fails, roll
                # back the whole group with ladder_partial_failure.
                n_ok = _execute_ladder_group_atomic(
                    ladder_group_id, args, engine, DEFAULT_FEE_RATE)
                executed += n_ok
                groups_handled.add(ladder_group_id)
                continue

        # --- Legacy single-bin path (unchanged from pre-v9) ---
        # ADJUST verdict can override side — if the judge thought the bot
        # picked the wrong direction, use the judge's adjusted_side instead.
        adjusted_side = row["judge_adjusted_side"] if status == "ADJUSTED" else None
        side = adjusted_side or row["side"]
        token_id = row["token_id_yes"] if side == "YES" else row["token_id_no"]
        # HTTP — no DB lock held here
        book = fetch_orderbook(token_id)
        if not book or not book.get("asks"):
            log_event("execute_skipped", {"entry_id": entry_id,
                                          "reason": "no_orderbook"})
            continue

        sizing = compute_max_size_for_slippage(book, "BUY",
                                               max_slippage=args.max_slippage)
        if sizing["max_shares"] == 0:
            log_event("execute_skipped", {"entry_id": entry_id,
                                          "reason": "zero_max_size"})
            continue

        # Re-validate edge at current market prices. The entry was approved
        # at proposal time T0; by the time we get here (queued behind the
        # position-limit cap, judge backlog, etc.) the market may have moved.
        # By DB convention, forecast_prob_at_entry stores P(side) — NOT P(YES)
        # — and fill_price is the current ask for the chosen side (= implied
        # P(side) under the market). Edge is therefore P(side) - fill_price
        # without any side-conditional flip.
        fill_price = float(sizing["avg_fill"])
        forecast_prob = row["forecast_prob_at_entry"]
        if forecast_prob is None:
            current_edge_pp = None
        else:
            current_edge_pp = round(
                (float(forecast_prob) - fill_price) * 100.0, 4)

        min_edge_pp_for_execute = getattr(args, "execute_min_edge_pp", None)
        if min_edge_pp_for_execute is None:
            min_edge_pp_for_execute = args.min_edge_pp

        if (current_edge_pp is not None
                and current_edge_pp < min_edge_pp_for_execute):
            log_event("execute_skipped", {
                "entry_id": entry_id,
                "reason": "edge_stale",
                "original_edge_pp": row["edge_pp_at_entry"],
                "current_edge_pp": round(current_edge_pp, 2),
                "fill_price": round(fill_price, 4),
                "forecast_prob": round(float(forecast_prob), 4),
                "min_edge_pp_threshold": min_edge_pp_for_execute,
                "side": side,
            })
            with db.connect() as conn2:
                db.update_entry_status(conn2, entry_id, "SKIPPED",
                                        skip_reason="edge_stale")
            continue

        # Per-trade size is driven by orderbook slippage cap (volume/depth)
        # and the per-market exposure cap below. The 10% portfolio cap was
        # removed at operator request — only paper-engine's internal risk
        # checks (insufficient balance) still gate at the engine layer.
        target_usd = float(sizing["max_usd"])
        # v11 cheap_convexity: tail bets mostly expire at zero, so cap the
        # depth-driven size with half-Kelly ∩ --cc-max-size-pct of portfolio
        # (CLAUDE.md §2). forecast_prob_at_entry stores P(side) = the raw fair.
        row_strategy = row["strategy"] if "strategy" in row.keys() else None
        if row_strategy == "cheap_convexity":
            try:
                pv = float(engine.get_portfolio().get("total_value") or 0.0)
            except Exception:
                pv = 0.0
            p_side = row["forecast_prob_at_entry"]
            cc_usd = _cheap_convexity_target_usd(
                pv, float(p_side or 0.0), fill_price, args)
            log_event("cheap_convexity_sizing", {
                "entry_id": entry_id, "portfolio_value": round(pv, 2),
                "p_side": float(p_side or 0.0), "fill_price": round(fill_price, 4),
                "depth_max_usd": round(target_usd, 2),
                "cc_target_usd": round(cc_usd, 2)})
            target_usd = min(target_usd, cc_usd)
        # Honor judge's size cap for ADJUSTED entries — usually half or a
        # third of the full size when judge has medium confidence.
        judge_size_cap = row["judge_adjusted_size_usd"] if status == "ADJUSTED" else None
        if judge_size_cap is not None and judge_size_cap > 0:
            target_usd = min(target_usd, float(judge_size_cap))
            log_event("execute_size_adjusted", {
                "entry_id": entry_id,
                "judge_size_cap_usd": float(judge_size_cap),
                "applied_target_usd": target_usd,
                "adjusted_side": adjusted_side,
            })

        # Per-market exposure cap: sum YES+NO open positions on this slug,
        # leave at most args.max_market_exposure_usd in any single market.
        market_slug = row["market_slug"]
        with db.connect() as conn2:
            current_exposure = db.current_market_exposure_usd(conn2, market_slug)
        remaining_cap = float(args.max_market_exposure_usd) - current_exposure
        if remaining_cap <= 0:
            log_event("execute_skipped", {"entry_id": entry_id,
                                          "reason": "market_exposure_cap_reached",
                                          "market_slug": market_slug,
                                          "current_exposure_usd": round(current_exposure, 2),
                                          "cap_usd": float(args.max_market_exposure_usd)})
            continue
        if remaining_cap < target_usd:
            log_event("execute_market_exposure_clamp", {
                "entry_id": entry_id, "market_slug": market_slug,
                "original_target_usd": round(target_usd, 2),
                "current_exposure_usd": round(current_exposure, 2),
                "cap_usd": float(args.max_market_exposure_usd),
                "clamped_target_usd": round(remaining_cap, 2),
            })
            target_usd = remaining_cap

        if target_usd < 10:
            log_event("execute_skipped", {"entry_id": entry_id,
                                          "reason": "size_below_min_$10"})
            continue

        if args.dry_run:
            log_event("execute_dry_run", {"entry_id": entry_id,
                                          "side": side, "target_usd": target_usd,
                                          "avg_fill": sizing["avg_fill"],
                                          "slippage_pct": sizing["slippage_pct"]})
            with db.connect() as conn2:
                db.update_entry_status(conn2, entry_id, "EXECUTED",
                                       size_usd=target_usd,
                                       size_shares=sizing["max_shares"],
                                       entry_price=sizing["avg_fill"])
            executed += 1
            continue

        # Real paper execution via PaperEngine — touches portfolio.db, not weather_edge.db
        try:
            result = engine.open_position(
                token_id=token_id,
                side=side,
                size_usd=target_usd,
                market_question=row["market_question"][:200],
                fee_rate=DEFAULT_FEE_RATE,
                confidence=0.65,
                reasoning=f"weather_edge_bot entry_id={entry_id}",
            )
            # Brief weather_edge.db connection just for the status update
            with db.connect() as conn2:
                if result.get("status") == "executed":
                    db.update_entry_status(conn2, entry_id, "EXECUTED",
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
                    db.update_entry_status(conn2, entry_id, "SKIPPED",
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
    """Check each open position; if its adaptive interval elapsed, do a check.

    Reads the open-position list with a short-lived connection, then iterates
    without holding any DB lock. Each _do_monitor_check opens its own brief
    connection only when it needs to write — letting the judge daemon write
    in the gaps.
    """
    now_mono = time.monotonic()
    with db.connect() as conn:
        rows = db.query_open_positions(conn)
        # Materialize the rows now; the connection will be closed below.
        rows = [dict(r) for r in rows]

    for row in rows:
        entry_id = row["entry_id"]
        ttr_h = _ttr_hours(row["end_date"] or "")
        interval = _monitor_interval_for_ttr(ttr_h)
        last = _last_monitor_per_entry.get(entry_id, 0)
        if now_mono - last < interval:
            continue
        _last_monitor_per_entry[entry_id] = now_mono
        _do_monitor_check(None, row, cities, args)


def _do_monitor_check(conn, row, cities: dict, args) -> None:
    """Run one monitor check for an entry. The `conn` arg is ignored —
    HTTP calls happen first without holding any DB lock, then a brief
    connection is opened for the writes at the end."""
    entry_id = row["entry_id"]
    side = row["side"]
    spec = parse_market(row["market_question"], row["end_date"], cities)
    # v8: use resolution-station coords if available
    station = resolve_station(spec.city, cities) if spec else None
    # HTTP — no DB lock
    if spec and station:
        forecast = fetch_forecast(spec.city,
                                    lat=station["lat"], lon=station["lon"])
    else:
        forecast = fetch_forecast(spec.city) if spec else None
    if not spec or not forecast:
        log_event("monitor_check", {"entry_id": entry_id, "decision": "HOLD",
                                    "reason": "no_forecast_or_spec"})
        return
    # v7+v8+v13: dynamic MAE + per-city bias + (when available) NGR-
    # calibrated ensemble σ + ensemble mean μ. Also writes new forecast
    # snapshots to forecast_history.
    mae_dyn, bias, mu_over, mae_meta = _compute_mae_for_market(
        spec, forecast, args, station=station)
    ens_cal = bool(mae_meta.get("ensemble_calibrated"))
    forecast_prob_yes = forecast_probability(spec, forecast,
                                               mae_override=mae_dyn,
                                               bias_override=bias,
                                               mu_override=mu_over)
    if forecast_prob_yes is None:
        return
    # v12.1/v13: range markets return raw P(YES); cap the held side's
    # confidence for the cashout math so convergence/forecast-reversal use
    # the same bounded P(side) the entry was sized with. Cap is 0.70 in
    # the legacy MAE path, 0.95 in the ensemble_calibrated path.
    forecast_prob_yes = prob_yes_for_sizing(forecast_prob_yes, side,
                                            spec.comparison,
                                            ensemble_calibrated=ens_cal)

    # v11 cheap_convexity: exit on the RAW fair (never the capped value the
    # legacy triggers use). Recompute the raw P(YES) with the same overrides.
    row_strategy = row["strategy"] if "strategy" in row.keys() else None
    is_cc = row_strategy == "cheap_convexity"
    cc_fair_yes_raw = None
    if is_cc:
        cc_fair_yes_raw = _forecast_probability_raw(
            spec, forecast, mae_override=mae_dyn,
            bias_override=bias, mu_override=mu_over)

    token_id = row["token_id_yes"] if side == "YES" else row["token_id_no"]
    book = fetch_orderbook(token_id)  # HTTP
    if not book:
        return
    bid = book["bids"][0]["price"] if book.get("bids") else 0.0
    ask = book["asks"][0]["price"] if book.get("asks") else 0.0
    entry_price = float(row["entry_price"])

    prev_peak = float(row["peak_bid_seen"] or 0.0)
    peak = max(prev_peak, bid)

    # v11: raw forecast value for the range_cross stop-loss trigger.
    forecast_ref = forecast_ref_value(spec, forecast)
    verdict = evaluate_cashout_triggers(
        side=side,
        entry_price=entry_price,
        current_bid=bid,
        peak_bid_seen=peak,
        forecast_prob_yes=forecast_prob_yes,
        profit_lock_pp=args.profit_lock_pp,
        trailing_drawdown_pct=args.trailing_drawdown_pct,
        convergence_pp=args.convergence_pp,
        comparison=spec.comparison,
        forecast_value=forecast_ref,
        range_low=spec.threshold_value,
        range_high=spec.threshold_value_high,
        enable_fair_target=is_cc and cc_fair_yes_raw is not None,
        fair_uncapped_yes=cc_fair_yes_raw,
        fair_target_margin_pp=getattr(args, "cc_exit_margin_pp", 1.0),
    )
    decision = verdict["decision"]
    trigger = verdict["trigger"]
    reason = f"{trigger}: {verdict['reason']}"
    forecast_prob_now = forecast_prob_yes if side == "YES" else 1.0 - forecast_prob_yes

    # v11 cheap_convexity: a cashout is only real if the bid book can absorb
    # the position at <= --max-slippage. Cheap tail bins are thin, so re-check
    # exit liquidity right before selling; if the bid can't take it, record
    # TRY_CASHOUT_BLOCKED and hold (do not dump into an empty book).
    if decision == "CASHOUT" and is_cc:
        sell = compute_max_size_for_slippage(book, "SELL", args.max_slippage)
        held_shares = float(row["size_shares"] or 0.0)
        if sell["max_shares"] <= 0 or (held_shares > 0
                                       and sell["max_shares"] < held_shares):
            decision = "TRY_CASHOUT_BLOCKED"
            reason = (f"exit_liquidity_insufficient: sell_max "
                      f"{sell['max_shares']:.1f} < held {held_shares:.1f} "
                      f"at <= {args.max_slippage:.0%} slippage")
            log_event("cheap_convexity_cashout_blocked", {
                "entry_id": entry_id, "held_shares": held_shares,
                "sell_max_shares": sell["max_shares"], "bid": bid})

    # Brief write-only connection
    with db.connect() as conn2:
        if bid > prev_peak:
            conn2.execute(
                "UPDATE entries SET peak_bid_seen = ?, peak_bid_seen_at = ? "
                "WHERE entry_id = ?",
                (bid, _now_iso(), entry_id),
            )
        db.insert_monitor_check(
            conn2, entry_id=entry_id,
            ts=_now_iso(),
            forecast_prob_now=forecast_prob_now,
            forecast_snapshot_json=forecast,
            market_best_bid=bid,
            market_best_ask=ask,
            decision=decision,
            decision_reason=reason,
        )
    log_event("monitor_check", {"entry_id": entry_id, "decision": decision,
                                "trigger": trigger,
                                "forecast_prob_now": forecast_prob_now,
                                "entry_price": entry_price,
                                "bid": bid, "peak": peak,
                                "reason": verdict["reason"]})

    if decision == "CASHOUT":
        _do_cashout(None, row, bid, forecast, forecast_prob_now, args, reason)


# v9: groups currently mid-cashout. Prevents the monitor from triggering
# duplicate close_position calls for sibling legs in the same cycle when
# multiple triggers fire concurrently.
_ladder_cashing_out: set[str] = set()


def _do_cashout(conn, row, bid: float, forecast: dict,
                forecast_prob_now: float, args, reason: str) -> None:
    # v9: if this entry is part of a ladder group, cash out ALL legs of
    # the group atomically. Single-bin entries (ladder_group_id NULL)
    # fall through to the legacy per-leg path.
    ladder_group_id = row["ladder_group_id"] if "ladder_group_id" in row.keys() else None
    if ladder_group_id:
        if ladder_group_id in _ladder_cashing_out:
            # A sibling is already handling the group cashout in this cycle
            return
        _ladder_cashing_out.add(ladder_group_id)
        try:
            _do_ladder_cashout(ladder_group_id, forecast, forecast_prob_now,
                                args, reason, trigger_entry_id=row["entry_id"])
        finally:
            _ladder_cashing_out.discard(ladder_group_id)
        return

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
            exit_price = result.get("avg_sell_price") or result.get("avg_price")
            with db.connect() as conn2:
                db.insert_cashout(
                    conn2, entry_id=entry_id,
                    ts=_now_iso(),
                    exit_price=exit_price,
                    exit_shares=result.get("shares_sold"),
                    realized_pnl_usd=result.get("realized_pnl"),
                    forecast_prob_at_exit=forecast_prob_now,
                    forecast_snapshot_json=forecast,
                    reason=reason[:200],
                )
            log_event("cashout_executed", {"entry_id": entry_id,
                                            "exit_price": exit_price,
                                            "pnl": result.get("realized_pnl")})
        else:
            log_event("cashout_rejected", {"entry_id": entry_id,
                                            "reason": result.get("reason")})
    except Exception as e:
        log_event("error", {"where": "cashout", "entry_id": entry_id, "err": str(e)})


def _do_ladder_cashout(group_id: str, forecast: dict,
                        forecast_prob_now: float, args, reason: str,
                        trigger_entry_id: int) -> None:
    """v9: atomic cashout of all legs in a ladder group. Triggered by
    any one leg's cashout signal (convergence/trailing/profit-lock).
    Closes all legs via paper_engine, then writes all cashouts in a
    single DB transaction. Partial failures are logged loudly but
    leave the partial-close state intact (no rollback in paper_engine).
    """
    with db.connect() as conn:
        legs = db.query_ladder_group(conn, group_id)
    # Only legs that are still open (no cashout row yet)
    open_legs = []
    with db.connect() as conn:
        for leg in legs:
            r = conn.execute(
                "SELECT cashout_id FROM cashouts WHERE entry_id = ?",
                (leg["entry_id"],)).fetchone()
            if r is None:
                open_legs.append(leg)
    if not open_legs:
        return

    if args.dry_run:
        log_event("ladder_cashout_dry", {
            "ladder_group_id": group_id, "n_legs": len(open_legs),
            "trigger_entry_id": trigger_entry_id, "reason": reason})
        return

    try:
        from paper_engine import PaperEngine
        engine = PaperEngine(portfolio=args.portfolio)
    except ImportError as e:
        log_event("error", {"where": "ladder_cashout",
                             "err": f"paper_engine import: {e}",
                             "ladder_group_id": group_id}, level="ERROR")
        return

    # Phase 1: close each leg's position via the engine, accumulating
    # results in memory. No DB writes until all closes attempted.
    results = []
    for leg in open_legs:
        side = leg["side"]
        token_id = (leg["token_id_yes"] if side == "YES"
                     else leg["token_id_no"])
        try:
            r = engine.close_position(
                token_id=token_id, side=side,
                reasoning=(f"weather_edge_bot ladder group={group_id[:8]} "
                            f"trigger={reason}"))
        except Exception as e:
            log_event("error", {"where": "ladder_cashout_close_position",
                                "entry_id": leg["entry_id"],
                                "ladder_group_id": group_id,
                                "err": str(e)}, level="ERROR")
            r = {"status": "failed", "reason": str(e)}
        results.append((leg, r))

    # Phase 2: write all cashout rows in a single transaction. Even if
    # some closes failed at the engine level, we persist what succeeded
    # so the DB matches paper_engine state.
    successes = 0
    with db.connect() as conn:
        for leg, r in results:
            if r.get("status") == "closed":
                exit_price = r.get("avg_sell_price") or r.get("avg_price")
                db.insert_cashout(
                    conn, entry_id=leg["entry_id"],
                    ts=_now_iso(),
                    exit_price=exit_price,
                    exit_shares=r.get("shares_sold"),
                    realized_pnl_usd=r.get("realized_pnl"),
                    forecast_prob_at_exit=forecast_prob_now,
                    forecast_snapshot_json=forecast,
                    reason=f"ladder_group:{reason}"[:200],
                )
                successes += 1
            else:
                log_event("ladder_leg_close_rejected", {
                    "entry_id": leg["entry_id"],
                    "ladder_group_id": group_id,
                    "reason": r.get("reason"),
                }, level="WARN")
        conn.commit()

    total_pnl = sum(r.get("realized_pnl", 0) or 0
                     for _, r in results if r.get("status") == "closed")
    log_event("ladder_cashout_executed", {
        "ladder_group_id": group_id,
        "trigger_entry_id": trigger_entry_id,
        "trigger_reason": reason,
        "n_legs_total": len(open_legs),
        "n_legs_closed": successes,
        "total_pnl_usd": round(total_pnl, 4),
    })


# ---------------------------------------------------------------------------
# Resolution sweep
# ---------------------------------------------------------------------------


# v9.11.1: Polymarket sometimes archives bracket sub-market URLs after
# settlement but keeps the parent event live via /events. Try both.
_RESOLUTION_SUFFIX_RE = _re.compile(
    r"-\d{1,3}(?:-\d{1,3})?[cf]?(?:or\w+)?$", _re.IGNORECASE)


def _fetch_resolved_market(slug: str) -> Optional[dict]:
    """Return Gamma market dict for `slug`, falling back to /events?slug=parent
    when /markets?slug= returns empty (typical for archived sub-markets)."""
    if not slug:
        return None
    try:
        r = requests.get(f"{GAMMA_API}/markets",
                         params={"slug": slug}, timeout=15)
        r.raise_for_status()
        results = r.json()
        if isinstance(results, list) and results:
            return results[0]
    except requests.RequestException:
        pass
    # Parent fallback
    parent = _RESOLUTION_SUFFIX_RE.sub("", slug)
    if parent == slug or not parent:
        return None
    try:
        r = requests.get(f"{GAMMA_API}/events",
                         params={"slug": parent}, timeout=15)
        r.raise_for_status()
        events = r.json()
        if not isinstance(events, list) or not events:
            return None
        sub_markets = events[0].get("markets") or []
        for sm in sub_markets:
            if sm.get("slug") == slug:
                return sm
        # Suffix-match in case the canonical slug got rewritten
        suffix = slug[len(parent):]
        if suffix:
            for sm in sub_markets:
                if (sm.get("slug") or "").endswith(suffix):
                    return sm
    except requests.RequestException:
        pass
    return None


def _observed_value_for(row, cities) -> Optional[float]:
    """v11: realized temp extreme for this entry's target date, in the
    market's unit, from the Open-Meteo archive. v13.4: lowest-temperature
    markets record the observed MIN (was always the max — which made the
    advisor's calibration data garbage for low markets). Returns None when
    the market isn't a temp market, the station coords are unknown, or the
    archive has no data yet (~1-2 day lag). Used to populate
    resolutions.observed_value so the advisor can calibrate against truth."""
    try:
        city = row["city_resolved"]
        end_date = (row["end_date"] or "")[:10]
        unit = (row["threshold_unit"] or "").upper()
        slug = (row["market_slug"] or "").lower()
        question = (row["market_question"] or "").lower()
    except Exception:
        return None
    if not city or not end_date or unit not in ("C", "F"):
        return None
    station = resolve_station(city, cities)
    if not station or station.get("lat") is None or station.get("lon") is None:
        return None
    arch = fetch_open_meteo_archive(station["lat"], station["lon"], end_date)
    if not arch:
        return None
    is_low = slug.startswith("lowest") or "lowest temperature" in question
    key = ("observed_min_" if is_low else "observed_max_") + unit.lower()
    return arch.get(key)


def _decide_final_outcome(prices: list, gamma_closed: bool) -> Optional[str]:
    """Decide YES/NO/VOID from Gamma outcomePrices + the closed flag.

    v13.3 (2026-07-05): the previous inline logic was
        if gamma_closed or prices[0] >= 0.95: final = "YES"
        elif gamma_closed or prices[1] >= 0.95: final = "NO"
    — when Polymarket marks the market `closed`, the FIRST branch is always
    true, so EVERY officially-closed market resolved as "YES" regardless of
    prices. All 17 positions settled by the 2026-07-05 on-demand sweep were
    recorded as losses, including NO bets the operator verifiably WON (e.g.
    Paris July-3 14°C bracket resolved "No" on Polymarket). The daemon only
    got outcomes right when it swept BEFORE Gamma flipped `closed` (deciding
    purely by price). Correct order — prices first, `closed` only as the
    VOID/not-settled discriminator (same logic force_resolution_sweep.py
    already had):

    Returns "YES" / "NO" / "VOID" (closed but no winner price — inconclusive)
    or None (not closed and prices inconclusive — try next sweep).
    YES is index 0, NO is index 1 (Polymarket convention). Threshold 0.95:
    markets often settle at 0.97/0.03 before the official closed flag flips.
    """
    price_threshold = 0.95
    if prices[0] >= price_threshold:
        return "YES"
    if prices[1] >= price_threshold:
        return "NO"
    if gamma_closed:
        return "VOID"
    return None  # not yet settled


def run_resolution_sweep() -> int:
    """For each EXECUTED position past end_date, fetch outcomePrices and persist.
    On resolution, also close the paper-portfolio position at payout so the
    slot is freed for new bets."""
    resolved = 0
    cities = load_cities()
    try:
        import paper_engine
    except ImportError as e:
        log_event("error", {"where": "resolution_sweep",
                            "err": f"paper_engine import: {e}"},
                  level="ERROR")
        return 0
    with db.connect() as conn:
        rows = db.query_unresolved_past_end(conn, _now_iso())
        log_event("resolution_sweep_started", {"unresolved_past_end": len(rows)})
        for row in rows:
            slug = row["market_slug"]
            try:
                # v9.11.1: try /markets first, fall back to /events?slug=parent
                # because Polymarket sometimes archives the sub-market URL
                # after settlement but keeps the parent event live.
                m = _fetch_resolved_market(slug)
                if m is None:
                    log_event("resolution_skipped", {
                        "entry_id": row["entry_id"], "slug": slug,
                        "reason": "market_not_found_in_gamma"})
                    continue
                outcomes = json.loads(m.get("outcomes", "[]"))
                prices = [float(p) for p in json.loads(m.get("outcomePrices", "[]"))]
                if not outcomes or not prices or len(outcomes) != len(prices):
                    log_event("resolution_skipped", {
                        "entry_id": row["entry_id"], "slug": slug,
                        "reason": "no_outcomes_or_prices"})
                    continue
                # v13.3 (2026-07-05): outcome decision extracted to
                # _decide_final_outcome. The v9.9 inline version had
                # `gamma_closed OR price` in BOTH branches, so every
                # officially-closed market resolved "YES" — see the
                # post-mortem in _decide_final_outcome's docstring.
                gamma_closed = bool(m.get("closed"))
                final_outcome = _decide_final_outcome(prices, gamma_closed)
                if final_outcome is None:
                    # Not yet resolved — try again next sweep cycle.
                    log_event("resolution_skipped", {
                        "entry_id": row["entry_id"], "slug": slug,
                        "reason": "not_yet_settled",
                        "prices": prices, "closed": gamma_closed})
                    continue
                payout = 1.0 if (final_outcome == row["side"]) else 0.0
                if final_outcome == "VOID":
                    payout = float(row["entry_price"] or 0)  # neutral
                observed_value = _observed_value_for(row, cities)
                db.insert_resolution(
                    conn, entry_id=row["entry_id"],
                    ts_resolved=_now_iso(),
                    final_outcome=final_outcome,
                    payout_per_share=payout,
                    observed_value=observed_value,
                )
                resolved += 1
                log_event("resolution_observed", {"entry_id": row["entry_id"],
                                                   "slug": slug,
                                                   "outcome": final_outcome,
                                                   "payout": payout,
                                                   "observed_value": observed_value})

                # Close the corresponding paper-portfolio position at the
                # resolution payout. This credits proceeds + frees the
                # max_concurrent_positions slot. If the position was already
                # cashed out earlier (no paper position open), skip silently.
                token_id = (row["token_id_yes"] if row["side"] == "YES"
                            else row["token_id_no"])
                if token_id:
                    try:
                        close_result = paper_engine.close_position(
                            token_id=token_id, side=row["side"],
                            reasoning=f"resolution:{final_outcome}",
                            force_exit_price=payout,
                        )
                        log_event("resolution_closed", {
                            "entry_id": row["entry_id"],
                            "token_id": token_id,
                            "side": row["side"],
                            "payout": payout,
                            "realized_pnl": close_result.get("realized_pnl"),
                            "new_balance": close_result.get("new_balance"),
                        })
                    except RuntimeError as ce:
                        # Most common: "No open position for token..."
                        # Means the position was already closed via cashout
                        # before resolution. Not an error.
                        log_event("resolution_close_skipped", {
                            "entry_id": row["entry_id"],
                            "reason": str(ce),
                        })
                    except Exception as ce:
                        log_event("error", {
                            "where": "resolution_close_position",
                            "entry_id": row["entry_id"], "err": str(ce),
                        }, level="WARN")
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


def _check_portfolio_thresholds() -> None:
    """v6 Tier 4A: emit notifications when portfolio crosses risk thresholds.

    Lazy-imports the dashboard's portfolio service + notifier so this is a
    no-op when neither is available (e.g. during pure-bot deploys without
    the dashboard installed).

    Thresholds (CLAUDE.md §2):
      - drawdown > 10% → warning
      - drawdown > 15% → critical
      - daily realized loss > 3% of starting → warning
      - daily realized loss > 5% of starting → critical
    """
    try:
        sys.path.insert(0, str(REPO_ROOT))
        from dashboard.services import portfolio as pf_svc, notifier
    except ImportError:
        return  # dashboard not on this host, skip silently

    if not notifier.is_configured():
        return

    try:
        kpis = pf_svc.get_kpis()
    except Exception:
        return

    dd = kpis.get("drawdown_pct_from_peak", 0)
    starting = kpis.get("starting_balance_usd", 0) or 0
    realized = kpis.get("realized_pnl_today_usd", 0) or 0
    daily_loss_pct = -(realized / starting * 100) if starting > 0 else 0
    total = kpis.get("portfolio_total_usd", 0)

    body_template = (
        f"Portfolio: ${total:.2f}\n"
        f"Drawdown from peak: {dd:.2f}%\n"
        f"Realized today: ${realized:+.2f} ({-daily_loss_pct:+.2f}%)"
    )

    if dd <= -15:
        notifier.send("critical", "Drawdown CRITICAL > 15%", body_template,
                       rate_limit_key="dd_critical")
    elif dd <= -10:
        notifier.send("warning", "Drawdown warning > 10%", body_template,
                       rate_limit_key="dd_warning")

    if daily_loss_pct >= 5:
        notifier.send("critical",
                       "Daily loss CRITICAL > 5% — entries blocked",
                       body_template, rate_limit_key="dl_critical")
    elif daily_loss_pct >= 3:
        notifier.send("warning", "Daily loss warning > 3%",
                       body_template, rate_limit_key="dl_warning")


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--min-edge-pp", type=float, default=20.0,
                   help="Minimum edge_pp required at DISCOVERY time. "
                        "Default 20 (lowered from 25 on 2026-05-16: snapshot "
                        "showed 100%% of mid-band [0.30, 0.85] proposals "
                        "had edge >= 25pp because v6 clipping forces "
                        "bot_prob to {0.10, 0.90} extremes — so 20pp is "
                        "ample headroom and stays above the 10-20pp "
                        "cohort that historically had near-zero win rate).")
    p.add_argument("--range-min-bin-gap-mae", type=float, default=1.0,
                   help="For range/bracket markets, skip a NO bet at "
                        "DISCOVERY when the forecast is within X × MAE of the "
                        "nearest bin edge (default 1.0). In that zone the "
                        "Gaussian model reports a high P(NO) that is illusory "
                        "— the ~1°C bin is inside the forecast's own "
                        "uncertainty and the realized temp lands in it ~92%% "
                        "of the time (advisor sug_005, -$740 week). Saves the "
                        "judge LLM cost; the judge's proximity REJECT / range "
                        "ADJUST remain backstops. Set 0 to disable.")
    p.add_argument("--execute-min-edge-pp", type=float, default=8.0,
                   help="Minimum edge_pp required at EXECUTION time. "
                        "Default 8 (lowered from 12 on 2026-05-16: snapshot "
                        "showed 343 mid-band proposals all skipped as "
                        "edge_stale at 12pp threshold; 8pp is the floor "
                        "where post-judge edge decay still leaves room "
                        "above the loss-prone 4-8pp band).")
    p.add_argument("--min-volume", type=float, default=100,
                   help="Min market USD volume; sub-bracket markets have low volume each")
    p.add_argument("--min-price", type=float, default=0.05,
                   help="Min entry-side price (0.05 default; tail brackets often "
                        "have biggest payout asymmetry and biggest edge)")
    p.add_argument("--max-price", type=float, default=0.95,
                   help="Max entry-side price (0.95 default)")
    p.add_argument("--max-slippage", type=float, default=0.20)
    p.add_argument("--window-hours", type=float, default=48)
    p.add_argument("--daemon", action="store_true", default=True)
    p.add_argument("--once", action="store_true",
                   help="Run discovery+monitor once and exit")
    p.add_argument("--discovery-interval-min", type=float, default=60,
                   help="Minutes between discovery cycles (default 60). "
                        "Discovery scans ~1249 markets which takes 5-15 min; "
                        "10 min was too tight and starved the judge of DB writes.")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--judge-mode", choices=("sync", "off"), default="sync")
    # v7: multi-source forecast consensus during discovery
    p.add_argument("--multi-source", dest="multi_source",
                   action="store_true", default=None,
                   help="Cross-check OpenWeather with Visual Crossing during "
                        "discovery; inflates MAE on disagreement. Auto-ON if "
                        "VISUAL_CROSSING_API_KEY env is set.")
    p.add_argument("--no-multi-source", dest="multi_source",
                   action="store_false",
                   help="Disable multi-source consensus (overrides auto-detect).")
    # v8: Open-Meteo ensemble + station coords + bias + min-TTR
    p.add_argument("--open-meteo", dest="open_meteo",
                   action="store_true", default=True,
                   help="Cross-check with Open-Meteo ICON+GFS+ECMWF ensemble "
                        "(free, no API key). Inflates MAE x1.5 if 3-model "
                        "spread > 3C. Default ON. Requires station coords "
                        "in weather-cities.json 'stations' dict.")
    p.add_argument("--no-open-meteo", dest="open_meteo", action="store_false",
                   help="Disable Open-Meteo ensemble fetch.")
    p.add_argument("--min-ttr-hours", type=float, default=12.0,
                   help="Skip markets with TTR < X hours at discovery "
                        "(single-bin / orphan path). Default 12 (lowered "
                        "from 24 on 2026-06-14 after the zero-entry run: "
                        "24h floor killed 4607/7073 (65%%) of all candidates, "
                        "the entire single-bin universe. The original 24h "
                        "rationale — that TTR<18h range NO bets were the "
                        "post-mortem losers — is now over-protected by "
                        "v12 range_bin_gap_too_small + v12.1 range_edge_"
                        "after_cap + judge proximity REJECT, which all "
                        "specifically target the near-edge NO bets the TTR "
                        "floor was a blunt proxy for. Ladder mode uses the "
                        "lower --ladder-min-ttr-hours floor. Set 0 to disable.")
    p.add_argument("--ladder-min-ttr-hours", type=float, default=6.0,
                   help="Minimum TTR (hours) for a leg to enter ladder "
                        "discovery (default 6). Lower than --min-ttr-"
                        "hours 12 because laddering captures forecast "
                        "improvements in the 6-12h band (more accurate "
                        "than 24h+) without depending on point precision. "
                        "Orphan lone-bracket events still must pass "
                        "--min-ttr-hours to be admitted as single-bin. "
                        "In --ladder-mode off, --min-ttr-hours applies "
                        "uniformly.")
    # v9: upstream filters for the cheap-bet adverse-selection trap
    p.add_argument("--min-entry-price", type=float, default=0.30,
                   help="Skip trades with entry_price < X. Default 0.30 "
                        "(raised from 0.20 on 2026-05-16 for high-prob "
                        "strategic pivot: operator chose mid-to-low payoff "
                        "/ high-probability bets over the long-shot pattern "
                        "that produced -53%% unrealized loss in the 0.06-"
                        "0.20 band. Combined with --max-entry-price 0.85 "
                        "this targets the 0.30-0.85 band where the bot is "
                        "betting on outcomes the market already considers "
                        "plausible but mispriced.).")
    p.add_argument("--max-entry-price", type=float, default=0.85,
                   help="Skip trades with entry_price > X. Default 0.85 "
                        "(added 2026-05-16 with the strategic pivot: above "
                        "0.85 the payoff asymmetry collapses — a winning "
                        "$0.90 ticket only earns $0.10 so even a 5%% miss "
                        "rate eats the edge. 0.85 keeps payoff floor at "
                        "+18%% per ticket while still preferring high-prob "
                        "outcomes.).")
    # v11 cheap_convexity: separate discovery path for 1-20c tail bins.
    # OFF by default and additionally gated by cheap_convexity_gate.json
    # (produced by cheap_convexity_calibration.py). Uses the RAW model fair
    # (never the [0.30,0.70] clip) and its own price/edge floors.
    p.add_argument("--cheap-convexity", action="store_true", default=False,
                   help="Enable the cheap_convexity discovery path (buy 1-20c "
                        "tail bins below raw model fair, exit on cashout at "
                        "convergence). Still gated by the tail-calibration "
                        "gate artifact; no-op until that passes. PAPER only.")
    p.add_argument("--cc-min-entry-price", type=float, default=0.01,
                   help="cheap_convexity: min entry price (default 0.01).")
    p.add_argument("--cc-max-entry-price", type=float, default=0.20,
                   help="cheap_convexity: max entry price (default 0.20).")
    p.add_argument("--cc-min-edge-pp", type=float, default=3.0,
                   help="cheap_convexity: min edge (raw fair - price) in pp "
                        "(default 3.0).")
    p.add_argument("--cc-exit-margin-pp", type=float, default=1.0,
                   help="cheap_convexity: cash out when best_bid reaches "
                        "raw_fair - X pp (default 1.0).")
    p.add_argument("--cc-max-size-pct", type=float, default=1.0,
                   help="cheap_convexity: max position as %% of portfolio "
                        "(default 1.0; hard-capped at 5.0). half-Kelly is "
                        "applied under this cap.")
    p.add_argument("--max-disagreement-pp", type=float, default=0.0,
                   help="Skip trades where |bot_prob - market_implied| > X pp. "
                        "Adverse selection guard. Loss analysis 2026-05-15 "
                        "sensitivity: thresh=50 is net-zero (blocks Karachi "
                        "winners equal to Lucknow losers); thresh=70 is +$46 "
                        "net on 24h sample but n is small. Default 0 (OFF) "
                        "until operator validates a thresh on more data.")
    # v9: 3-bin laddering
    p.add_argument("--ladder-mode", choices=("off", "3bin"), default="3bin",
                   help="When '3bin' (default), discovery groups same-event "
                        "brackets and emits coordinated 3-leg ladders "
                        "(central + below + above) with shared "
                        "ladder_group_id, atomic execution and atomic "
                        "cashout. Set to 'off' to revert to single-bin "
                        "(legacy behavior).")
    p.add_argument("--ladder-stake-split",
                   choices=("kelly", "equal"), default="kelly",
                   help="How to split the total ladder stake across legs. "
                        "'kelly' (default) weights by per-leg Kelly fraction "
                        "(more $ where edge x prob is biggest). 'equal' "
                        "divides 1/N for A/B comparison against kelly.")
    p.add_argument("--ladder-min-leg-price", type=float, default=0.10,
                   help="Minimum entry_price for a leg to enter ladder "
                        "discovery (default 0.10). Lower than "
                        "--min-entry-price (default 0.30) because cheap "
                        "ladder legs are protected by Kelly proportional "
                        "sizing (auto-caps stake at $0 for negative-EV "
                        "legs). Orphan legs that end up alone in an event "
                        "still must pass --min-entry-price (the higher "
                        "single-bin floor). In --ladder-mode off, this "
                        "flag is ignored and --min-entry-price applies "
                        "uniformly.")
    p.add_argument("--ladder-min-leg-edge-pp", type=float, default=10.0,
                   help="Minimum edge_pp for a leg to enter ladder "
                        "discovery (default 10). Lower than --min-edge-pp "
                        "(default 20) because adjacent legs (below/above) "
                        "structurally have smaller edges than the central "
                        "— they're hedges, not standalone bets. Kelly "
                        "sizing caps stake on small-edge legs. Orphan "
                        "legs (alone in an event) must still pass "
                        "--min-edge-pp to be admitted as single-bin. In "
                        "--ladder-mode off, --min-edge-pp applies "
                        "uniformly.")
    p.add_argument("--ladder-execute-min-leg-edge-pp", type=float, default=4.0,
                   help="Minimum edge_pp at EXECUTION time for ladder "
                        "legs (default 4). Lower than --execute-min-edge-"
                        "pp (default 8) because losing any one ladder "
                        "leg to edge_stale aborts the whole group via "
                        "atomic gate. Allows the central + adjacents to "
                        "execute together even when adjacents have "
                        "decayed into the 4-8pp band.")
    p.add_argument("--fast-path-ttr-min", type=int, default=60)
    p.add_argument("--profit-lock-pp", type=float, default=50.0,
                   help="Cashout when bid >= entry + X pp (default 50pp = "
                        "+$0.50). Operator preference: keep the original 50pp "
                        "and use trailing-drawdown-pct=15 as primary exit "
                        "instead of locking profit early.")
    p.add_argument("--trailing-drawdown-pct", type=float, default=15.0,
                   help="Cashout if bid falls X%% below peak (default 15%% — "
                        "was 30%% but operator analysis 2026-05-15 showed "
                        "84%% of trades held to resolution where they lost)")
    p.add_argument("--convergence-pp", type=float, default=3.0,
                   help="Cash out when bid converges within X pp of fair "
                        "value (forecast prob). Default 3 (re-enabled "
                        "2026-06-01 from 0). Advisor backtest over the 57 "
                        "trades of the -$740 week: convergence_pp=3 lifts "
                        "P&L from -$740 to -$320 (+$420) and win rate 35%% "
                        "→ 54%% by exiting 15-27 hold_to_resolution trades "
                        "before realized weather lands inside the range bin "
                        "and zeroes the position. (v9.5 had disabled it on "
                        "the theory that laddering captures the payout at "
                        "resolution; the realized data disproved that.) "
                        "Set 0 to disable.")
    p.add_argument("--max-market-exposure-usd", type=float, default=50.0,
                   help="Total $ exposure cap per market_slug (YES+NO summed, "
                        "across all open positions). Default $50.")
    # v11 (post-mortem): hard-enforced risk gate in run_execute. CLAUDE.md §2
    # mandates these but nothing enforced them before — the bot opened 42
    # positions on the -$771 day and kept trading the next day.
    p.add_argument("--max-drawdown-halt-pct", type=float, default=20.0,
                   help="Block ALL new entries when portfolio drawdown from "
                        "peak >= X%% (default 20, per CLAUDE.md §2). Open "
                        "positions are left to resolve/cash out. Set 0 to "
                        "disable the gate.")
    p.add_argument("--daily-loss-limit-pct", type=float, default=5.0,
                   help="Block new entries when today's realized loss >= X%% "
                        "of starting balance (default 5, per CLAUDE.md §2). "
                        "Set 0 to disable.")
    p.add_argument("--portfolio", default="default")
    p.add_argument("--log-file", default=None,
                   help="Write JSONL log here (default ~/.polymarket-paper/weather_edge.jsonl)")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()

    # v9: env-var fallback for ladder flags. Lets the dashboard settings
    # page edit ladder strategy without touching the bot launch script.
    # Env var overrides argparse default but NOT explicit CLI flag (since
    # we can't tell argparse "user passed this explicitly" without more
    # work; in practice operator uses one mechanism or the other).
    _LADDER_ENV = {
        "LADDER_MODE": ("ladder_mode", str),
        "LADDER_STAKE_SPLIT": ("ladder_stake_split", str),
        "LADDER_MIN_LEG_PRICE": ("ladder_min_leg_price", float),
        "LADDER_MIN_LEG_EDGE_PP": ("ladder_min_leg_edge_pp", float),
        "LADDER_EXECUTE_MIN_LEG_EDGE_PP": ("ladder_execute_min_leg_edge_pp", float),
        "LADDER_MIN_TTR_HOURS": ("ladder_min_ttr_hours", float),
    }
    for env_name, (attr, cast) in _LADDER_ENV.items():
        v = os.environ.get(env_name)
        if v is not None and v != "":
            try:
                setattr(args, attr, cast(v))
                log_event("ladder_env_override", {
                    "var": env_name, "value": v, "attr": attr})
            except (ValueError, TypeError) as e:
                log_event("error", {"where": "ladder_env_parse",
                                     "var": env_name, "value": v,
                                     "err": str(e)}, level="WARN")

    # v7: auto-detect multi-source if user didn't pass either flag
    if args.multi_source is None:
        args.multi_source = bool(os.environ.get("VISUAL_CROSSING_API_KEY"))

    # Override log file if user asked
    global LOG_FILE
    if args.log_file:
        LOG_FILE = Path(args.log_file).resolve()
        # If user gave a relative path, make it absolute and ensure parent exists
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Truncate on --once for a clean per-run file
        if args.once:
            LOG_FILE.write_text("", encoding="utf-8")
        print(f"Logging JSONL to: {LOG_FILE}", flush=True)

    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    db.init_db()
    # Write PID file so the dashboard can find + restart us on apply.
    # Atomic write (tmp → rename) + best-effort cleanup at shutdown.
    pid_file = Path.home() / ".polymarket-paper" / "bot.pid.json"
    try:
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = pid_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "pid": os.getpid(),
            "argv": [sys.executable] + sys.argv,
            "cwd": str(Path.cwd()),
            "started_at": _now_iso(),
        }), encoding="utf-8")
        tmp.replace(pid_file)
    except OSError as e:
        log_event("warn", {"where": "pidfile_write", "err": str(e)}, level="WARN")

    cities = load_cities()
    log_event("startup", {"args": vars(args), "cities_loaded": len(cities.get("us_top50", [])) +
                          len(cities.get("world", [])) + len(cities.get("europe_top30", [])) +
                          len(cities.get("north_america_extra", []))})

    if args.once:
        run_discovery(args, cities)
        run_discovery_cheap_convexity(args, cities)  # v11, no-op unless gated flag on
        if args.judge_mode == "off":
            run_execute(args)
        run_monitor_tick(args, cities)
        run_resolution_sweep()
        try:
            size = LOG_FILE.stat().st_size
            print(f"\n=== Full log: {LOG_FILE} ({size:,} bytes) ===", flush=True)
        except OSError:
            pass
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
                run_discovery_cheap_convexity(args, cities)  # v11, gated
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
                # v6 Tier 4A: portfolio threshold checks for notifications.
                # Wrapped in try so notifier failures never crash the bot.
                try:
                    _check_portfolio_thresholds()
                except Exception as e:
                    log_event("error", {"where": "notify_thresholds",
                                         "err": str(e)}, level="WARN")
        except Exception as e:
            log_event("error", {"where": "main_loop", "err": str(e),
                                "type": type(e).__name__}, level="ERROR")

        time.sleep(MONITOR_TICK)

    log_event("shutdown_clean", {})
    try:
        pid_file.unlink(missing_ok=True)
    except (NameError, OSError):
        pass


def _test_atomic_execution():
    """v9: Hermetic tests for the atomic-execution gate (no network).
    Stubs db.query_ladder_group + db.update_entry_status."""
    import weather_edge_bot as mod

    # Build a fake leg dict mimicking sqlite3.Row.keys() behavior.
    class FakeRow(dict):
        def keys(self):
            return super().keys()

    def mk(eid, status):
        return FakeRow({"entry_id": eid, "status": status,
                        "ladder_group_id": "g1", "ladder_position": "central"})

    saved_query = db.query_ladder_group
    saved_update = db.update_entry_status
    saved_connect = db.connect
    updates = []

    class FakeConn:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def commit(self): pass
        def execute(self, *a, **kw): return self
        def fetchone(self): return None

    db.connect = lambda: FakeConn()
    db.update_entry_status = lambda conn, eid, status, **kw: updates.append((eid, status, kw))

    try:
        # Test 1: all legs APPROVED → READY
        db.query_ladder_group = lambda conn, gid: [
            mk(1, "APPROVED"), mk(2, "APPROVED"), mk(3, "APPROVED")]
        assert mod._ladder_atomic_gate("g1", 1) == "READY"
        print("Test A1 PASS: all APPROVED → READY")

        # Test 2: one PROPOSED → DEFER
        db.query_ladder_group = lambda conn, gid: [
            mk(1, "APPROVED"), mk(2, "PROPOSED"), mk(3, "APPROVED")]
        assert mod._ladder_atomic_gate("g1", 1) == "DEFER"
        print("Test A2 PASS: any PROPOSED → DEFER")

        # Test 3: one REJECTED → DEAD
        db.query_ladder_group = lambda conn, gid: [
            mk(1, "APPROVED"), mk(2, "REJECTED"), mk(3, "APPROVED")]
        assert mod._ladder_atomic_gate("g1", 1) == "DEAD"
        print("Test A3 PASS: any REJECTED → DEAD")

        # Test 4: one SKIPPED → DEAD
        db.query_ladder_group = lambda conn, gid: [
            mk(1, "APPROVED"), mk(2, "SKIPPED"), mk(3, "APPROVED")]
        assert mod._ladder_atomic_gate("g1", 1) == "DEAD"
        print("Test A4 PASS: any SKIPPED → DEAD")

        # Test 5: one already EXECUTED + others APPROVED → DEAD (partial state guard)
        db.query_ladder_group = lambda conn, gid: [
            mk(1, "EXECUTED"), mk(2, "APPROVED"), mk(3, "APPROVED")]
        assert mod._ladder_atomic_gate("g1", 2) == "DEAD"
        print("Test A5 PASS: partial EXECUTED → DEAD (no double-exec)")

        # Test 6: empty group → DEAD
        db.query_ladder_group = lambda conn, gid: []
        assert mod._ladder_atomic_gate("g1", 1) == "DEAD"
        print("Test A6 PASS: empty group → DEAD")

        # Test 7: mark_dead marks all non-terminal legs SKIPPED
        updates.clear()
        db.query_ladder_group = lambda conn, gid: [
            mk(1, "APPROVED"), mk(2, "SKIPPED"), mk(3, "ADJUSTED")]
        mod._ladder_mark_dead("g1", "ladder_sibling_failed")
        # Should update legs 1 (APPROVED) and 3 (ADJUSTED), NOT 2 (already SKIPPED)
        updated_ids = {u[0] for u in updates}
        assert updated_ids == {1, 3}, f"expected {{1,3}}, got {updated_ids}"
        assert all(u[1] == "SKIPPED" for u in updates)
        assert all(u[2].get("skip_reason") == "ladder_sibling_failed" for u in updates)
        print("Test A7 PASS: mark_dead marks APPROVED+ADJUSTED, skips terminal")

        print("\nAll atomic-execution gate tests PASS (7/7)")
    finally:
        db.query_ladder_group = saved_query
        db.update_entry_status = saved_update
        db.connect = saved_connect


def _test_ladder_orphan_floor():
    """v9.1: orphan single-bin legs must pass --min-entry-price even
    when discovery used --ladder-min-leg-price (lower floor)."""
    import weather_edge_bot as mod
    from types import SimpleNamespace

    spec_stub = SimpleNamespace(threshold_value=70, confidence=1.0)
    base_args = SimpleNamespace(
        max_market_exposure_usd=50.0,
        min_entry_price=0.30,
        min_edge_pp=20.0,
        min_ttr_hours=12.0,
        ladder_min_leg_price=0.10,
        ladder_min_leg_edge_pp=10.0,
        ladder_min_ttr_hours=6.0,
        ladder_mode="3bin",
        ladder_stake_split="kelly",
    )

    # Test 1: orphan cheap leg (no event_slug) blocked by single_floor
    cands = [{"slug": "s1", "event_slug": "",
              "entry_price": 0.15, "spec": spec_stub,
              "forecast_prob": 0.50, "edge_pp": 25.0, "ttr_hours": 24.0}]
    out = mod._build_ladder_candidates(cands, base_args)
    assert len(out) == 0, f"orphan at 0.15 < min 0.30 should drop, got {out}"
    print("Test O1 PASS: orphan single-bin below 0.30 dropped")

    # Test 2: orphan above all floors kept
    cands = [{"slug": "s2", "event_slug": "",
              "entry_price": 0.40, "spec": spec_stub,
              "forecast_prob": 0.50, "edge_pp": 25.0, "ttr_hours": 24.0}]
    out = mod._build_ladder_candidates(cands, base_args)
    assert len(out) == 1
    print("Test O2 PASS: orphan single-bin above all floors kept")

    # Test 3: lone leg in event_slug below price floor → blocked
    cands = [{"slug": "s3", "event_slug": "evX",
              "entry_price": 0.20, "spec": spec_stub,
              "forecast_prob": 0.50, "edge_pp": 25.0, "ttr_hours": 24.0}]
    out = mod._build_ladder_candidates(cands, base_args)
    assert len(out) == 0
    print("Test O3 PASS: lone leg in event below 0.30 dropped (orphan)")

    # Test 4: 2-leg ladder with one cheap+low-edge leg, both kept (Kelly)
    spec_a = SimpleNamespace(threshold_value=70, confidence=1.0)
    spec_b = SimpleNamespace(threshold_value=75, confidence=1.0)
    cands = [{"slug": "s4a", "event_slug": "evY",
              "entry_price": 0.55, "spec": spec_a,
              "forecast_prob": 0.70, "edge_pp": 15.0, "ttr_hours": 8.0},
             {"slug": "s4b", "event_slug": "evY",
              "entry_price": 0.18, "spec": spec_b,
              "forecast_prob": 0.30, "edge_pp": 12.0, "ttr_hours": 8.0}]
    out = mod._build_ladder_candidates(cands, base_args)
    assert len(out) == 2, f"expected 2 legs, got {len(out)}"
    assert all(c.get("ladder_group_id") for c in out)
    assert out[0]["ladder_group_id"] == out[1]["ladder_group_id"]
    print("Test O4 PASS: 2-leg ladder admits cheap+low-edge+8h-TTR legs")

    # Test 5: orphan with low edge blocked
    cands = [{"slug": "s5", "event_slug": "",
              "entry_price": 0.40, "spec": spec_stub,
              "forecast_prob": 0.50, "edge_pp": 15.0, "ttr_hours": 24.0}]
    out = mod._build_ladder_candidates(cands, base_args)
    assert len(out) == 0
    print("Test O5 PASS: orphan with edge below --min-edge-pp dropped")

    # Test 6: lone ladder leg with low edge blocked
    cands = [{"slug": "s6", "event_slug": "evZ",
              "entry_price": 0.40, "spec": spec_stub,
              "forecast_prob": 0.50, "edge_pp": 15.0, "ttr_hours": 24.0}]
    out = mod._build_ladder_candidates(cands, base_args)
    assert len(out) == 0
    print("Test O6 PASS: lone ladder leg with low edge dropped")

    # Test 7 (v9.3): orphan with short TTR (8h < 12h single floor) blocked
    cands = [{"slug": "s7", "event_slug": "",
              "entry_price": 0.40, "spec": spec_stub,
              "forecast_prob": 0.50, "edge_pp": 25.0, "ttr_hours": 8.0}]
    out = mod._build_ladder_candidates(cands, base_args)
    assert len(out) == 0, f"orphan with TTR=8h should drop, got {out}"
    print("Test O7 PASS: orphan with TTR below --min-ttr-hours dropped")

    # Test 8 (v9.3): lone leg in event_slug with TTR=8h dropped (orphan path)
    cands = [{"slug": "s8", "event_slug": "evW",
              "entry_price": 0.40, "spec": spec_stub,
              "forecast_prob": 0.50, "edge_pp": 25.0, "ttr_hours": 8.0}]
    out = mod._build_ladder_candidates(cands, base_args)
    assert len(out) == 0
    print("Test O8 PASS: lone ladder leg with TTR below 12h dropped")

    print("\nAll ladder orphan-floor tests PASS (8/8)")


# ---------------------------------------------------------------------------
# Inline tests for the v13.5 ladder judge-size-cap fix
# Run: python weather_edge_bot.py --test-ladder-cap
# ---------------------------------------------------------------------------

def _test_ladder_judge_cap():
    """Hermetic E2E of _execute_ladder_group_atomic (dry-run) proving the
    judge's ADJUST size cap is honored on ladder legs. Regression for Seoul
    #10 (2026-07-01): judge ADJUST $15, ladder executed the $29.32 Kelly
    split. Monkeypatches orderbook/slippage/exposure; uses a temp DB."""
    import tempfile
    import types
    from pathlib import Path as P
    import weather_edge_bot as mod

    tmp = P(tempfile.mkdtemp()) / "ladder_cap_test.db"
    db.init_db(tmp)
    gid = "test-group-cap"
    with db.connect(tmp) as conn:
        ts = "2026-07-01T04:00:00+00:00"
        # Leg 1: ADJUSTED com cap do judge $15, Kelly split $29.32 (o caso Seoul)
        conn.execute(
            "INSERT INTO entries (ts, market_slug, market_question, side, "
            "status, ladder_group_id, ladder_position, entry_price, "
            "ladder_stake_usd, forecast_prob_at_entry, token_id_yes, token_id_no) "
            "VALUES (?, 's30c', 'q', 'NO', 'ADJUSTED', ?, 'central', 0.48, "
            "29.3243, 0.8, 'tY1', 'tN1')", (ts, gid))
        e1 = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO judge_reviews (entry_id, ts, verdict, confidence, "
            "judge_prob, bot_prob, rationale, adjusted_size_usd) "
            "VALUES (?, ?, 'ADJUST', 0.6, 0.65, 0.8, '', 15.0)", (e1, ts))
        # Leg 2: APPROVED sem cap — mantém o Kelly split
        conn.execute(
            "INSERT INTO entries (ts, market_slug, market_question, side, "
            "status, ladder_group_id, ladder_position, entry_price, "
            "ladder_stake_usd, forecast_prob_at_entry, token_id_yes, token_id_no) "
            "VALUES (?, 's31c', 'q', 'NO', 'APPROVED', ?, 'below', 0.65, "
            "20.6757, 0.9, 'tY2', 'tN2')", (ts, gid))
        e2 = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()

    saved = (mod.fetch_orderbook, mod.compute_max_size_for_slippage,
             db.connect, db.current_market_exposure_usd)
    try:
        mod.fetch_orderbook = lambda tid: {"asks": [{"price": 0.48, "size": 500}]}
        mod.compute_max_size_for_slippage = lambda book, side, max_slippage: {
            "max_usd": 100.0, "max_shares": 200.0, "avg_fill": 0.48}
        _orig_connect = saved[2]
        db.connect = lambda path=None: _orig_connect(tmp)
        db.current_market_exposure_usd = lambda conn, slug: 0.0

        args = types.SimpleNamespace(
            max_slippage=0.2, ladder_execute_min_leg_edge_pp=5.0,
            execute_min_edge_pp=8.0, min_edge_pp=20.0,
            max_market_exposure_usd=50.0, dry_run=True)
        n = mod._execute_ladder_group_atomic(gid, args, engine=None,
                                             default_fee_rate=0.0)
        assert n == 2, f"esperava 2 pernas executadas, obtive {n}"

        with db.connect() as conn:
            s1 = conn.execute("SELECT size_usd FROM entries WHERE entry_id=?",
                              (e1,)).fetchone()[0]
            s2 = conn.execute("SELECT size_usd FROM entries WHERE entry_id=?",
                              (e2,)).fetchone()[0]
        assert abs(s1 - 15.0) < 1e-6, \
            f"perna ADJUSTED deveria executar $15 (cap do judge), executou ${s1}"
        print(f"Test L1 PASS: perna ADJUSTED capada em ${s1} (Kelly era $29.32)")
        assert abs(s2 - 20.6757) < 1e-4, \
            f"perna APPROVED deveria manter Kelly $20.68, executou ${s2}"
        print(f"Test L2 PASS: perna APPROVED mantém Kelly ${s2:.2f} (sem cap)")

        # L3: cap do judge abaixo do mínimo $10 → grupo inteiro aborta
        with db.connect() as conn:
            conn.execute("UPDATE judge_reviews SET adjusted_size_usd = 8.0 "
                         "WHERE entry_id = ?", (e1,))
            conn.execute("UPDATE entries SET status='ADJUSTED', size_usd=NULL "
                         "WHERE entry_id = ?", (e1,))
            conn.execute("UPDATE entries SET status='APPROVED', size_usd=NULL "
                         "WHERE entry_id = ?", (e2,))
            conn.commit()
        n = mod._execute_ladder_group_atomic(gid, args, engine=None,
                                             default_fee_rate=0.0)
        assert n == 0, f"cap $8 < min $10 deveria abortar o grupo, obtive {n}"
        print("Test L3 PASS: cap do judge < $10 → grupo aborta (size_below_min)")

        print("\nAll ladder judge-cap tests PASS (3/3)")
    finally:
        (mod.fetch_orderbook, mod.compute_max_size_for_slippage,
         db.connect, db.current_market_exposure_usd) = saved


# ---------------------------------------------------------------------------
# Inline tests for _decide_final_outcome (v13.3 settlement-outcome fix)
# Run: python weather_edge_bot.py --test-outcome
# ---------------------------------------------------------------------------

def _test_decide_outcome():
    """Hermetic tests for the resolution outcome decision. The v9.9 bug made
    every gamma_closed market resolve YES — these lock in the correct
    behavior (Paris July-3 14°C case: closed, prices [0, 1] → NO)."""
    # 1. THE bug case: closed market where NO won → must be NO, not YES
    assert _decide_final_outcome([0.0, 1.0], gamma_closed=True) == "NO"
    print("Test R1 PASS: closed + prices [0,1] → NO (era YES no bug v9.9)")

    # 2. closed market where YES won
    assert _decide_final_outcome([1.0, 0.0], gamma_closed=True) == "YES"
    print("Test R2 PASS: closed + prices [1,0] → YES")

    # 3. settled by price before official close (v9.9 motivation preserved)
    assert _decide_final_outcome([0.03, 0.97], gamma_closed=False) == "NO"
    assert _decide_final_outcome([0.97, 0.03], gamma_closed=False) == "YES"
    print("Test R3 PASS: 0.97/0.03 antes do closed → decide por preço")

    # 4. not closed, prices inconclusive → None (retry next sweep)
    assert _decide_final_outcome([0.60, 0.40], gamma_closed=False) is None
    print("Test R4 PASS: aberto + preços inconclusivos → None (retry)")

    # 5. closed but no winner price → VOID
    assert _decide_final_outcome([0.50, 0.50], gamma_closed=True) == "VOID"
    print("Test R5 PASS: closed + 50/50 → VOID")

    # 6. boundary: exactly at threshold counts as settled
    assert _decide_final_outcome([0.95, 0.05], gamma_closed=False) == "YES"
    print("Test R6 PASS: preço exatamente 0.95 → settled (>=)")

    print("\nAll resolution-outcome tests PASS (6/6)")


def _test_cheap_convexity_discovery():
    """Hermetic E2E of run_discovery_cheap_convexity (no network). Proves:
      - a 1-20c tail bin is proposed with strategy='cheap_convexity',
        ladder_group_id NULL, and fair/ask/bid stashed in discovery_meta;
      - the RAW fair (0.07) is used, NOT the clipped forecast_probability
        (0.30) — the whole point of the strategy;
      - the gate blocks everything when tail_calibration_pass is false;
      - a market with no exit-side bids is skipped (no_exit_liquidity)."""
    import tempfile
    import types
    from pathlib import Path as P
    import weather_edge_bot as mod

    tmp = P(tempfile.mkdtemp()) / "cc_discovery.db"
    db.init_db(tmp)

    future = (datetime.now(timezone.utc) + timedelta(hours=24)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    market = {
        "slug": "highest-temp-in-paris-14c-on-july-9",
        "question": "Will the highest temperature in Paris be 14°C on July 9?",
        "endDate": future, "acceptingOrders": True,
        "clobTokenIds": json.dumps(["tY", "tN"]),
        "conditionId": "0xcc",
    }
    spec = types.SimpleNamespace(
        city="Paris", confidence=0.9, metric="temp", comparison="range",
        threshold_value=14.0, threshold_value_high=15.0, threshold_unit="C")

    saved = (mod.fetch_weather_markets, mod.parse_market, mod.fetch_orderbook,
             mod.implied_probabilities, mod.resolve_station,
             mod.auto_extract_station, mod.fetch_forecast,
             mod._compute_mae_for_market, mod._forecast_probability_raw,
             mod.forecast_probability, mod.compute_edge,
             mod.compute_max_size_for_slippage, mod._load_cc_gate, db.connect)
    _orig_connect = db.connect
    try:
        mod.fetch_weather_markets = lambda min_volume=0: [market]
        mod.parse_market = lambda q, e, c: spec
        mod.resolve_station = lambda city, cities: None
        mod.auto_extract_station = lambda *a, **k: None
        mod.fetch_forecast = lambda *a, **k: {"daily_forecast": [{}]}
        mod._compute_mae_for_market = lambda s, f, a, station=None: (
            2.0, None, None, {"station": "LFPG", "mae_dynamic": 2.0})
        # RAW fair 0.07 (tradeable tail). Clipped path would say 0.30.
        mod._forecast_probability_raw = lambda s, f, **k: 0.07
        mod.forecast_probability = lambda s, f, **k: 0.30  # must NOT be used
        mod.implied_probabilities = lambda by, bn: {"yes_ask": 0.02,
                                                    "no_ask": 0.97}
        mod.compute_edge = lambda fair, imp: {
            "best_side": "YES",
            "edge_pp_at_best": round((fair - imp["yes_ask"]) * 100, 2)}
        book_ok = {"asks": [{"price": 0.02, "size": 1000}],
                   "bids": [{"price": 0.015, "size": 1000}]}
        mod.fetch_orderbook = lambda tid: book_ok
        mod.compute_max_size_for_slippage = lambda book, side, max_slippage: {
            "max_shares": 500.0, "max_usd": 10.0, "slippage_pct": 0.05}
        mod._load_cc_gate = lambda: {"tail_calibration_pass": True}
        db.connect = lambda path=None: _orig_connect(tmp)

        args = types.SimpleNamespace(
            cheap_convexity=True, cc_min_entry_price=0.01,
            cc_max_entry_price=0.20, cc_min_edge_pp=3.0, cc_exit_margin_pp=1.0,
            min_volume=100, window_hours=48, min_ttr_hours=0.0,
            max_slippage=0.2, debug=False)

        n = mod.run_discovery_cheap_convexity(args, cities={})
        assert n == 1, f"expected 1 proposal, got {n}"
        with db.connect() as conn:
            row = conn.execute(
                "SELECT strategy, side, entry_price, forecast_prob_at_entry, "
                "ladder_group_id, discovery_meta_json, status "
                "FROM entries").fetchone()
        assert row["strategy"] == "cheap_convexity", row["strategy"]
        assert row["side"] == "YES" and row["status"] == "PROPOSED"
        assert row["ladder_group_id"] is None, row["ladder_group_id"]
        assert abs(row["entry_price"] - 0.02) < 1e-9, row["entry_price"]
        # RAW fair used, NOT the clip: P(side=YES) ≈ 0.07, not 0.30.
        assert abs(row["forecast_prob_at_entry"] - 0.07) < 1e-9, (
            f"expected raw fair 0.07, got {row['forecast_prob_at_entry']} "
            "(0.30 would mean the clip leaked in)")
        meta = json.loads(row["discovery_meta_json"])
        assert abs(meta["fair_target"] - 0.07) < 1e-9, meta
        assert "ask_at_entry" in meta and "bid_at_entry" in meta, meta
        print("Test CC-D1 PASS: cheap_convexity proposed with raw fair 0.07 "
              "(clip 0.30 not used), single-bin, meta stashed")

        # CC-D2: gate not passed → no proposals.
        mod._load_cc_gate = lambda: {"tail_calibration_pass": False}
        n2 = mod.run_discovery_cheap_convexity(args, cities={})
        assert n2 == 0, n2
        print("Test CC-D2 PASS: gate not passed → 0 proposals (no-op)")

        # CC-D3: exit book has no bids → no_exit_liquidity, nothing proposed.
        mod._load_cc_gate = lambda: {"tail_calibration_pass": True}
        mod.compute_max_size_for_slippage = (
            lambda book, side, max_slippage:
            {"max_shares": (500.0 if side == "BUY" else 0.0),
             "max_usd": 10.0, "slippage_pct": 0.05})
        # fresh DB so the duplicate guard doesn't fire on the CC-D1 row
        tmp2 = P(tempfile.mkdtemp()) / "cc_discovery2.db"
        db.init_db(tmp2)
        db.connect = lambda path=None: _orig_connect(tmp2)
        n3 = mod.run_discovery_cheap_convexity(args, cities={})
        assert n3 == 0, n3
        with db.connect() as conn:
            skips = conn.execute("SELECT reason FROM discovery_skips").fetchall()
        assert any(s["reason"] == "cc_no_exit_liquidity" for s in skips), skips
        print("Test CC-D3 PASS: no exit bids → cc_no_exit_liquidity, 0 proposed")

    finally:
        (mod.fetch_weather_markets, mod.parse_market, mod.fetch_orderbook,
         mod.implied_probabilities, mod.resolve_station,
         mod.auto_extract_station, mod.fetch_forecast,
         mod._compute_mae_for_market, mod._forecast_probability_raw,
         mod.forecast_probability, mod.compute_edge,
         mod.compute_max_size_for_slippage, mod._load_cc_gate,
         db.connect) = saved
    print("\nAll cheap_convexity discovery tests PASS")


if __name__ == "__main__":
    import sys
    if "--test-atomic" in sys.argv:
        _test_atomic_execution()
    elif "--test-orphan" in sys.argv:
        _test_ladder_orphan_floor()
    elif "--test-outcome" in sys.argv:
        _test_decide_outcome()
    elif "--test-ladder-cap" in sys.argv:
        _test_ladder_judge_cap()
    elif "--test-cc-discovery" in sys.argv:
        _test_cheap_convexity_discovery()
    else:
        main()
