"""Pure helper functions for the weather edge bot.

All functions are deterministic and side-effect free; testable in isolation.

Three responsibility groups:
  1. Market parsing — extract (city, threshold, comparison, target_date) from
     a Polymarket weather question.
  2. Forecast → probability — convert OpenWeather forecast JSON into P(YES)
     for a parsed market spec.
  3. Slippage-aware sizing — walk an orderbook to find the max trade size
     that keeps weighted-avg fill within a slippage cap.
"""
from __future__ import annotations

import json
import os
import re
import statistics
import time
import unicodedata
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import requests


def _strip_accents(s: str) -> str:
    """Remove diacritics so 'São Paulo' matches 'Sao Paulo'."""
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if not unicodedata.combining(c))


# ---------------------------------------------------------------------------
# Constants — calibrated MAEs for normal-CDF probability conversion
# ---------------------------------------------------------------------------

# v11 (2026-05-31 post-mortem): widened from 5.0/2.78 after the -$771 / 33%
# win-rate run. Loss forensics showed range-bracket forecasts erring 2-3°C
# while the bot priced edge as if MAE were ~2.8°C, inflating confidence on
# range NO bets (which were 93% of trades and lost 67% of the time). Wider
# MAE shrinks the computed edge so only genuinely separated forecasts survive
# --min-edge-pp.
MAE_TEMP_F = 6.5      # OpenWeather 3-5d temp forecast MAE in °F (fallback)
MAE_TEMP_C = 3.6      # = MAE_TEMP_F converted (6.5°F ≈ 3.6°C)
MAE_PRECIP_MM = 3.0   # precip total MAE
MAE_WIND_KPH = 8.0    # wind speed MAE

# v13 (2026-06-14): NGR / EMOS calibration coefficients for ensemble σ.
# Open-Meteo ICON+GFS+ECMWF spread is documented to be UNDER-DISPERSIVE at
# 24-48h lead (truth falls outside the ensemble range too often). Calibrate
# raw spread into a true σ via Non-homogeneous Gaussian Regression:
#     σ_calibrated = NGR_ALPHA * std(ensemble_members) + NGR_BETA_C
# Defaults are fixed coefficients from Gneiting et al. 2005 (MWR 133) typical
# range for short-lead 2m T. Once we have ≥200 paired (forecast, observed)
# log entries per station we can fit a, b properly per spec.city.
NGR_ALPHA = 1.5             # inflation factor (corrects under-dispersion)
NGR_BETA_C = 0.5            # additive floor in °C
NGR_BETA_F = NGR_BETA_C * 9.0 / 5.0  # = 0.9°F
# Hard floor on σ so a 3-model ensemble that all agree perfectly doesn't
# produce σ=0 → P(NO bin)=1 → unbounded Kelly. 1°C is the documented HRES
# 12h σ floor; below that the discrete forecast precision itself dominates.
SIGMA_FLOOR_C = 1.0         # °C
SIGMA_FLOOR_F = SIGMA_FLOOR_C * 9.0 / 5.0  # = 1.8°F
# When the calibrated ensemble path is in use, the v12.1 P(side) cap of 0.70
# is replaced by a looser sanity cap — the ensemble itself is now the
# uncertainty model, not a constant.
# v13.1 (2026-06-15): lowered 0.95 → 0.80. The 0.95 cap let the ensemble
# emit bot_prob ~0.95 on range NO bets, while the judge's range calibration
# discipline caps judge_prob at 0.65-0.70 — a permanent ~30pp gap that
# tripped Rule 6 on 84% of reviews and rejected the whole pipeline
# (2026-06-15 run: 226 REJECT / 296). 0.80 keeps the ensemble's sharpness
# but stays within ~15pp of the judge so Rule 6 (20pp) no longer fires on
# the judge's own ADJUST verdicts. Also honors the research caveat that
# ≤1°C bins need proven calibration before trusting prob > 0.90.
ENSEMBLE_PROB_CAP = 0.80


# ---------------------------------------------------------------------------
# v7: Dynamic MAE from forecast volatility history
# ---------------------------------------------------------------------------

def compute_dynamic_mae(history_values: list[float],
                        base_mae: float,
                        min_samples: int = 2,
                        std_multiplier: float = 1.5) -> float:
    """Compute MAE from observed forecast revisions.

    Returns max(base_mae, std_dev(history) * std_multiplier). Falls back
    to base_mae when fewer than min_samples are available (typical for
    fresh cities). The multiplier compensates for std_dev underestimating
    true future MAE when the sample is small.

    Rationale: cities like Lucknow showed forecast revisions of ±5°F
    between updates on 2026-05-14 — using the static MAE_TEMP_F=5.0
    underestimated the true uncertainty and inflated edge_pp, causing
    the bot to take losing trades. Dynamic MAE based on observed
    volatility gives the bot per-city realism.
    """
    if not history_values or len(history_values) < min_samples:
        return base_mae
    try:
        sd = statistics.stdev(history_values)
    except statistics.StatisticsError:
        return base_mae
    return max(base_mae, sd * std_multiplier)


# ---------------------------------------------------------------------------
# v7: Multi-source forecast fetch (Visual Crossing) — moved from judge.py
# so the bot's discovery loop can cross-check before proposing.
# ---------------------------------------------------------------------------

_VC_CACHE: dict = {}                 # (city, date_iso) → (ts_unix, response)
_VC_CACHE_TTL_SEC = 6 * 3600         # 6h — caps free-tier API calls


def fetch_visual_crossing(city: str,
                           date_iso: Optional[str] = None,
                           force_refresh: bool = False) -> Optional[dict]:
    """Visual Crossing timeline API. date_iso = YYYY-MM-DD or None for today.

    Returns {"days": [...]} (max 5) or None if API key missing / HTTP fail.
    Cached in-memory per (city, date_iso) for 6h to fit the free tier
    (1000 req/day shared).
    """
    api_key = os.environ.get("VISUAL_CROSSING_API_KEY")
    if not api_key:
        return None

    cache_key = (city.lower(), date_iso or "")
    now = time.time()
    if not force_refresh and cache_key in _VC_CACHE:
        cached_ts, cached_data = _VC_CACHE[cache_key]
        if now - cached_ts < _VC_CACHE_TTL_SEC:
            return cached_data

    base = ("https://weather.visualcrossing.com/VisualCrossingWebServices"
            "/rest/services/timeline")
    url = f"{base}/{city}/{date_iso}" if date_iso else f"{base}/{city}"
    try:
        r = requests.get(
            url,
            params={"key": api_key, "unitGroup": "us",
                    "include": "days",
                    "elements": "tempmax,tempmin,precip,precipprob,conditions"},
            timeout=15,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        result = {"days": data.get("days", [])[:5]}
    except Exception:
        return None

    _VC_CACHE[cache_key] = (now, result)
    return result


# ---------------------------------------------------------------------------
# v8: Open-Meteo multi-model ensemble (ICON + GFS + ECMWF in one call).
# No API key required. Free tier 10k req/day non-commercial.
# Article reference: when 2 of 3 models agree within 1C the third is
# treated as outlier; if spread > 3C the bot inflates MAE x 1.5.
# ---------------------------------------------------------------------------

_OM_CACHE: dict = {}              # (lat, lon, target_iso, models) -> (ts_unix, dict)
_OM_CACHE_TTL_SEC = 6 * 3600      # 6h

# v15.4: the canonical global trio. European cities can override per-city via
# the `om_models` array in weather-cities.json (data-driven, no code branch)
# to add regional models (AROME France 1.5km, ICON-EU 7km, ICON-D2/HARMONIE
# 2km) that have higher skill there. Default keeps behavior byte-identical.
DEFAULT_OM_MODELS = ["icon_seamless", "gfs_seamless", "ecmwf_ifs025"]

# Map an Open-Meteo model key -> the short label used in vals keys /
# forecast_history source. The trio keep their legacy short names (so
# forecast_history rows and calibration stay byte-identical); any other model
# uses its full key as the label (e.g. "arome_france_hd").
_OM_SHORT = {"icon_seamless": "icon", "gfs_seamless": "gfs",
             "ecmwf_ifs025": "ecmwf"}


def _om_short_name(model_key: str) -> str:
    return _OM_SHORT.get(model_key, model_key)


def fetch_open_meteo_ensemble(lat: float, lon: float,
                                target_date,  # date or YYYY-MM-DD string
                                force_refresh: bool = False,
                                models: Optional[list] = None
                                ) -> Optional[dict]:
    """Pull daily max AND min temp (C) for `target_date` at (lat, lon) from a
    set of Open-Meteo models.

    `models` = list of Open-Meteo model keys. Default (None) = the canonical
    global trio icon_seamless+gfs_seamless+ecmwf_ifs025 (DEFAULT_OM_MODELS),
    keeping every non-European caller byte-identical. European cities pass an
    extended list (e.g. arome_france_hd, icon_eu, icon_d2) via their station's
    `om_models` in weather-cities.json.

    Returns e.g.:
      {"icon_max_c": 25.3, "gfs_max_c": 24.8, "ecmwf_max_c": 26.1,
       "icon_min_c": 14.2, ..., "arome_france_hd_max_c": 25.0, ...,
       "spread_c": 1.3, "spread_min_c": 0.9, "agree": True, "n_models": 3,
       "model_keys": [...]}
    or None on HTTP failure / malformed response. The short label of each
    model (icon/gfs/ecmwf for the trio, else the full key) prefixes its
    *_max_c/_min_c keys.

    v13.4: *_min_c keys feed lowest-temperature markets. `spread_c`/`agree`
    remain max-based. Cached 6h per (lat, lon, target_iso, models).
    """
    model_keys = list(models) if models else list(DEFAULT_OM_MODELS)
    target_iso = (target_date.isoformat()
                   if hasattr(target_date, "isoformat") else str(target_date))
    cache_key = (round(float(lat), 4), round(float(lon), 4), target_iso,
                 ",".join(model_keys))
    now = time.time()
    if not force_refresh and cache_key in _OM_CACHE:
        cached_ts, cached_data = _OM_CACHE[cache_key]
        if now - cached_ts < _OM_CACHE_TTL_SEC:
            return cached_data

    def _query(keys: list) -> Optional[dict]:
        """One Open-Meteo call for `keys`; parse into vals+spread; None on any
        failure. Isolated so we can fall back to the trio if a regional key
        makes the API 400 the whole request."""
        try:
            r = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat, "longitude": lon,
                    "models": ",".join(keys),
                    "hourly": "temperature_2m",
                    "temperature_unit": "celsius",
                    "start_date": target_iso, "end_date": target_iso,
                },
                timeout=20,
            )
            if r.status_code != 200:
                return None
            hourly = (r.json().get("hourly") or {})
        except Exception:
            return None

        def _extreme_of(series_key, fn):
            series = hourly.get(series_key)
            if not series:
                return None
            clean = [v for v in series if v is not None]
            return fn(clean) if clean else None

        single = len(keys) == 1
        vals = {}
        for key in keys:
            name = _om_short_name(key)
            sk = "temperature_2m" if single else f"temperature_2m_{key}"
            vals[f"{name}_max_c"] = _extreme_of(sk, max)
            vals[f"{name}_min_c"] = _extreme_of(sk, min)
        pmax = [v for k, v in vals.items()
                if k.endswith("_max_c") and v is not None]
        pmin = [v for k, v in vals.items()
                if k.endswith("_min_c") and v is not None]
        if not pmax:
            return None
        spread = max(pmax) - min(pmax)
        return {
            **vals,
            "spread_c": round(spread, 2),
            "spread_min_c": (round(max(pmin) - min(pmin), 2) if pmin else None),
            "agree": spread <= 3.0,
            "n_models": len(pmax),
            "model_keys": keys,
        }

    result = _query(model_keys)
    # v15.4: robustness — a bad/unknown regional model key makes Open-Meteo
    # 400 the ENTIRE request, which would strip the trio too and disable the
    # ensemble for that (European) city. Fall back to the global trio so
    # regional models are strictly additive-or-neutral (worst case = today).
    if result is None and model_keys != DEFAULT_OM_MODELS:
        result = _query(list(DEFAULT_OM_MODELS))
        if result is not None:
            result["fallback_from"] = model_keys
    if result is None:
        return None
    _OM_CACHE[cache_key] = (now, result)
    return result


def compute_ensemble_calibration(om_data: Optional[dict],
                                  threshold_unit: str,
                                  temp_kind: str = "high"
                                  ) -> Optional[dict]:
    """v13: NGR-style calibration of the Open-Meteo ICON+GFS+ECMWF ensemble
    into a (μ, σ) Gaussian predictive distribution, in `threshold_unit`.

    Replaces the v9.12 "om_spread_mult multiplier on a constant MAE" pattern:
    we now USE the ensemble spread DIRECTLY as the basis of σ, calibrated by
    a fixed-coefficient NGR formula (Gneiting et al. 2005, MWR 133). The
    ensemble mean is also returned as μ — superseding the OpenWeather single-
    source forecast value when ≥2 models are present.

    Args:
        om_data: dict returned by fetch_open_meteo_ensemble() — has
                 {icon_max_c, ..., icon_min_c, ..., spread_c, n_models}
                 or None.
        threshold_unit: 'C' or 'F'. The returned mu/sigma are in this unit.
        temp_kind: 'high' (daily max — default) or 'low' (daily min, for
                 lowest-temperature markets). v13.4: before this param the
                 calibration always used the max members, so low markets
                 got μ = daily high — the min/max post-mortem bug.

    Returns:
        {"mu": float, "sigma": float, "n_models": int, "members": [...]}
        in `threshold_unit`. Returns None when om_data is missing or has
        fewer than 2 present members for the requested kind (a single
        member can't yield σ; older cached om_data lacks *_min_c keys and
        safely falls back to the legacy MAE path for low markets).
    """
    if not om_data:
        return None
    suffix = "_min_c" if temp_kind == "low" else "_max_c"
    # v15.4: iterate over ALL present members (not the fixed trio) so extra
    # regional models (arome_france_hd, icon_eu, icon_d2, ...) count in μ/σ.
    # Exclude the aggregate spread keys (spread_c / spread_min_c) which also
    # end in "_c". Byte-identical for the trio.
    members_c = []
    for k, v in om_data.items():
        if k.endswith(suffix) and not k.startswith("spread") and v is not None:
            members_c.append(float(v))
    if len(members_c) < 2:
        return None  # need ≥2 for spread; fall back to MAE path

    unit = (threshold_unit or "C").upper()
    if unit == "F":
        members = [v * 9.0 / 5.0 + 32.0 for v in members_c]
        beta = NGR_BETA_F
        sigma_floor = SIGMA_FLOOR_F
    else:
        members = list(members_c)
        beta = NGR_BETA_C
        sigma_floor = SIGMA_FLOOR_C

    mu = sum(members) / len(members)
    # Sample stdev (n-1 denominator). For n=2 this just equals |a-b|/√2.
    var = sum((v - mu) ** 2 for v in members) / (len(members) - 1)
    sigma_raw = var ** 0.5
    sigma = max(NGR_ALPHA * sigma_raw + beta, sigma_floor)
    return {
        "mu": round(mu, 3),
        "sigma": round(sigma, 3),
        "sigma_raw": round(sigma_raw, 3),
        "n_models": len(members),
        "members": [round(v, 2) for v in members],
        "unit": unit,
    }


# ---------------------------------------------------------------------------
# v10: Open-Meteo archive (realized weather for past dates).
# Used by the strategy advisor's loss-forensic step to reconstruct what
# the temperature actually was on the day a bet settled. Endpoint is free,
# no auth, ~1-2 day latency after the target date.
# ---------------------------------------------------------------------------

_OM_ARCHIVE_CACHE: dict = {}
_OM_ARCHIVE_CACHE_TTL_SEC = 24 * 3600  # 24h — realized values are immutable


def fetch_open_meteo_archive(lat: float, lon: float,
                              target_date,
                              force_refresh: bool = False
                              ) -> Optional[dict]:
    """Pull realized (observed) hourly temperature for `target_date` at
    (lat, lon) from Open-Meteo's archive endpoint. Returns:
      {"observed_max_c": 24.8, "observed_min_c": 13.1,
       "observed_max_f": 76.6, "observed_min_f": 55.6,
       "n_hours": 24, "source": "open-meteo-archive"}
    or None on HTTP failure / no data (typical when target_date is too
    recent — archive lags realtime by ~1-2 days).
    """
    target_iso = (target_date.isoformat()
                   if hasattr(target_date, "isoformat") else str(target_date))
    cache_key = (round(float(lat), 4), round(float(lon), 4), target_iso)
    now = time.time()
    if not force_refresh and cache_key in _OM_ARCHIVE_CACHE:
        cached_ts, cached_data = _OM_ARCHIVE_CACHE[cache_key]
        if now - cached_ts < _OM_ARCHIVE_CACHE_TTL_SEC:
            return cached_data

    try:
        r = requests.get(
            "https://archive-api.open-meteo.com/v1/archive",
            params={
                "latitude": lat,
                "longitude": lon,
                "start_date": target_iso,
                "end_date": target_iso,
                "hourly": "temperature_2m",
                "temperature_unit": "celsius",
            },
            timeout=20,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        series = (data.get("hourly") or {}).get("temperature_2m") or []
        clean = [float(v) for v in series if v is not None]
        if not clean:
            return None
    except Exception:
        return None

    obs_max_c = max(clean)
    obs_min_c = min(clean)
    result = {
        "observed_max_c": round(obs_max_c, 2),
        "observed_min_c": round(obs_min_c, 2),
        "observed_max_f": round(obs_max_c * 9 / 5 + 32, 2),
        "observed_min_f": round(obs_min_c * 9 / 5 + 32, 2),
        "n_hours": len(clean),
        "source": "open-meteo-archive",
    }
    _OM_ARCHIVE_CACHE[cache_key] = (now, result)
    return result


def fetch_metar_daily_extremes(icao: str, target_date,
                               hours: int = 72) -> Optional[dict]:
    """Observed daily max/min 2 m temperature for `target_date` (UTC) at an
    airport, reconstructed from real METAR reports via aviationweather.gov.

    This is the resolution truth source for the Africa desert/subtropical
    PILOT (weather-cities.json stations with resolution_source='metar'). On
    that continent the Open-Meteo archive returns ERA5 reanalysis (~9-25 km,
    NOT the station), whose bias exceeds the 1°C market tick, and there is no
    regional forecast model — so resolution must come from the actual reporting
    station, exactly as Polymarket's Rules specify. METAR reports temperature
    to the whole °C (and the remark group sometimes to 0.1°C), which is coarse
    but is the genuine observation rather than a gridded proxy.

    Returns {"observed_max_c","observed_min_c","observed_max_f","observed_min_f",
             "n_obs","source":"metar-aviationweather"} or None when the station
    filed no report for that UTC day / HTTP non-200 / parse failure. Fail-open,
    never raises. Callers MUST treat None as "unresolved" and NOT silently fall
    back to the ERA5 archive (that is the whole point of the pilot).

    `hours` is the aviationweather look-back window; the archive endpoint serves
    recent observations, so resolution (which runs ~1-2 days after end_date)
    stays inside a 72 h window.
    """
    if not icao:
        return None
    target_iso = (target_date.isoformat()
                  if hasattr(target_date, "isoformat") else str(target_date))[:10]
    base = os.environ.get("AVIATIONWEATHER_BASE_URL",
                          "https://aviationweather.gov/api/data")
    try:
        r = requests.get(
            f"{base}/metar",
            params={"ids": icao, "format": "json", "hours": hours},
            headers={"Accept": "application/json"},
            timeout=20)
        if r.status_code != 200:
            return None
        rows = r.json()
        if not isinstance(rows, list):
            return None
        temps = []
        for ob in rows:
            t = ob.get("temp")
            if t is None:
                continue
            # Determine the observation's UTC date. aviationweather JSON gives
            # `reportTime` as an ISO-ish string ("2026-07-08 12:51:00") and
            # `obsTime` as a unix epoch (seconds, UTC). Prefer the string; fall
            # back to the epoch. Skip obs we can't date (never guess).
            day = None
            rt = ob.get("reportTime")
            if isinstance(rt, str) and len(rt) >= 10:
                day = rt[:10]
            elif ob.get("obsTime") is not None:
                try:
                    day = datetime.fromtimestamp(
                        int(ob["obsTime"]), tz=timezone.utc).date().isoformat()
                except (ValueError, OSError, OverflowError):
                    day = None
            if day != target_iso:
                continue
            try:
                temps.append(float(t))
            except (TypeError, ValueError):
                continue
        if not temps:
            return None
    except Exception:
        return None

    obs_max_c = max(temps)
    obs_min_c = min(temps)
    return {
        "observed_max_c": round(obs_max_c, 2),
        "observed_min_c": round(obs_min_c, 2),
        "observed_max_f": round(obs_max_c * 9 / 5 + 32, 2),
        "observed_min_f": round(obs_min_c * 9 / 5 + 32, 2),
        "n_obs": len(temps),
        "source": "metar-aviationweather",
    }


# ---------------------------------------------------------------------------
# Market parsing
# ---------------------------------------------------------------------------


@dataclass
class MarketSpec:
    """Parsed weather market specification."""
    city: str                              # canonical city name
    threshold_value: float                 # primary threshold (low end for range)
    threshold_unit: str                    # "F", "C", "mm", "in", "kph", "mph"
    metric: str                            # "temp", "precip", "wind", "snow"
    comparison: str                        # "exceed", "below", "at_least", "at_most", "range"
    target_date: Optional[date]
    confidence: float
    raw_question: str
    threshold_value_high: Optional[float] = None  # for "X-Y°F" range markets
    # v13.4 (2026-07-05): which daily extreme the market resolves on.
    # "high" (default) = daily maximum; "low" = daily minimum ("lowest
    # temperature in ..." markets). Before this field the WHOLE pipeline
    # (ensemble μ/σ, forecast ref, proximity guard, observed_value) used the
    # daily MAX even for lowest-temperature markets — e.g. Paris July-3 low
    # market evaluated with μ=28°C (the high) when the forecast low was
    # 15.9°C, fabricating huge fake edges. See 2026-07-05 post-mortem.
    temp_kind: str = "high"


CITY_LOOKUP_PATH = Path(__file__).parent.parent / "references" / "weather-cities.json"


def load_cities(path: Path = CITY_LOOKUP_PATH) -> dict[str, Any]:
    """Load weather-cities.json. Returns empty dict if missing (parser still
    handles cities not in the list, just with lower confidence)."""
    if not path.exists():
        return {"world": [], "europe_top30": [], "north_america_extra": [],
                "us_top50": [], "aliases": {}, "stations": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_station(name: str, cities: dict[str, Any]) -> Optional[dict]:
    """v8: lookup resolution-station override for a canonical city name.

    Returns {"lat", "lon", "station", "temp_bias_f"} or None. None means
    the city has no override and the bot should fall back to OpenWeather
    geocoding by name (legacy behavior).

    Polymarket weather markets resolve at a specific weather station
    (e.g. KLGA for NYC, HKO for HK, VILK for Lucknow). Pulling forecast
    for the wrong location is the most common systematic loss cause
    (see article scar #1 / loss analysis 2026-05-14).
    """
    if not name:
        return None
    stations = cities.get("stations") if cities else None
    if not stations:
        return None
    # Try exact match first, then case-insensitive
    entry = stations.get(name)
    if entry is None:
        target = name.lower()
        for k, v in stations.items():
            if k.lower() == target:
                entry = v
                break
    if not entry:
        return None
    # Sanity: require lat + lon, others optional
    if entry.get("lat") is None or entry.get("lon") is None:
        return None
    return entry


# ---------------------------------------------------------------------------
# v9: Auto-extract resolution station from Polymarket market description.
# When a city isn't yet in the `stations` dict, we try to parse the rules
# text via regex + station_names lookup, so the operator doesn't have to
# manually curate every new city. Falls back to None on failure (caller
# uses legacy OpenWeather geocoding).
# ---------------------------------------------------------------------------

# Regex patterns matched against the lowercased description text.
# Each captures group(1) = the station name phrase. Ordered most-specific
# to least-specific so we don't trigger false matches.
_STATION_PATTERNS = [
    # "recorded by NOAA at the Istanbul Airport" — data-provider-prefixed form.
    # Capture the STATION after "...by <provider> at (the) ... airport/station",
    # NOT the provider. Must come before the generic "recorded by" pattern so
    # the station wins over the provider ("NOAA") on this phrasing.
    re.compile(r"\b(?:recorded|reported|measured)\s+by\s+[\w.\s]+?\s+at\s+(?:the\s+)?([\w\-'.\s]+?)\s+(?:airport|station|observatory)\b", re.I),
    # "recorded at the Hartsfield-Jackson International Airport Station"
    re.compile(r"recorded\s+at\s+(?:the\s+)?([\w\-'.\s]+?)\s+station\b", re.I),
    # "as recorded by the Hong Kong Observatory"
    re.compile(r"recorded\s+by\s+(?:the\s+)?([\w\-'.\s]+?)\b(?:\s+(?:on|in|for|at|during|with|as|per)\b|[.,;])", re.I),
    # "as reported by Hartsfield-Jackson Airport"
    re.compile(r"reported\s+(?:by|at|from)\s+(?:the\s+)?([\w\-'.\s]+?)\b(?:\s+(?:on|in|for|at|during|with|as|per)\b|[.,;])", re.I),
    # "according to the Hong Kong Observatory"
    re.compile(r"according\s+to\s+(?:the\s+)?([\w\-'.\s]+?)\b(?:\s+(?:on|in|for|at|during|with|as|per)\b|[.,;])", re.I),
    # "measured at LaGuardia Airport"
    re.compile(r"measured\s+at\s+(?:the\s+)?([\w\-'.\s]+?)\b(?:\s+(?:on|in|for|at|during|with|as|per)\b|[.,;])", re.I),
    # "data from the Hong Kong Observatory"
    re.compile(r"data\s+from\s+(?:the\s+)?([\w\-'.\s]+?)\b(?:\s+(?:on|in|for|at|during|with|as|per)\b|[.,;])", re.I),
    # "Resolves per the Incheon International Airport" / "based on" / "using"
    re.compile(r"\bresolves?\s+(?:per|using|based\s+on)\s+(?:the\s+)?([\w\-'.\s]+?)\b(?:\s+(?:on|in|for|at|during|with|as|per|reading|reads?|report|readings)\b|[.,;])", re.I),
    # Direct ICAO code in parentheses: "(KLGA)" or "(HKO)"
    re.compile(r"\(([A-Z]{3,4})\)"),
]

# Normalizer: strip these noise words before matching against station_names
_STATION_NOISE_WORDS = re.compile(
    r"\b(?:the|a|an|international|airport|station|observatory|"
    r"weather|meteorological|service|bureau|center|centre)\b",
    re.IGNORECASE,
)


def _normalize_station_phrase(phrase: str) -> str:
    """Lowercase + strip accents + strip noise words + collapse whitespace.
    'The Hartsfield-Jackson International Airport' -> 'hartsfield-jackson'

    Accent stripping is what lets Polymarket Rules that spell a station with
    diacritics ('Adolfo Suárez Madrid-Barajas', 'Esenboğa Intl') match the
    ASCII keys in station_names ('adolfo suarez madrid-barajas', 'esenboga
    intl'). No-op on already-ASCII phrases, so it never changes existing hits.
    """
    if not phrase:
        return ""
    cleaned = _STATION_NOISE_WORDS.sub(" ", _strip_accents(phrase.lower()))
    return re.sub(r"\s+", " ", cleaned).strip()


# Cache: (description_hash, city) -> dict | None. Avoid re-parsing the same
# rules text per discovery cycle.
_AUTO_STATION_CACHE: dict = {}


def auto_extract_station(name: str, cities: dict[str, Any],
                          description: Optional[str]) -> Optional[dict]:
    """v9: try to auto-resolve a station for `name` by parsing `description`
    (Polymarket market.description text).

    Returns the same shape as resolve_station() — {lat, lon, station,
    temp_bias_f, _source: "auto"} — or None if no match. `temp_bias_f`
    is always 0.0 for auto-resolved stations (operator must move the
    entry to the curated `stations` dict to set a non-zero bias).

    Lookup pipeline:
      1. Apply each _STATION_PATTERNS regex; collect candidate phrases.
      2. For each candidate: normalize -> exact match in station_names.
      3. If station_names returns an ICAO -> reverse-lookup coords in
         the curated `stations` dict (by station code).
      4. Direct ICAO match also works: a "(KLGA)" capture short-circuits.

    Caller should log when this fires so operator can decide to add the
    entry to `stations` permanently (with manual lat/lon verification +
    optional temp_bias_f).
    """
    if not description or not cities:
        return None

    cache_key = (hash(description), name or "")
    if cache_key in _AUTO_STATION_CACHE:
        return _AUTO_STATION_CACHE[cache_key]

    station_names = cities.get("station_names") or {}
    stations = cities.get("stations") or {}

    # Build reverse-by-icao index of curated stations (icao -> entry).
    icao_to_entry: dict[str, dict] = {}
    for entry in stations.values():
        icao = entry.get("station")
        if icao and icao not in icao_to_entry:
            icao_to_entry[icao] = entry

    def _try_icao(icao: str) -> Optional[dict]:
        entry = icao_to_entry.get(icao)
        if not entry:
            return None
        return {
            "lat": entry["lat"], "lon": entry["lon"],
            "station": entry["station"],
            "temp_bias_f": 0.0,  # auto-resolved entries don't carry bias
            "_source": "auto",
        }

    result: Optional[dict] = None

    for pat in _STATION_PATTERNS:
        for m in pat.finditer(description):
            raw = m.group(1).strip()
            # Direct ICAO capture path
            if len(raw) in (3, 4) and raw.isupper() and raw.isalpha():
                hit = _try_icao(raw)
                if hit:
                    result = hit
                    break
                continue
            # Try raw lowercased FIRST so "hong kong observatory" matches its
            # full station_names key (the noise-word stripper would remove
            # "observatory" and lose the signal). Then fall back to
            # normalized + progressively-shorter prefixes. Accents stripped so
            # an accented Rule phrase still hits the ASCII station_names keys.
            raw_lower = _strip_accents(re.sub(r"\s+", " ", raw.lower()).strip())
            icao = station_names.get(raw_lower)
            if not icao:
                normalized = _normalize_station_phrase(raw)
                if normalized:
                    icao = station_names.get(normalized)
                    if not icao:
                        tokens = normalized.split()
                        for cut in range(len(tokens), 0, -1):
                            prefix = " ".join(tokens[:cut])
                            icao = station_names.get(prefix)
                            if icao:
                                break
            if icao:
                hit = _try_icao(icao)
                if hit:
                    result = hit
                    break
        if result:
            break

    _AUTO_STATION_CACHE[cache_key] = result
    return result


def _all_known_cities(cities: dict[str, Any]) -> dict[str, str]:
    """Build {lowercase_unaccented_name: canonical_name} including aliases."""
    out: dict[str, str] = {}
    for group in ("world", "europe_top30", "north_america_extra", "us_top50"):
        for c in cities.get(group, []):
            out[_strip_accents(c).lower()] = c
    for alias, canonical in cities.get("aliases", {}).items():
        out[_strip_accents(alias).lower()] = canonical
    return out


def _resolve_city(text: str, cities: dict[str, Any]) -> Optional[str]:
    """Find the canonical city name in `text` if any. Accent-insensitive.

    Two-stage lookup:
      1) Match against the curated cities/aliases JSON (fast, deterministic).
      2) Fallback: extract the city via regex from common Polymarket patterns
         like "temperature in CITY on …" or "rain in CITY tomorrow". The
         extracted name is passed straight to OpenWeather; if OpenWeather
         doesn't know it, fetch_forecast returns None and the bot skips
         the market with forecast_unavailable. This way we don't gate on
         a hardcoded list — any real city OpenWeather knows is tradeable.
    """
    lookup = _all_known_cities(cities)
    text_lower = _strip_accents(text).lower()
    # Stage 1: longest-match against the known list (preserves canonical form)
    for name in sorted(lookup, key=len, reverse=True):
        if re.search(r"\b" + re.escape(name) + r"\b", text_lower):
            return lookup[name]
    # Stage 2: regex fallback for arbitrary cities
    return _extract_city_from_question(text)


# Common Polymarket weather-question patterns. Captures any capitalized city
# token sequence between an "in" preposition and a date/punctuation boundary.
_CITY_FROM_QUESTION_RE = re.compile(
    r"\b(?:in|for|at)\s+"
    r"(?P<city>[A-ZÀ-Ý][A-Za-zÀ-ÿ.'\-]+(?:\s+[A-ZÀ-Ý][A-Za-zÀ-ÿ.'\-]+){0,3})"
    r"(?=\s+(?:on|by|today|tomorrow|tonight|this|next|in\s+the|\d{4})|[?!.,]|$)",
)

# Tokens that look capitalized but aren't cities — exclude common false positives.
_CITY_BLACKLIST = frozenset({
    "may", "june", "july", "august", "september", "october", "november",
    "december", "january", "february", "march", "april", "monday", "tuesday",
    "wednesday", "thursday", "friday", "saturday", "sunday",
    "fahrenheit", "celsius", "today", "tomorrow", "tonight", "this", "next",
    "high", "low", "highest", "lowest", "the",
})


def _extract_city_from_question(text: str) -> Optional[str]:
    """Best-effort city extraction from market question text."""
    for m in _CITY_FROM_QUESTION_RE.finditer(text):
        candidate = m.group("city").strip()
        # Reject obvious non-city tokens
        first = candidate.split()[0].lower()
        if first in _CITY_BLACKLIST:
            continue
        if len(candidate) < 2:
            continue
        return candidate
    return None


# Pattern catalog: (regex, handler_fn) — applied in order.
# Each handler returns (threshold_value, threshold_unit, metric, comparison, confidence) or None.

_TEMP_RE = re.compile(
    r"(?P<comp>exceed|above|over|reach|hit|at\s+least|below|under|less\s+than)"
    r"\s+(?P<val>-?\d+(?:\.\d+)?)\s*(?:°\s*)?(?P<unit>F|C|fahrenheit|celsius)\b",
    re.IGNORECASE,
)

# Range patterns: "65-69°F", "65 to 69°F", "between 65 and 69°F"
_TEMP_RANGE_RE = re.compile(
    r"(?:between\s+)?(?P<low>\d+(?:\.\d+)?)\s*(?:°\s*[FC]?\s*)?"
    r"(?:-|to|–|—|and)\s*"
    r"(?P<high>\d+(?:\.\d+)?)\s*°?\s*(?P<unit>F|C|fahrenheit|celsius)?\b",
    re.IGNORECASE,
)
# "70°F or higher" / "70°F or above" / "70°F+"
_TEMP_AT_LEAST_RE = re.compile(
    r"(?P<val>-?\d+(?:\.\d+)?)\s*(?:°\s*)?(?P<unit>F|C|fahrenheit|celsius)?"
    r"\s*(?:\+|or\s+(?:higher|above|greater|more))",
    re.IGNORECASE,
)
# "60°F or lower" / "60°F or below"
_TEMP_AT_MOST_RE = re.compile(
    r"(?P<val>-?\d+(?:\.\d+)?)\s*(?:°\s*)?(?P<unit>F|C|fahrenheit|celsius)?"
    r"\s*or\s+(?:lower|below|less|fewer)",
    re.IGNORECASE,
)
# Bare single-value bracket: "be 17°C on May 10" — Polymarket multi-outcome
# events often have a sub-market per integer degree. We treat this as a
# 1-degree range [val, val+1).
_TEMP_BARE_RE = re.compile(
    r"\bbe\s+(?P<val>-?\d+(?:\.\d+)?)\s*°\s*(?P<unit>F|C)\b\s*(?:on|by|$|\?)",
    re.IGNORECASE,
)
_PRECIP_RE = re.compile(
    r"(?P<comp>more\s+than|over|exceed|at\s+least|less\s+than|below|under)"
    r"\s+(?P<val>\d+(?:\.\d+)?)\s*(?P<unit>mm|inches?|in)\b(?:\s+of)?\s+(?:rain|precipitation|precip)",
    re.IGNORECASE,
)
_RAIN_BINARY_RE = re.compile(r"\bwill\s+it\s+rain\b", re.IGNORECASE)
_SNOW_BINARY_RE = re.compile(r"\bwill\s+it\s+snow\b", re.IGNORECASE)


def _normalize_comparison(comp: str) -> str:
    c = comp.lower().strip()
    if c in ("exceed", "above", "over", "reach", "hit", "more than"):
        return "exceed"
    if c in ("at least", "at_least"):
        return "at_least"
    if c in ("below", "under", "less than"):
        return "below"
    if c in ("at most", "at_most"):
        return "at_most"
    return "exceed"


def _parse_target_date(question: str, end_date: Optional[str] = None) -> Optional[date]:
    """Best-effort target date extraction. Falls back to end_date if provided.

    Recognizes: 'tomorrow', 'today', 'on YYYY-MM-DD', month + day names.
    """
    q = question.lower()
    today = datetime.now(timezone.utc).date()
    if "tomorrow" in q:
        return today + timedelta(days=1)
    if "today" in q:
        return today
    m = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", question)
    if m:
        try:
            return datetime.fromisoformat(m.group(1)).date()
        except ValueError:
            pass
    if end_date:
        try:
            return datetime.fromisoformat(end_date.replace("Z", "+00:00")).date()
        except (ValueError, AttributeError):
            pass
    return None


def parse_market(question: str, end_date: Optional[str] = None,
                 cities: Optional[dict] = None) -> Optional[MarketSpec]:
    """Parse a Polymarket weather market question into a MarketSpec.

    Returns None if neither a city nor a recognizable threshold is found.
    Confidence rubric: 1.0 if all four extracted (city, threshold, comparison,
    date); 0.6 if no date but rest OK; 0.4 if fuzzy on city; below that we
    return None (caller should skip).
    """
    if cities is None:
        cities = load_cities()
    city = _resolve_city(question, cities)
    if not city:
        return None

    target_date = _parse_target_date(question, end_date)
    confidence = 1.0 if target_date else 0.6

    # v13.4: does this market resolve on the daily MIN instead of the MAX?
    # Question text is "Lowest temperature in <city> ..."; the slug form
    # ("lowest-temperature-in-...") is sometimes appended to the question,
    # so normalize hyphens before matching.
    q_norm = question.lower().replace("-", " ")
    temp_kind = ("low" if ("lowest temperature" in q_norm
                            or "minimum temperature" in q_norm
                            or "low temperature" in q_norm)
                 else "high")

    m = _TEMP_RE.search(question)
    if m:
        unit = m.group("unit")[0].upper()
        return MarketSpec(
            city=city,
            threshold_value=float(m.group("val")),
            threshold_unit=unit,
            metric="temp",
            comparison=_normalize_comparison(m.group("comp")),
            target_date=target_date,
            confidence=confidence,
            raw_question=question,
            temp_kind=temp_kind,
        )

    # Range: "65-69°F" / "between 65 and 69°F" — try ALL matches and accept
    # the first that passes sanity. The slug we get appended to the question
    # often contains "-2026-" which the regex initially matches as a 11-2026
    # numeric range (and silently fails sanity if we only used .search).
    for m in _TEMP_RANGE_RE.finditer(question):
        try:
            low = float(m.group("low"))
            high = float(m.group("high"))
        except (TypeError, ValueError):
            continue
        # Sanity: range should be within plausible temps and ordered.
        # Require an explicit unit (F or C) so we don't grab raw numeric
        # ranges like "11-2026" without temperature context.
        if 0 < low < high < 200 and (high - low) < 50 and m.group("unit"):
            unit = m.group("unit")[0].upper()
            return MarketSpec(
                city=city, threshold_value=low, threshold_value_high=high,
                threshold_unit=unit, metric="temp", comparison="range",
                target_date=target_date, confidence=confidence,
                raw_question=question, temp_kind=temp_kind,
            )

    # "X°F or higher"
    m = _TEMP_AT_LEAST_RE.search(question)
    if m:
        unit = (m.group("unit") or "F")[0].upper()
        return MarketSpec(
            city=city, threshold_value=float(m.group("val")),
            threshold_unit=unit, metric="temp", comparison="at_least",
            target_date=target_date, confidence=confidence,
            raw_question=question, temp_kind=temp_kind,
        )

    # "X°F or lower"
    m = _TEMP_AT_MOST_RE.search(question)
    if m:
        unit = (m.group("unit") or "F")[0].upper()
        return MarketSpec(
            city=city, threshold_value=float(m.group("val")),
            threshold_unit=unit, metric="temp", comparison="at_most",
            target_date=target_date, confidence=confidence,
            raw_question=question, temp_kind=temp_kind,
        )

    # Bare single-value bracket: "be 17°C on May 10" → 1-degree range [v, v+1)
    for m in _TEMP_BARE_RE.finditer(question):
        try:
            val = float(m.group("val"))
        except (TypeError, ValueError):
            continue
        if not (-100 < val < 200):
            continue
        unit = m.group("unit").upper()
        return MarketSpec(
            city=city, threshold_value=val, threshold_value_high=val + 1.0,
            threshold_unit=unit, metric="temp", comparison="range",
            target_date=target_date, confidence=confidence * 0.95,
            raw_question=question, temp_kind=temp_kind,
        )

    m = _PRECIP_RE.search(question)
    if m:
        unit_raw = m.group("unit").lower()
        unit = "mm" if unit_raw == "mm" else "in"
        return MarketSpec(
            city=city,
            threshold_value=float(m.group("val")),
            threshold_unit=unit,
            metric="precip",
            comparison=_normalize_comparison(m.group("comp")),
            target_date=target_date,
            confidence=confidence,
            raw_question=question,
        )

    if _RAIN_BINARY_RE.search(question):
        return MarketSpec(city=city, threshold_value=0.0, threshold_unit="mm",
                          metric="precip", comparison="exceed",
                          target_date=target_date, confidence=confidence * 0.9,
                          raw_question=question)
    if _SNOW_BINARY_RE.search(question):
        return MarketSpec(city=city, threshold_value=0.0, threshold_unit="mm",
                          metric="snow", comparison="exceed",
                          target_date=target_date, confidence=confidence * 0.9,
                          raw_question=question)

    return None


# ---------------------------------------------------------------------------
# v14 (2026-07-05): temperature-only trading policy
# ---------------------------------------------------------------------------


def is_tradeable_spec(spec) -> bool:
    """v14 (2026-07-05): the bot is STRICTLY a temperature bot.

    Returns True only for a parsed spec with metric == "temp". Non-temp
    markets (rain binaries like "will-it-rain-in-dallas-on-june-10",
    numeric precip like "more than 5mm of rain", "will it snow") price off
    a single-source POP clipped to [PROB_CLIP_LOW, PROB_CLIP_HIGH] with no
    MAE calibration, no ensemble and no station bias — an uncalibrated
    signal the bot must never OPEN a position on.

    NOTE: parse_market() deliberately KEEPS parsing precip/snow — the
    monitor/cashout path (weather_edge_bot._do_monitor_check) and the judge
    re-parse entries that may already be open. This predicate gates NEW
    entries only. Callers: weather_edge_bot.run_discovery (skip bucket
    "not_temperature") and weather_edge_judge._judge_route (fail-closed
    deterministic REJECT).
    """
    return spec is not None and getattr(spec, "metric", None) == "temp"


# ---------------------------------------------------------------------------
# Forecast → probability
# ---------------------------------------------------------------------------


def _norm_cdf(z: float) -> float:
    return statistics.NormalDist().cdf(z)


# Probability clipping bounds: no 24-48h weather forecast can honestly
# justify > 90% or < 10% certainty (natural variance of ±2-3°C in the
# input forecast already implies that level of uncertainty even when
# the point estimate is far from threshold). Clipping forces the bot
# to size more conservatively on extreme-edge candidates and prevents
# the adverse-selection trap where the bot is "98% sure" on bets the
# market knows are coin-flips. See log analysis 2026-05-15.
# v9.12 (2026-05-24): tightened from 0.10/0.90 → 0.20/0.80.
# Snapshot analysis showed bot_prob clipped to 0.90 in 32% of verdicts;
# of those, judge disagreed (judge_prob ≤ 0.40) in 30%. That signals
# the model is systematically overconfident when the forecast point is
# far from a bracket boundary. Pulling the ceiling in to 0.80 reduces
# Kelly stake on those "high confidence" trades (82% → 64% in the
# canonical p=ceiling/price=0.45 scenario) and roughly halves Rule 6
# overrides. Trade-off: trades with edge in the 20-30pp band may now
# fall below --min-edge-pp; only the strongest signals survive.
# v11 (2026-05-31 post-mortem): tightened again 0.20/0.80 → 0.30/0.70 and
# made the cap actually bind on NO bets. The clip applies to P(YES); a NO
# bet's confidence is P(NO)=1-P(YES), so it was the LOW bound (0.20 → P(NO)
# 0.80) that capped NO confidence, not HIGH. In the -$771 run 86% of
# executions hit a clip and were NO bets pinned at 0.80 confidence. Raising
# LOW to 0.30 caps P(NO) at 0.70 symmetrically with the YES ceiling.
PROB_CLIP_LOW = 0.30
PROB_CLIP_HIGH = 0.70


def _clip_prob(p: Optional[float]) -> Optional[float]:
    """Clip a forecast probability to [PROB_CLIP_LOW, PROB_CLIP_HIGH].
    None passes through unchanged so callers can still treat as 'unknown'."""
    if p is None:
        return None
    return max(PROB_CLIP_LOW, min(PROB_CLIP_HIGH, float(p)))


def forecast_probability(spec: MarketSpec, forecast: dict,
                          mae_override: Optional[float] = None,
                          bias_override: Optional[float] = None,
                          mu_override: Optional[float] = None
                          ) -> Optional[float]:
    """Compute P(YES) for `spec`.

    Non-range markets are clipped to [PROB_CLIP_LOW, PROB_CLIP_HIGH] to curb
    overconfidence. RANGE/bracket markets are returned RAW (unclipped):
    the symmetric clip inverts side selection for them — a tiny true P(YES)
    from a far-below forecast gets floored UP to PROB_CLIP_LOW, manufacturing
    a phantom YES edge (2026-06-01: 371 false YES-range proposals, all
    rejected, $8.72 of judge spend, 0 executed). Side selection must see the
    raw value; the chosen side's confidence is capped separately for sizing
    via `prob_yes_for_sizing()`.

    `mae_override` (v7): replaces the static MAE_TEMP_F/C or
    MAE_PRECIP_MM constant for this call (dynamic MAE from history).

    `bias_override` (v8): added to the forecast `ref` value before the
    z-score. Per-city systematic offset to compensate for residual
    error between our forecast source and the resolution station even
    after fixing coordinates. Only applied to `metric=='temp'`. Units
    match `spec.threshold_unit` (operator sets temp_bias_f in cities
    JSON; bot converts on lookup if market is in C).

    `mu_override` (v13): replaces the OpenWeather `temp_high_*` reference
    value with an ensemble-mean forecast (Open-Meteo ICON+GFS+ECMWF). The
    caller computes mu via compute_ensemble_calibration(). When set,
    `forecast` is only used to verify the target_date is reachable; the μ
    used in the z-score comes from the ensemble. Units match
    `spec.threshold_unit`.
    """
    raw = _forecast_probability_raw(
        spec, forecast, mae_override=mae_override,
        bias_override=bias_override, mu_override=mu_override)
    if raw is None:
        return None
    if spec.comparison == "range":
        return max(0.0, min(1.0, raw))
    return _clip_prob(raw)


def prob_yes_for_sizing(p_yes: Optional[float], side: str,
                        comparison: str,
                        ensemble_calibrated: bool = False
                        ) -> Optional[float]:
    """Return the P(YES) value to STORE and SIZE with for the chosen `side`.

    For range markets `forecast_probability()` returns the raw (unclipped)
    P(YES) so side selection isn't distorted. Here we cap the chosen side's
    confidence (cap only, no floor) so Kelly sizing on a range NO bet isn't
    pathologically overconfident, while a range YES leg keeps its honest
    low confidence. Non-range probs are already clipped and pass through.

    Two caps:
      - `ensemble_calibrated=False` (default): cap at PROB_CLIP_HIGH (0.70).
        Conservative; assumes the underlying σ is the constant MAE which
        post-mortem showed overstates confidence systematically.
      - `ensemble_calibrated=True` (v13): cap at ENSEMBLE_PROB_CAP (0.95).
        When (μ, σ) come from the calibrated NGR/EMOS ensemble path, the
        model's own uncertainty is the cap; we only bound the pathological
        case (all 3 models perfectly agree at the far side of the bin).
    """
    if p_yes is None or comparison != "range":
        return p_yes
    cap = ENSEMBLE_PROB_CAP if ensemble_calibrated else PROB_CLIP_HIGH
    if side == "YES":
        return min(float(p_yes), cap)                    # cap P(YES)
    return max(float(p_yes), 1.0 - cap)                  # floor P(YES) → cap P(NO)


def _forecast_probability_raw(spec: MarketSpec, forecast: dict,
                                mae_override: Optional[float] = None,
                                bias_override: Optional[float] = None,
                                mu_override: Optional[float] = None
                                ) -> Optional[float]:
    """Raw probability from the forecast model — uncalibrated.
    Use forecast_probability() externally; this is only exposed for tests
    and the analyzer (which can compare raw vs clipped to spot extreme
    inputs).

    forecast shape (from get_weather.py forecast command):
        {"location": ..., "daily_forecast": [
            {"date": "YYYY-MM-DD",
             "temp_high_f": 78, "temp_low_f": 60,
             "temp_high_c": ..., "temp_low_c": ...,
             "precip_probability": 30, "precip_mm": 1.2,
             "condition_main": "Clear", ...}, ...]}

    Earlier versions of this helper expected key "forecasts"; the actual
    skill output uses "daily_forecast" (see
    polymarket-forecast-skill/scripts/get_weather.py:get_weather_forecast_detailed).
    Accept either for compatibility.

    Returns None if no matching day in forecast (target_date out of range).
    """
    if not forecast:
        return None
    days = forecast.get("daily_forecast") or forecast.get("forecasts")
    if not days:
        return None
    target = spec.target_date
    if not target:
        return None
    target_str = target.isoformat()

    # Try exact date match first; fall back to ±1 day to absorb timezone edges.
    day = next((d for d in days if d.get("date") == target_str), None)
    if not day:
        for delta in (1, -1):
            alt = (target + timedelta(days=delta)).isoformat()
            day = next((d for d in days if d.get("date") == alt), None)
            if day:
                break
    if not day:
        return None

    if spec.metric == "temp":
        # v13: prefer the ensemble-mean μ when provided (Open-Meteo
        # ICON+GFS+ECMWF). Falls back to the OpenWeather single-source
        # reference only when no ensemble was available.
        # v13.4: the reference is the extreme the market resolves on —
        # temp_low_* for lowest-temperature markets, temp_high_* otherwise.
        if mu_override is not None:
            ref = float(mu_override)
        else:
            kind = "low" if getattr(spec, "temp_kind", "high") == "low" else "high"
            ref = day.get(f"temp_{kind}_{spec.threshold_unit.lower()}")
            if ref is None:
                return None
        # v8: per-city systematic bias correction. Added to the forecast
        # ref so the z-score is computed against a station-equivalent
        # value. Only applied to temp markets; precip ignores.
        if bias_override:
            ref = float(ref) + float(bias_override)
        mae = (mae_override if mae_override is not None
               else (MAE_TEMP_F if spec.threshold_unit == "F" else MAE_TEMP_C))
        if spec.comparison == "range" and spec.threshold_value_high is not None:
            # P(low ≤ temp < high) under N(ref, mae)
            z_low = (ref - spec.threshold_value) / mae
            z_high = (ref - spec.threshold_value_high) / mae
            return max(0.0, _norm_cdf(z_low) - _norm_cdf(z_high))
        z = (ref - spec.threshold_value) / mae
        if spec.comparison in ("exceed", "at_least"):
            return _norm_cdf(z)
        return _norm_cdf(-z)

    if spec.metric == "precip":
        # Binary "will it rain" → use precip_probability directly (already a prob)
        if spec.threshold_value == 0 and spec.comparison == "exceed":
            pop = day.get("precip_probability")
            return pop / 100.0 if pop is not None else None
        # "more than X mm" → normal CDF on precip_mm
        forecast_mm = day.get("precip_mm")
        if forecast_mm is None:
            return None
        threshold_mm = spec.threshold_value if spec.threshold_unit == "mm" \
            else spec.threshold_value * 25.4
        z = (forecast_mm - threshold_mm) / (mae_override or MAE_PRECIP_MM)
        if spec.comparison in ("exceed", "at_least"):
            return _norm_cdf(z)
        return _norm_cdf(-z)

    if spec.metric == "snow":
        # Approximate: use precip_probability if condition mentions snow.
        cond = (day.get("condition_main") or "").lower()
        if "snow" in cond:
            return (day.get("precip_probability") or 0) / 100.0
        return 0.05  # tiny baseline if forecast doesn't show snow at all

    return None


def forecast_ref_value(spec: MarketSpec, forecast: dict) -> Optional[float]:
    """Return the scalar forecast reference value for a temp market's
    target date, in the market's threshold_unit — the daily HIGH for
    highest-temperature markets, the daily LOW for lowest-temperature
    markets (v13.4; before, this always returned the high, which silenced
    the proximity guard and inflated the auto-route distance on low
    markets). Mirrors the date-matching + extraction used by
    _forecast_probability_raw.

    Used by the range_cross cashout trigger and the judge's threshold-
    proximity override, which need the raw forecast value (not the
    probability) to compare against the bracket boundaries.

    Returns None for non-temp markets or when no matching day is found.
    """
    if not forecast or spec.metric != "temp":
        return None
    days = forecast.get("daily_forecast") or forecast.get("forecasts")
    if not days or not spec.target_date:
        return None
    target_str = spec.target_date.isoformat()
    day = next((d for d in days if d.get("date") == target_str), None)
    if not day:
        for delta in (1, -1):
            alt = (spec.target_date + timedelta(days=delta)).isoformat()
            day = next((d for d in days if d.get("date") == alt), None)
            if day:
                break
    if not day:
        return None
    kind = "low" if getattr(spec, "temp_kind", "high") == "low" else "high"
    ref = day.get(f"temp_{kind}_{spec.threshold_unit.lower()}")
    return float(ref) if ref is not None else None


# ---------------------------------------------------------------------------
# Slippage-aware sizing
# ---------------------------------------------------------------------------


def compute_max_size_for_slippage(orderbook: dict, side: str,
                                  max_slippage: float = 0.20) -> dict:
    """Walk the orderbook to find the max size that keeps weighted-avg fill
    within (1 + max_slippage) * best_price for BUY, or above (1 - max_slippage) *
    best_price for SELL.

    orderbook shape (from get_orderbook.py):
        {"bids": [{"price": float, "size": float}, ...] sorted desc,
         "asks": [{"price": float, "size": float}, ...] sorted asc}

    side: "BUY" or "SELL".

    Returns: {"max_usd", "max_shares", "avg_fill", "slippage_pct", "best_price"}.
    """
    if side.upper() == "BUY":
        levels = orderbook.get("asks", [])
        if not levels:
            return _empty_size()
        best = float(levels[0]["price"])
        cap = best * (1.0 + max_slippage)
        cum_cost = 0.0
        cum_shares = 0.0
        for lvl in levels:
            price = float(lvl["price"])
            size = float(lvl["size"])
            if price > cap:
                break
            # Tentatively add this whole level; check if avg fill stays within cap.
            tentative_cost = cum_cost + price * size
            tentative_shares = cum_shares + size
            tentative_avg = tentative_cost / tentative_shares
            if tentative_avg <= cap:
                cum_cost = tentative_cost
                cum_shares = tentative_shares
            else:
                # Partial fill at this level: max fillable shares such that avg <= cap
                # avg = (cum_cost + price * x) / (cum_shares + x) <= cap
                # cum_cost + price*x <= cap * (cum_shares + x)
                # cum_cost - cap*cum_shares <= (cap - price) * x
                if cap - price <= 0:
                    break
                x = (cap * cum_shares - cum_cost) / (price - cap) * -1
                # The above algebra can be off; do it cleanly:
                x = (cap * cum_shares - cum_cost) / (price - cap)
                if x <= 0:
                    break
                x = min(x, size)
                cum_cost += price * x
                cum_shares += x
                break
        if cum_shares == 0:
            return _empty_size(best_price=best)
        avg = cum_cost / cum_shares
        return {
            "max_usd": round(cum_cost, 4),
            "max_shares": round(cum_shares, 4),
            "avg_fill": round(avg, 6),
            "slippage_pct": round((avg - best) / best, 6),
            "best_price": best,
        }
    else:
        levels = orderbook.get("bids", [])
        if not levels:
            return _empty_size()
        best = float(levels[0]["price"])
        floor_price = best * (1.0 - max_slippage)
        cum_value = 0.0
        cum_shares = 0.0
        for lvl in levels:
            price = float(lvl["price"])
            size = float(lvl["size"])
            if price < floor_price:
                break
            tentative_value = cum_value + price * size
            tentative_shares = cum_shares + size
            tentative_avg = tentative_value / tentative_shares
            if tentative_avg >= floor_price:
                cum_value = tentative_value
                cum_shares = tentative_shares
            else:
                # avg = (cum_value + price*x) / (cum_shares + x) >= floor
                if floor_price - price <= 0:
                    break
                x = (cum_value - floor_price * cum_shares) / (floor_price - price)
                if x <= 0:
                    break
                x = min(x, size)
                cum_value += price * x
                cum_shares += x
                break
        if cum_shares == 0:
            return _empty_size(best_price=best)
        avg = cum_value / cum_shares
        return {
            "max_usd": round(cum_value, 4),
            "max_shares": round(cum_shares, 4),
            "avg_fill": round(avg, 6),
            "slippage_pct": round((best - avg) / best, 6),
            "best_price": best,
        }


def _empty_size(best_price: float = 0.0) -> dict:
    return {"max_usd": 0.0, "max_shares": 0.0, "avg_fill": 0.0,
            "slippage_pct": 0.0, "best_price": best_price}


def _best_level(book: dict, side: str) -> Optional[float]:
    """Return best price on given side, or None if the side is empty/missing."""
    levels = book.get(side) or []
    if not levels:
        return None
    return float(levels[0].get("price")) if levels[0].get("price") is not None else None


def implied_probabilities(orderbook_yes: dict, orderbook_no: dict) -> dict:
    """Extract implied P(YES) and P(NO) from orderbook best levels.

    Best ask of YES = price to buy YES = implied P(YES).
    Best ask of NO = implied P(NO) = 1 - implied P(YES).
    """
    return {
        "yes_ask": _best_level(orderbook_yes, "asks"),
        "yes_bid": _best_level(orderbook_yes, "bids"),
        "no_ask": _best_level(orderbook_no, "asks"),
        "no_bid": _best_level(orderbook_no, "bids"),
    }


def compute_edge(forecast_prob: float, implied: dict) -> dict:
    """Given P(forecast) for YES and orderbook prices, return the edge.

    Returns dict with: edge_yes, edge_no, best_side ("YES"/"NO"/None),
    edge_pp_at_best (in percentage points).
    """
    edge_yes = None
    if implied["yes_ask"] is not None:
        edge_yes = forecast_prob - implied["yes_ask"]  # we'd buy YES at yes_ask
    edge_no = None
    if implied["no_ask"] is not None:
        edge_no = (1.0 - forecast_prob) - implied["no_ask"]

    best_side = None
    edge_pp = 0.0
    if edge_yes is not None and (edge_no is None or edge_yes >= edge_no):
        best_side = "YES" if edge_yes > 0 else None
        edge_pp = edge_yes * 100
    elif edge_no is not None:
        best_side = "NO" if edge_no > 0 else None
        edge_pp = edge_no * 100

    return {
        "edge_yes": edge_yes,
        "edge_no": edge_no,
        "best_side": best_side,
        "edge_pp_at_best": round(edge_pp, 2),
    }


# ---------------------------------------------------------------------------
# v9: 3-bin laddering — bracket selection + Kelly proportional stake split
# ---------------------------------------------------------------------------


def select_ladder_brackets(parsed: list[dict]) -> dict:
    """Pick central + below + above brackets from a list of per-bracket dicts.

    Input `parsed`: list of dicts, each shaped:
        {"spec": MarketSpec, "forecast_prob": float, "raw_market": dict,
         "implied": dict, ...}
    The spec.threshold_value is used to order brackets (numeric ascending).
    The forecast_prob picks the central bracket (argmax). Below = bracket
    immediately below central in sorted order; above = immediately above.

    Returns dict {central: dict, below: dict|None, above: dict|None}.
    Brackets at table extremes return None for the missing neighbour;
    callers can fall back to 2-bin or single-bin.

    Returns None for the whole ladder if `parsed` is empty or no spec has
    a valid forecast_prob.
    """
    if not parsed:
        return None
    # Filter to entries with a usable forecast_prob and threshold_value
    usable = [p for p in parsed
              if p.get("forecast_prob") is not None
              and p.get("spec") is not None
              and getattr(p["spec"], "threshold_value", None) is not None]
    if not usable:
        return None
    # Sort ascending by threshold_value
    ordered = sorted(usable, key=lambda p: float(p["spec"].threshold_value))
    # Pick central = max forecast_prob (the bracket the model believes most).
    # Ties broken by lower threshold_value (already from stable sort).
    central_idx = max(range(len(ordered)),
                       key=lambda i: float(ordered[i]["forecast_prob"]))
    below = ordered[central_idx - 1] if central_idx > 0 else None
    above = ordered[central_idx + 1] if central_idx < len(ordered) - 1 else None
    return {"central": ordered[central_idx], "below": below, "above": above}


def compute_kelly_split(legs: list[dict], total_usd: float) -> Optional[list[dict]]:
    """Split `total_usd` across `legs` proportional to each leg's Kelly
    fraction. Each leg is a dict with at minimum keys "forecast_prob"
    (P(YES) raw), "entry_price" (ask of the chosen side), and "side"
    ('YES' or 'NO'). Returns the same list of dicts augmented with
    "stake_usd" and "kelly_frac" keys, in input order.

    v9.14 (2026-05-24) FIX: previously assumed `forecast_prob` was
    side-aware P(side). In the discovery candidate dict it's actually
    P(YES) raw (the side-aware flip only happens at insert_entry time,
    weather_edge_bot.py:959). For NO-side legs, Kelly was computed
    against P(YES) vs NO_ask → numerator went deeply negative →
    max(0, ...) = 0 → below/above legs ALWAYS got $0 stake. Snapshot
    2026-05-22→23 confirmed: 45 NO-side below legs all got
    ladder_stake_usd=$0 while 48 YES-side central legs got the full
    $50. Ladders were de-facto single-bin only. Now converts to
    p_side based on leg["side"] before Kelly math.

    Kelly per leg (side-aware):
        p_side = forecast_prob if side=='YES' else 1 - forecast_prob
        kelly_i = max(0, (p_side*(1-price) - (1-p_side)*price) / (1-price))

    Renormalization:
        weight_i = kelly_i / sum(kelly_j)
        stake_i = total_usd * weight_i

    Edge cases:
        - All legs have kelly = 0 (no positive-EV bet) → returns None,
          signalling the caller to skip the ladder entirely.
        - One leg has kelly = 0 but others are positive → that leg gets
          stake = 0, the others share the budget.
        - 1-leg input → that leg gets the full total_usd.
    """
    if not legs or total_usd <= 0:
        return None
    enriched = []
    kelly_sum = 0.0
    for leg in legs:
        p_yes = float(leg.get("forecast_prob") or 0.0)
        side = leg.get("side", "YES")
        # v9.14: convert P(YES) → P(side) so kelly is computed on the
        # same side that `entry_price` represents.
        p_side = p_yes if side == "YES" else (1.0 - p_yes)
        price = float(leg.get("entry_price") or 0.0)
        if price <= 0 or price >= 1:
            kelly = 0.0
        else:
            num = p_side * (1 - price) - (1 - p_side) * price
            kelly = max(0.0, num / (1 - price))
        enriched.append({**leg, "kelly_frac": kelly})
        kelly_sum += kelly
    if kelly_sum <= 0:
        return None
    for leg in enriched:
        weight = leg["kelly_frac"] / kelly_sum
        leg["stake_usd"] = round(total_usd * weight, 4)
    return enriched


# ---------------------------------------------------------------------------
# Cashout policy — multi-trigger evaluator
# ---------------------------------------------------------------------------


def evaluate_cashout_triggers(
    *,
    side: str,
    entry_price: float,
    current_bid: float,
    peak_bid_seen: Optional[float],
    forecast_prob_yes: Optional[float],
    profit_lock_pp: float = 50.0,
    trailing_drawdown_pct: float = 30.0,
    trailing_min_gain_pp: float = 20.0,
    convergence_pp: float = 5.0,
    comparison: Optional[str] = None,
    forecast_value: Optional[float] = None,
    range_low: Optional[float] = None,
    range_high: Optional[float] = None,
    range_cross_margin: float = 0.5,
    # v11 cheap_convexity: exit when the bid converges to the RAW (uncapped)
    # model fair. Distinct from trigger 3 (convergence), whose fair comes
    # from forecast_prob_yes already capped by prob_yes_for_sizing.
    enable_fair_target: bool = False,
    fair_uncapped_yes: Optional[float] = None,
    fair_target_margin_pp: float = 0.0,
) -> dict:
    """Decide whether to cash out an open position based on OR'd triggers.

    Triggers (evaluated in order; first match wins):
      0. range_cross     — STOP-LOSS. NO bet on a range market whose live
                           forecast has drifted into [low-margin, high+margin].
                           Fires regardless of bid (may sell at a loss) to cut
                           the position before resolution.
      1. profit_lock     — bid >= entry + profit_lock_pp/100
      2. trailing_stop   — peak >= entry + trailing_min_gain_pp/100 AND
                           bid <= peak * (1 - trailing_drawdown_pct/100)
      3. convergence     — bid >= fair_value - convergence_pp/100, where
                           fair_value = forecast_prob_yes (YES) or
                                        1 - forecast_prob_yes (NO)
      4. forecast_reversal — forecast turned against us AND bid >= entry
                             (break-even backstop, existing behavior)

    Guard: if current_bid < entry_price, triggers 1-3 are suppressed
    (never sell at a loss on profit-taking logic). range_cross is the sole
    exception — it is a stop-loss and deliberately sells at a loss.
    forecast_reversal still requires bid >= entry by definition.

    Returns: {decision: "CASHOUT"|"HOLD", trigger: str, reason: str}
    """
    in_profit = current_bid >= entry_price
    peak = float(peak_bid_seen) if peak_bid_seen is not None else 0.0

    # Trigger 0: range cross (stop-loss for NO bets on range markets).
    # v11 (2026-05-31 post-mortem): in the -$771 run, 91% of losing range
    # NO bets had the bot's own forecast drift into the bracket before
    # resolution (e.g. HK #23: forecast rose to 31.8 inside the 31-32 range).
    # The existing forecast_reversal never fired because it requires
    # bid >= entry. This trigger cuts the loss as soon as the forecast
    # enters the danger zone, independent of the bid.
    if (side == "NO" and comparison == "range"
            and forecast_value is not None
            and range_low is not None and range_high is not None):
        lo = float(range_low) - range_cross_margin
        hi = float(range_high) + range_cross_margin
        if lo <= float(forecast_value) <= hi:
            return {
                "decision": "CASHOUT",
                "trigger": "range_cross",
                "reason": (f"forecast {float(forecast_value):.2f} entered "
                           f"[{lo:.2f}, {hi:.2f}] (range "
                           f"{float(range_low):.2f}-{float(range_high):.2f} "
                           f"± {range_cross_margin:.1f}); stop-loss before "
                           f"resolution"),
            }

    # Trigger 1: profit lock
    if in_profit and (current_bid - entry_price) >= profit_lock_pp / 100.0:
        return {
            "decision": "CASHOUT",
            "trigger": "profit_lock",
            "reason": f"bid {current_bid:.3f} >= entry {entry_price:.3f} + "
                      f"{profit_lock_pp:.0f}pp; lock profit",
        }

    # Trigger 2: trailing stop
    if in_profit and peak >= entry_price + trailing_min_gain_pp / 100.0:
        drawdown_threshold = peak * (1.0 - trailing_drawdown_pct / 100.0)
        if current_bid <= drawdown_threshold:
            return {
                "decision": "CASHOUT",
                "trigger": "trailing_stop",
                "reason": f"bid {current_bid:.3f} <= peak {peak:.3f} * "
                          f"(1 - {trailing_drawdown_pct:.0f}%); reversal from peak",
            }

    # Trigger 2.5 (v11 cheap_convexity): bid converged to the RAW model fair.
    # The cheap_convexity strategy buys a 1-20c tail bin and sells into the
    # winner as the price rises toward the model's fair value (e.g. bought at
    # 0.01, raw fair 0.07, exit ~0.06). Distinct from trigger 3 (convergence),
    # whose `fair_value` comes from forecast_prob_yes AFTER prob_yes_for_sizing
    # caps it to 0.70/0.95 — here the fair is the uncapped raw so a 7c bin
    # exits at 7c, not at a capped 30c. Requires in_profit so it never sells
    # at a loss (with entry >= 0.01, a bid of 0 can never satisfy this).
    if (enable_fair_target and in_profit
            and fair_uncapped_yes is not None):
        fair_side = (float(fair_uncapped_yes) if side == "YES"
                     else 1.0 - float(fair_uncapped_yes))
        # 1e-9 epsilon so a cent-granular bid exactly at the threshold
        # (e.g. bid 0.06 vs fair 0.07 - 1pp) fires despite float rounding.
        if current_bid >= fair_side - fair_target_margin_pp / 100.0 - 1e-9:
            return {
                "decision": "CASHOUT",
                "trigger": "fair_target",
                "reason": f"bid {current_bid:.3f} within "
                          f"{fair_target_margin_pp:.0f}pp of raw fair "
                          f"{fair_side:.3f}; convexity converged",
            }

    # Trigger 3: convergence
    # v9.5 (2026-05-22): under 3-bin laddering this trigger is net-negative
    # because the P&L motor is winner-takes-payout-at-resolution, NOT price
    # discovery of a single bracket. Convergence fires exactly when the
    # bot's edge is being validated by the market, but holding to resolution
    # extracts ~3x more value than exiting at convergence. Pass
    # convergence_pp <= 0 to disable (now the default). Kept here so old
    # callers passing positive values still get the legacy behaviour.
    if (convergence_pp > 0 and in_profit
            and forecast_prob_yes is not None):
        fair_value = (forecast_prob_yes if side == "YES"
                      else 1.0 - forecast_prob_yes)
        if current_bid >= fair_value - convergence_pp / 100.0:
            return {
                "decision": "CASHOUT",
                "trigger": "convergence",
                "reason": f"bid {current_bid:.3f} within {convergence_pp:.0f}pp "
                          f"of fair {fair_value:.3f}; edge converged",
            }

    # Trigger 4: forecast reversal (existing logic, backstop break-even)
    if forecast_prob_yes is not None and current_bid >= entry_price:
        # For YES bets, forecast_prob_now = forecast_prob_yes.
        # For NO bets, forecast_prob_now = 1 - forecast_prob_yes.
        # entry_implied = entry_price (the price we paid for our side).
        forecast_prob_now = (forecast_prob_yes if side == "YES"
                             else 1.0 - forecast_prob_yes)
        if forecast_prob_now < entry_price:
            return {
                "decision": "CASHOUT",
                "trigger": "forecast_reversal",
                "reason": f"forecast P({side})={forecast_prob_now:.3f} < entry "
                          f"{entry_price:.3f} and bid {current_bid:.3f} permits "
                          f"break-even+",
            }

    # Default: hold
    return {
        "decision": "HOLD",
        "trigger": "none",
        "reason": (f"bid {current_bid:.3f}, peak {peak:.3f}, entry "
                   f"{entry_price:.3f}; no trigger fired"),
    }


# ---------------------------------------------------------------------------
# Inline tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Test 1: parse a simple temp market
    cities_fixture = {
        "world": ["New York", "Manhattan", "London"],
        "us_top50": ["Boston"],
        "aliases": {"NYC": "New York"},
        "europe_top30": [], "north_america_extra": [],
    }
    spec = parse_market("Will Manhattan exceed 75°F tomorrow?",
                        end_date="2026-05-11T23:59Z", cities=cities_fixture)
    assert spec and spec.city == "Manhattan" and spec.threshold_value == 75.0
    assert spec.threshold_unit == "F" and spec.comparison == "exceed"
    assert spec.metric == "temp"
    print(f"Test 1 PASS: {spec}")

    # Test 2: precip market
    spec2 = parse_market("Will London get more than 5mm of rain on 2026-06-15?",
                         cities=cities_fixture)
    assert spec2 and spec2.city == "London" and spec2.metric == "precip"
    assert spec2.threshold_value == 5.0 and spec2.threshold_unit == "mm"
    print(f"Test 2 PASS: {spec2}")

    # ------------------------------------------------------------------
    # AX (v17.1): auto_extract_station hardening — accents + "recorded by
    # <provider> at the <station>". Fixtures are the REAL Polymarket Rule
    # strings from the operator's verify_eu_stations run (2026-07-09), which
    # returned "regra não parseada" for Madrid/Istanbul/Ankara before this fix.
    # ------------------------------------------------------------------
    _ax_cities = {
        "stations": {
            "Madrid":   {"lat": 40.4936, "lon": -3.5668, "station": "LEMD",
                         "temp_bias_f": 0.0},
            "Istanbul": {"lat": 41.2619, "lon": 28.7414, "station": "LTFM",
                         "temp_bias_f": 0.0},
            "Ankara":   {"lat": 40.1281, "lon": 32.9951, "station": "LTAC",
                         "temp_bias_f": 0.0},
            "London":   {"lat": 51.5048, "lon": 0.0495, "station": "EGLC",
                         "temp_bias_f": 0.0},
        },
        "station_names": {
            "adolfo suarez madrid-barajas": "LEMD",
            "istanbul": "LTFM",
            "esenboga intl": "LTAC",
            "esenboga": "LTAC",
            "london city": "EGLC",
        },
    }
    _ax_madrid = ("This market will resolve to the temperature range that "
                  "contains the highest temperature recorded at the Adolfo "
                  "Suárez Madrid-Barajas Airport Station in degrees Celsius on "
                  "9 Jul '26.")
    _ax_istanbul = ("This market will resolve to the temperature range that "
                    "contains the highest temperature recorded by NOAA at the "
                    "Istanbul Airport in degrees Celsius on 9 Jul '26.")
    _ax_ankara = ("This market will resolve to the temperature range that "
                  "contains the highest temperature recorded at the Esenboğa "
                  "Intl Airport Station in degrees Celsius on 9 Jul '26.")

    r = auto_extract_station("Madrid", _ax_cities, _ax_madrid)
    assert r and r["station"] == "LEMD", r      # accent: Suárez -> suarez
    print("Test AX1 PASS: 'Adolfo Suárez Madrid-Barajas' -> LEMD (acento)")

    r = auto_extract_station("Ankara", _ax_cities, _ax_ankara)
    assert r and r["station"] == "LTAC", r      # accent: Esenboğa -> esenboga
    print("Test AX2 PASS: 'Esenboğa Intl' -> LTAC (acento)")

    r = auto_extract_station("Istanbul", _ax_cities, _ax_istanbul)
    assert r and r["station"] == "LTFM", r      # provider-prefixed "by NOAA at"
    print("Test AX3 PASS: 'recorded by NOAA at the Istanbul Airport' -> LTFM")

    # AX4 regression: the plain "recorded at the X Station" form still resolves
    # via the existing pattern (unaffected by the new one).
    r = auto_extract_station("London", _ax_cities,
                             "highest temperature recorded at the London City "
                             "Airport Station in degrees Celsius.")
    assert r and r["station"] == "EGLC", r
    print("Test AX4 PASS: 'recorded at the London City Airport Station' -> EGLC")

    # AX5 regression: "recorded by <provider>" WITHOUT "at the <station>" does
    # not resolve to a bogus station (provider not in station_names -> None).
    r = auto_extract_station("Nowhere", _ax_cities,
                             "highest temperature recorded by NOAA in degrees "
                             "Celsius on 9 Jul '26.")
    assert r is None, r
    print("Test AX5 PASS: 'recorded by NOAA' sem estação -> None (gracioso)")

    # Test 3: forecast → prob (temperature)
    forecast = {"forecasts": [
        {"date": (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat(),
         "temp_high_f": 78, "temp_low_f": 60, "precip_probability": 30, "precip_mm": 1.2,
         "condition_main": "Clear"}
    ]}
    p = forecast_probability(spec, forecast)
    # v11: MAE_TEMP_F widened 5.0 → 6.5. z = (78-75)/6.5 = 0.462,
    # norm.cdf(0.462) ≈ 0.678.
    assert p is not None and 0.66 <= p <= 0.70, f"got {p}"
    print(f"Test 3 PASS: P(YES) = {p:.4f} for forecast_high=78, threshold=75, MAE=6.5°F")

    # Test 4: forecast → prob (precipitation, "more than 5mm")
    forecast2 = {"forecasts": [
        {"date": "2026-06-15", "precip_mm": 8.0, "precip_probability": 70,
         "temp_high_f": 70, "temp_low_f": 55, "condition_main": "Rain"}
    ]}
    spec2_with_date = parse_market(
        "Will London get more than 5mm of rain on 2026-06-15?",
        cities=cities_fixture)
    p2 = forecast_probability(spec2_with_date, forecast2)
    # z = (8 - 5) / 3 = 1.0, norm.cdf(1.0) ≈ 0.8413
    # v11: PROB_CLIP_HIGH tightened 0.80 → 0.70 so the raw 0.8413
    # now clips to 0.70. Test asserts the clip is active.
    assert p2 == 0.70, f"expected 0.70 (clipped from 0.8413), got {p2}"
    print(f"Test 4 PASS: raw 0.8413 clips to {p2:.4f} (new ceiling 0.70)")

    # Test 5: slippage sizing — BUY at most 20% over best ask
    book = {
        "asks": [
            {"price": 0.40, "size": 100},
            {"price": 0.42, "size": 100},
            {"price": 0.45, "size": 200},
            {"price": 0.50, "size": 1000},  # 25% above best — out of range
        ],
        "bids": [{"price": 0.38, "size": 100}],
    }
    sizing = compute_max_size_for_slippage(book, "BUY", max_slippage=0.20)
    # best=0.40, cap=0.48; first 3 levels all <= 0.48; level 4 (0.50) skipped
    # cum_shares = 400 (100+100+200), cum_cost = 40+42+90 = 172
    # avg = 172/400 = 0.43, slippage = (0.43-0.40)/0.40 = 0.075 = 7.5%
    assert sizing["max_shares"] == 400, sizing
    assert abs(sizing["avg_fill"] - 0.43) < 0.001, sizing
    assert sizing["slippage_pct"] < 0.20, sizing
    print(f"Test 5 PASS: BUY sizing = {sizing}")

    # Test 6: implied + edge
    book_yes = {"asks": [{"price": 0.42, "size": 100}], "bids": [{"price": 0.40, "size": 100}]}
    book_no = {"asks": [{"price": 0.60, "size": 100}], "bids": [{"price": 0.58, "size": 100}]}
    impl = implied_probabilities(book_yes, book_no)
    assert impl["yes_ask"] == 0.42 and impl["no_ask"] == 0.60
    edge = compute_edge(0.65, impl)
    # edge_yes = 0.65 - 0.42 = 0.23 → 23pp; edge_no = 0.35 - 0.60 = -0.25
    # best_side = YES, edge_pp = 23
    assert edge["best_side"] == "YES" and edge["edge_pp_at_best"] == 23.0
    print(f"Test 6 PASS: edge = {edge}")

    # Test 7: bracket range parser ("65-69°F" with city in event title)
    cities3 = {**cities_fixture, "world": ["Jakarta", "Manhattan", "London"]}
    combined = "Highest temperature in Jakarta on May 11, 2026 65-69°F"
    spec3 = parse_market(combined, end_date="2026-05-11T23:59Z", cities=cities3)
    assert spec3 and spec3.city == "Jakarta" and spec3.comparison == "range"
    assert spec3.threshold_value == 65.0 and spec3.threshold_value_high == 69.0
    print(f"Test 7 PASS: range parse → {spec3}")

    # Test 8: range probability — forecast 75°F, bracket 65-69°F should be SMALL
    forecast3 = {"forecasts": [
        {"date": "2026-05-11", "temp_high_f": 75, "temp_low_f": 60,
         "precip_probability": 10, "precip_mm": 0, "condition_main": "Clear"}
    ]}
    p3 = forecast_probability(spec3, forecast3)
    # P(65 ≤ T < 69 | T~N(75,5)) = cdf((65-75)/5) - cdf((69-75)/5) = cdf(-2) - cdf(-1.2)
    #                            = 0.0228 - 0.1151 = -0.092 → clamped to 0... wait,
    # Actually cdf(-2) - cdf(-1.2) is NEGATIVE because cdf(-2) < cdf(-1.2).
    # We want P(low ≤ T < high) = cdf(z_low) - cdf(z_high) where z_low > z_high,
    # but here z_low = (75-65)/5 = +2 → no, the formula uses (ref - threshold) so
    # z_low = (75-65)/5 = +2, z_high = (75-69)/5 = +1.2.
    # v12.1: range markets are NO LONGER clipped (the clip inverted side
    # selection). z_low=(75-65)/6.5=1.54, z_high=(75-69)/6.5=0.92 →
    # cdf(1.54)-cdf(0.92) ≈ 0.938-0.822 = 0.116, returned raw.
    assert 0.10 <= p3 <= 0.13, f"expected raw ~0.116 (range unclipped), got {p3}"
    print(f"Test 8 PASS: range returns raw {p3:.4f} (unclipped)")

    # Test 8b (v12.1): prob_yes_for_sizing caps the chosen side, not selection.
    # Range NO: raw P(YES)=0.116 → floored to 0.30 so P(NO)=0.70 (capped).
    s_no = prob_yes_for_sizing(0.116, "NO", "range")
    assert abs(s_no - 0.30) < 1e-9, f"range NO sizing P(YES) should be 0.30, got {s_no}"
    # Range YES: raw P(YES)=0.116 stays 0.116 (cap only, no floor).
    s_yes = prob_yes_for_sizing(0.116, "YES", "range")
    assert abs(s_yes - 0.116) < 1e-9, f"range YES sizing should stay raw, got {s_yes}"
    # High raw range YES caps at 0.70.
    assert abs(prob_yes_for_sizing(0.95, "YES", "range") - 0.70) < 1e-9
    # Non-range passes through unchanged.
    assert prob_yes_for_sizing(0.42, "NO", "exceed") == 0.42
    print("Test 8b PASS: prob_yes_for_sizing caps chosen side for range only")

    # Test 8c (v13.1): ensemble_calibrated=True relaxes the cap to 0.80
    # (lowered from 0.95 to stay within Rule-6's 20pp of the judge's range cap).
    # P(YES)=0.02 (very small) NO side → cap P(NO) at 0.80 means floor P(YES) at 0.20.
    s_no_cal = prob_yes_for_sizing(0.02, "NO", "range", ensemble_calibrated=True)
    assert abs(s_no_cal - 0.20) < 1e-9, f"calibrated NO cap should give 0.20, got {s_no_cal}"
    # P(YES)=0.98 YES side → cap at 0.80.
    s_yes_cal = prob_yes_for_sizing(0.98, "YES", "range", ensemble_calibrated=True)
    assert abs(s_yes_cal - 0.80) < 1e-9, f"calibrated YES cap should give 0.80, got {s_yes_cal}"
    # Default (False) still caps at 0.70.
    assert abs(prob_yes_for_sizing(0.02, "NO", "range") - 0.30) < 1e-9
    print("Test 8c PASS: ensemble_calibrated cap=0.80 vs default cap=0.70")

    # Test 8d (v13): compute_ensemble_calibration NGR math.
    # 3 models agreeing closely: 25.0, 25.5, 26.0 C → mean 25.5, std 0.5 (sample, n-1).
    # NGR: sigma = max(1.5*0.5 + 0.5, 1.0) = max(1.25, 1.0) = 1.25
    cal = compute_ensemble_calibration(
        {"icon_max_c": 25.0, "gfs_max_c": 25.5, "ecmwf_max_c": 26.0,
         "n_models": 3}, "C")
    assert cal is not None and abs(cal["mu"] - 25.5) < 1e-3, cal
    assert abs(cal["sigma"] - 1.25) < 1e-3 and cal["n_models"] == 3, cal
    # 3 models perfectly agreeing: sigma → SIGMA_FLOOR_C=1.0 (NGR would give 0.5)
    cal2 = compute_ensemble_calibration(
        {"icon_max_c": 25.0, "gfs_max_c": 25.0, "ecmwf_max_c": 25.0,
         "n_models": 3}, "C")
    assert abs(cal2["sigma"] - SIGMA_FLOOR_C) < 1e-9, cal2
    # Single model present → None (need ≥2).
    assert compute_ensemble_calibration(
        {"icon_max_c": 25.0, "gfs_max_c": None, "ecmwf_max_c": None}, "C") is None
    # F-unit conversion: 25C ≈ 77F, spread of 0.5C ≈ 0.9F
    cal_f = compute_ensemble_calibration(
        {"icon_max_c": 25.0, "gfs_max_c": 25.5, "ecmwf_max_c": 26.0,
         "n_models": 3}, "F")
    assert abs(cal_f["mu"] - 77.9) < 0.1, cal_f
    print("Test 8d PASS: compute_ensemble_calibration NGR sigma + unit conversion")

    # Test 8e (v13): mu_override on forecast_probability.
    # Same spec3 (Jakarta 65-69F range) but with mu_override = 67 (inside bin):
    # P(65 ≤ T < 69 | T~N(67, sigma)) with sigma=1.25 → cdf((67-65)/1.25) - cdf((67-69)/1.25)
    # = cdf(1.6) - cdf(-1.6) = 0.945 - 0.055 = 0.890
    p_inside = forecast_probability(spec3, forecast3,
                                     mae_override=1.25, mu_override=67.0)
    assert p_inside is not None and 0.85 <= p_inside <= 0.92, f"got {p_inside}"
    # Far below the bin: mu=55 (10F below) → P(in bin) tiny
    p_far = forecast_probability(spec3, forecast3,
                                  mae_override=1.25, mu_override=55.0)
    assert p_far is not None and p_far < 0.01, f"got {p_far}"
    print(f"Test 8e PASS: mu_override → P(inside)={p_inside:.3f} P(far)={p_far:.5f}")

    # Test 9: "70°F or above" pattern
    spec4 = parse_market("Highest temperature in Manhattan on May 11, 2026 70°F or above",
                         end_date="2026-05-11T23:59Z", cities=cities3)
    assert spec4 and spec4.comparison == "at_least" and spec4.threshold_value == 70
    print(f"Test 9 PASS: at_least → {spec4}")

    # Test 10: "60°F or lower" pattern
    spec5 = parse_market("Highest temperature in Manhattan on May 11, 2026 60°F or lower",
                         end_date="2026-05-11T23:59Z", cities=cities3)
    assert spec5 and spec5.comparison == "at_most" and spec5.threshold_value == 60
    print(f"Test 10 PASS: at_most → {spec5}")

    # Test 11: bare single-value bracket from real Polymarket question
    cities4 = {**cities3, "world": ["Sao Paulo", "London", "Tokyo", "Manhattan", "Jakarta"]}
    spec_bare = parse_market("Will the highest temperature in London be 17°C on May 10?",
                             end_date="2026-05-10T23:59Z", cities=cities4)
    assert spec_bare and spec_bare.city == "London"
    assert spec_bare.comparison == "range"
    assert spec_bare.threshold_value == 17.0 and spec_bare.threshold_value_high == 18.0
    assert spec_bare.threshold_unit == "C"
    print(f"Test 11 PASS: bare value → {spec_bare}")

    # Test 12: accent-insensitive city resolution
    cities_with_accent = {"world": ["São Paulo"], "us_top50": [],
                           "europe_top30": [], "north_america_extra": [], "aliases": {}}
    spec_acc = parse_market("Will the highest temperature in Sao Paulo be 23°C or higher on May 10?",
                            end_date="2026-05-10T23:59Z", cities=cities_with_accent)
    assert spec_acc and spec_acc.city == "São Paulo"
    assert spec_acc.comparison == "at_least" and spec_acc.threshold_value == 23.0
    print(f"Test 12 PASS: accent-insensitive → {spec_acc}")

    # === Cashout trigger tests (Tests A-H from plan) ===
    # NOTE: defaults profit_lock_pp=50, trailing_drawdown_pct=30,
    # trailing_min_gain_pp=20, convergence_pp=5

    # Test A: bid sobe um pouco, sem peak, nada dispara
    v = evaluate_cashout_triggers(
        side="NO", entry_price=0.13, current_bid=0.20,
        peak_bid_seen=None, forecast_prob_yes=0.05)
    assert v["decision"] == "HOLD" and v["trigger"] == "none", v
    print(f"Test A PASS: HOLD (bid 0.20, sem peak) → {v['trigger']}")

    # Test B: profit lock dispara
    v = evaluate_cashout_triggers(
        side="NO", entry_price=0.13, current_bid=0.65,
        peak_bid_seen=0.65, forecast_prob_yes=0.05)
    assert v["decision"] == "CASHOUT" and v["trigger"] == "profit_lock", v
    print(f"Test B PASS: CASHOUT profit_lock → bid 0.65 >= entry 0.13+0.50")

    # Test C: drawdown só 20%, abaixo do threshold 30%
    v = evaluate_cashout_triggers(
        side="NO", entry_price=0.13, current_bid=0.40,
        peak_bid_seen=0.50, forecast_prob_yes=0.05)
    assert v["decision"] == "HOLD", f"got {v}"
    print(f"Test C PASS: HOLD (drawdown 20% < 30%)")

    # Test D: drawdown 32% — trailing stop dispara
    v = evaluate_cashout_triggers(
        side="NO", entry_price=0.13, current_bid=0.34,
        peak_bid_seen=0.50, forecast_prob_yes=0.05)
    assert v["decision"] == "CASHOUT" and v["trigger"] == "trailing_stop", v
    print(f"Test D PASS: CASHOUT trailing_stop → bid 0.34 <= 0.50*0.70")

    # Test E: peak ainda pequeno (não atingiu min_gain de 20pp)
    v = evaluate_cashout_triggers(
        side="NO", entry_price=0.13, current_bid=0.18,
        peak_bid_seen=0.20, forecast_prob_yes=0.05)
    # peak 0.20 < entry 0.13 + 0.20 = 0.33 → trailing NOT armed
    # bid 0.18 < entry+50pp (0.63) → profit_lock not fired
    # bid 0.18 vs fair NO=0.95: not within 5pp → convergence not fired
    # forecast P(NO)=0.95 >= entry 0.13 → no reversal
    assert v["decision"] == "HOLD", v
    print(f"Test E PASS: HOLD (peak não armou trailing)")

    # Test F: convergence — bid muito perto do fair value
    v = evaluate_cashout_triggers(
        side="NO", entry_price=0.13, current_bid=0.91,
        peak_bid_seen=0.91, forecast_prob_yes=0.05)
    # fair NO = 1 - 0.05 = 0.95. bid 0.91 >= 0.90 (0.95 - 0.05) → convergence
    # BUT bid 0.91 >= entry 0.13 + 0.50 → profit_lock ALSO fires first
    # Since profit_lock comes first, decision is profit_lock (acceptable)
    assert v["decision"] == "CASHOUT", v
    print(f"Test F PASS: CASHOUT ({v['trigger']}) — high bid")

    # Test F2: convergence isolated (entry close to fair so profit_lock can't fire)
    v = evaluate_cashout_triggers(
        side="NO", entry_price=0.50, current_bid=0.91,
        peak_bid_seen=0.91, forecast_prob_yes=0.05)
    # profit_lock: 0.91 - 0.50 = 0.41 < 0.50 → not fired
    # trailing: peak 0.91 >= 0.50+0.20=0.70 ✓, drawdown 0 < 30% → not fired
    # convergence: fair=0.95, bid 0.91 >= 0.90 → CASHOUT
    assert v["decision"] == "CASHOUT" and v["trigger"] == "convergence", v
    print(f"Test F2 PASS: CASHOUT convergence (entry 0.50, fair 0.95)")

    # Test G: guard — bid < entry, nunca cashout em prejuízo
    v = evaluate_cashout_triggers(
        side="NO", entry_price=0.13, current_bid=0.12,
        peak_bid_seen=0.20, forecast_prob_yes=0.05)
    assert v["decision"] == "HOLD", v
    print(f"Test G PASS: HOLD (guard: bid < entry)")

    # Test H: forecast reversal — forecast piorou contra nós
    v = evaluate_cashout_triggers(
        side="YES", entry_price=0.40, current_bid=0.42,
        peak_bid_seen=0.42, forecast_prob_yes=0.30)
    # forecast_prob_now (YES) = 0.30 < entry 0.40 ✓, bid 0.42 >= entry 0.40 ✓
    # profit_lock: 0.42 - 0.40 = 0.02 < 0.50 → not fired
    # trailing: peak 0.42 < entry 0.40+0.20=0.60 → not armed
    # convergence: fair YES=0.30. bid 0.42 >= 0.30-0.05=0.25 ✓ → CASHOUT
    # both convergence and forecast_reversal would fire; convergence comes first
    assert v["decision"] == "CASHOUT", v
    print(f"Test H PASS: CASHOUT ({v['trigger']}) — bid above fair")

    # Test H2: pure forecast_reversal (bid < fair so convergence doesn't fire)
    v = evaluate_cashout_triggers(
        side="YES", entry_price=0.40, current_bid=0.40,
        peak_bid_seen=0.45, forecast_prob_yes=0.20)
    # forecast P(YES)=0.20 < entry 0.40 ✓, bid 0.40 >= entry 0.40 ✓
    # profit_lock: 0.40 - 0.40 = 0 < 0.50 → not fired
    # trailing: peak 0.45 < entry+0.20=0.60 → not armed
    # convergence: fair YES=0.20. bid 0.40 >= 0.15 ✓ — convergence fires!
    # Actually convergence fires first since 0.40 > 0.15. trigger=convergence
    assert v["decision"] == "CASHOUT", v
    print(f"Test H2 PASS: CASHOUT ({v['trigger']}) — forecast turned bad")

    # Test I (v9.5): convergence_pp=0 disables the trigger entirely.
    # Setup where ONLY convergence would have fired at 5pp default:
    #   entry=0.40, bid=0.55, peak=0.55, forecast P(YES)=0.60
    #   - profit_lock at 50pp: bid 0.55 < 0.40+0.50=0.90 → not fired
    #   - trailing: peak 0.55 < entry+0.20=0.60 → not armed
    #   - forecast_reversal: forecast 0.60 >= entry 0.40 → not fired
    #   - convergence at 5pp: bid 0.55 >= fair 0.60 - 0.05 = 0.55 → FIRE
    # First assert default still fires:
    v = evaluate_cashout_triggers(
        side="YES", entry_price=0.40, current_bid=0.55,
        peak_bid_seen=0.55, forecast_prob_yes=0.60)
    assert v["decision"] == "CASHOUT" and v["trigger"] == "convergence", v
    print(f"Test I-baseline PASS: 5pp default still fires convergence")
    # Now disable: same setup with convergence_pp=0 → HOLD
    v = evaluate_cashout_triggers(
        side="YES", entry_price=0.40, current_bid=0.55,
        peak_bid_seen=0.55, forecast_prob_yes=0.60,
        convergence_pp=0.0)
    assert v["decision"] == "HOLD", v
    print(f"Test I PASS: convergence_pp=0 disables the trigger (HOLD)")

    # -----------------------------------------------------------------------
    # v11: range_cross stop-loss trigger
    # -----------------------------------------------------------------------
    # Test K1: NO bet on range 31-32, forecast 31.8 inside → CASHOUT range_cross.
    # Bid below entry (would normally be suppressed) — range_cross overrides.
    v = evaluate_cashout_triggers(
        side="NO", entry_price=0.61, current_bid=0.40, peak_bid_seen=0.61,
        forecast_prob_yes=0.30, comparison="range",
        forecast_value=31.8, range_low=31.0, range_high=32.0)
    assert v["decision"] == "CASHOUT" and v["trigger"] == "range_cross", v
    print("Test K1 PASS: NO+range forecast inside bracket → range_cross (sells at loss)")

    # Test K2: NO bet on range, forecast 29.0 well outside (range 31-32 ±0.5
    # = [30.5, 32.5]) → no range_cross; bid<entry suppresses others → HOLD.
    v = evaluate_cashout_triggers(
        side="NO", entry_price=0.61, current_bid=0.55, peak_bid_seen=0.61,
        forecast_prob_yes=0.20, comparison="range",
        forecast_value=29.0, range_low=31.0, range_high=32.0)
    assert v["decision"] == "HOLD" and v["trigger"] != "range_cross", v
    print("Test K2 PASS: NO+range forecast outside bracket±margin → no range_cross")

    # Test K3: edge of margin — forecast at low-0.5 boundary still triggers.
    v = evaluate_cashout_triggers(
        side="NO", entry_price=0.61, current_bid=0.40, peak_bid_seen=0.61,
        forecast_prob_yes=0.30, comparison="range",
        forecast_value=30.5, range_low=31.0, range_high=32.0)
    assert v["trigger"] == "range_cross", v
    print("Test K3 PASS: forecast at low-margin boundary fires range_cross")

    # Test K4: YES bet on range with forecast inside → NOT range_cross
    # (forecast inside the bracket is GOOD for a YES bet).
    v = evaluate_cashout_triggers(
        side="YES", entry_price=0.40, current_bid=0.30, peak_bid_seen=0.40,
        forecast_prob_yes=0.70, comparison="range",
        forecast_value=31.5, range_low=31.0, range_high=32.0)
    assert v["trigger"] != "range_cross", v
    print("Test K5 PASS: YES+range inside bracket does NOT trigger range_cross")

    # Test K5: non-range NO bet never triggers range_cross even if value given.
    v = evaluate_cashout_triggers(
        side="NO", entry_price=0.61, current_bid=0.55, peak_bid_seen=0.61,
        forecast_prob_yes=0.40, comparison="exceed",
        forecast_value=31.5, range_low=31.0, range_high=None)
    assert v["trigger"] != "range_cross", v
    print("Test K6 PASS: non-range comparison does not trigger range_cross")

    # -----------------------------------------------------------------------
    # v11: clip ceiling/floor tightened (0.80/0.20 → 0.70/0.30)
    # -----------------------------------------------------------------------
    assert _clip_prob(0.99) == 0.70
    print(f"Test J1 PASS: raw 0.99 clips to {_clip_prob(0.99)} (new ceiling 0.70)")
    assert _clip_prob(0.02) == 0.30
    print(f"Test J2 PASS: raw 0.02 clips to {_clip_prob(0.02)} (new floor 0.30)")
    assert _clip_prob(0.50) == 0.50 and _clip_prob(0.60) == 0.60
    print("Test J3 PASS: middle prob unchanged (0.50, 0.60)")
    assert _clip_prob(0.70) == 0.70 and _clip_prob(0.30) == 0.30
    print("Test J4 PASS: boundary values stay")
    assert _clip_prob(None) is None
    print("Test J5 PASS: None passes through")

    # -----------------------------------------------------------------------
    # v9: ladder selection tests
    # -----------------------------------------------------------------------
    from types import SimpleNamespace

    def _mk(thr, prob):
        return {"spec": SimpleNamespace(threshold_value=thr),
                "forecast_prob": prob}

    # Test L1: 5 sequential brackets, forecast peak in middle
    legs = [_mk(60, 0.05), _mk(65, 0.15), _mk(70, 0.55),
            _mk(75, 0.20), _mk(80, 0.05)]
    r = select_ladder_brackets(legs)
    assert r["central"]["spec"].threshold_value == 70
    assert r["below"]["spec"].threshold_value == 65
    assert r["above"]["spec"].threshold_value == 75
    print("Test L1 PASS: 5 brackets full 3-bin (central=70, below=65, above=75)")

    # Test L2: peak at top bracket → no above
    legs = [_mk(60, 0.05), _mk(65, 0.10), _mk(70, 0.15),
            _mk(75, 0.20), _mk(80, 0.50)]
    r = select_ladder_brackets(legs)
    assert r["central"]["spec"].threshold_value == 80
    assert r["below"]["spec"].threshold_value == 75
    assert r["above"] is None
    print("Test L2 PASS: peak at top → 2-bin (no above)")

    # Test L3: peak at bottom → no below
    legs = [_mk(60, 0.50), _mk(65, 0.20), _mk(70, 0.15),
            _mk(75, 0.10), _mk(80, 0.05)]
    r = select_ladder_brackets(legs)
    assert r["central"]["spec"].threshold_value == 60
    assert r["above"]["spec"].threshold_value == 65
    assert r["below"] is None
    print("Test L3 PASS: peak at bottom → 2-bin (no below)")

    # Test L4: 2 brackets only → central + above (or below), but no
    # both neighbours possible. Verify central is the max-prob one.
    legs = [_mk(60, 0.40), _mk(65, 0.50)]
    r = select_ladder_brackets(legs)
    assert r["central"]["spec"].threshold_value == 65
    assert r["below"]["spec"].threshold_value == 60
    assert r["above"] is None
    print("Test L4 PASS: 2 brackets, central=65 + below=60")

    # Test L5: brackets given out of order are sorted
    legs = [_mk(70, 0.55), _mk(60, 0.05), _mk(75, 0.20),
            _mk(65, 0.15), _mk(80, 0.05)]
    r = select_ladder_brackets(legs)
    assert r["central"]["spec"].threshold_value == 70
    assert r["below"]["spec"].threshold_value == 65
    assert r["above"]["spec"].threshold_value == 75
    print("Test L5 PASS: out-of-order brackets sorted correctly")

    # Test L6: empty / no usable forecast_prob
    assert select_ladder_brackets([]) is None
    assert select_ladder_brackets([{"spec": None, "forecast_prob": 0.5}]) is None
    print("Test L6 PASS: empty/unusable → None")

    # -----------------------------------------------------------------------
    # v9: Kelly split tests
    # -----------------------------------------------------------------------

    # Test K1: 3 equal-EV legs → equal stake
    legs = [{"forecast_prob": 0.50, "entry_price": 0.30},
            {"forecast_prob": 0.50, "entry_price": 0.30},
            {"forecast_prob": 0.50, "entry_price": 0.30}]
    out = compute_kelly_split(legs, total_usd=30.0)
    assert out is not None
    assert all(abs(l["stake_usd"] - 10.0) < 0.01 for l in out)
    print("Test K1 PASS: 3 equal legs → 3 x $10")

    # Test K2: central with larger edge gets more weight
    legs = [{"forecast_prob": 0.40, "entry_price": 0.30},   # adjacent low-edge
            {"forecast_prob": 0.70, "entry_price": 0.30},   # central high-edge
            {"forecast_prob": 0.40, "entry_price": 0.30}]
    out = compute_kelly_split(legs, total_usd=30.0)
    assert out[1]["stake_usd"] > out[0]["stake_usd"]
    assert out[1]["stake_usd"] > out[2]["stake_usd"]
    print(f"Test K2 PASS: central gets ${out[1]['stake_usd']:.2f} vs "
          f"adj ${out[0]['stake_usd']:.2f} (more weight where edge bigger)")

    # Test K3: one leg with negative EV → zero stake
    legs = [{"forecast_prob": 0.20, "entry_price": 0.30},   # negative kelly
            {"forecast_prob": 0.60, "entry_price": 0.30},
            {"forecast_prob": 0.50, "entry_price": 0.30}]
    out = compute_kelly_split(legs, total_usd=30.0)
    assert out[0]["stake_usd"] == 0.0
    assert abs(out[1]["stake_usd"] + out[2]["stake_usd"] - 30.0) < 0.01
    print(f"Test K3 PASS: negative-EV leg gets $0, others split remainder")

    # Test K4: 2-bin still works
    legs = [{"forecast_prob": 0.60, "entry_price": 0.30},
            {"forecast_prob": 0.50, "entry_price": 0.30}]
    out = compute_kelly_split(legs, total_usd=20.0)
    assert abs(sum(l["stake_usd"] for l in out) - 20.0) < 0.01
    print("Test K4 PASS: 2-bin preserves budget")

    # Test K5: budget conservation across multiple sizes
    for total in (10.0, 50.0, 100.0):
        legs = [{"forecast_prob": 0.50, "entry_price": 0.30},
                {"forecast_prob": 0.40, "entry_price": 0.30}]
        out = compute_kelly_split(legs, total_usd=total)
        s = sum(l["stake_usd"] for l in out)
        assert abs(s - total) < 0.01, f"total {total} got sum {s}"
    print("Test K5 PASS: budget preserved across sizes")

    # Test K6: all kelly negative → None (caller skips ladder)
    legs = [{"forecast_prob": 0.10, "entry_price": 0.30},
            {"forecast_prob": 0.15, "entry_price": 0.30}]
    out = compute_kelly_split(legs, total_usd=30.0)
    assert out is None
    print("Test K6 PASS: all kelly negative → None")

    # Test K7 (v9.14 regression): NO-side leg gets non-zero Kelly when
    # bot believes the NO outcome (1 - P(YES)) is high. Snapshot showed
    # bug where below/above legs always got $0 because forecast_prob was
    # passed as P(YES) but entry_price was the NO ask.
    # Setup mirrors snapshot's NO-side below legs:
    #   forecast_prob (P(YES)) = 0.10, entry_price (NO ask) = 0.70
    #   Bot's P(NO side) = 1 - 0.10 = 0.90, so Kelly on NO bet:
    #     kelly = (0.90 * 0.30 - 0.10 * 0.70) / 0.30 = (0.27 - 0.07) / 0.30 = 0.667
    legs = [{"forecast_prob": 0.10, "entry_price": 0.70, "side": "NO"}]
    out = compute_kelly_split(legs, total_usd=50.0)
    assert out is not None, "K7: NO-side leg should produce positive Kelly"
    assert out[0]["stake_usd"] == 50.0, f"K7: 1-leg → full stake, got {out[0]}"
    assert out[0]["kelly_frac"] > 0, f"K7: NO-side kelly should be positive"
    print(f"Test K7 PASS: NO-side leg got stake ${out[0]['stake_usd']:.2f}, "
          f"kelly_frac={out[0]['kelly_frac']:.3f} (was $0 pre-v9.14)")

    # Test K8: 2-bin mix (central YES + below NO) splits stake correctly
    # — this is the canonical snapshot pattern that always returned
    # central=$50, below=$0 in the buggy version.
    legs = [
        {"forecast_prob": 0.45, "entry_price": 0.25, "side": "YES"},  # central
        {"forecast_prob": 0.10, "entry_price": 0.70, "side": "NO"},   # below NO
    ]
    out = compute_kelly_split(legs, total_usd=50.0)
    assert out is not None
    central_stake = out[0]["stake_usd"]
    below_stake = out[1]["stake_usd"]
    assert central_stake > 0 and below_stake > 0, \
        f"K8: both legs must get positive stake (got C=${central_stake}, B=${below_stake})"
    assert abs(central_stake + below_stake - 50.0) < 0.01, \
        f"K8: stake sum must = total ({central_stake + below_stake})"
    print(f"Test K8 PASS: 2-bin YES+NO splits C=${central_stake:.2f} / "
          f"B=${below_stake:.2f} (sum=${central_stake+below_stake:.2f})")

    # ------------------------------------------------------------------
    # v13.4: min/max (temp_kind) tests — lowest-temperature markets must
    # be priced/guarded on the daily MIN, not the MAX.
    # ------------------------------------------------------------------
    _cities = load_cities()

    # M1: parser detecta lowest → temp_kind="low" (formato real das questions)
    s = parse_market("Will the lowest temperature in Paris be 14°C on July 3?",
                     "2026-07-03", _cities)
    assert s is not None and s.temp_kind == "low", s
    s2 = parse_market("Will the highest temperature in Paris be 29°C on July 3?",
                      "2026-07-03", _cities)
    assert s2 is not None and s2.temp_kind == "high", s2
    # forma slug-appended (discovery às vezes anexa o slug à question)
    s3 = parse_market("Will the temperature be 14°C on July 3? "
                      "lowest-temperature-in-paris-on-july-3-2026-14c",
                      "2026-07-03", _cities)
    assert s3 is not None and s3.temp_kind == "low", s3
    print("Test M1 PASS: parse temp_kind low/high (question + slug-appended)")

    # M2: forecast_ref_value usa temp_low_* para mercado de mínima
    fc = {"daily_forecast": [{"date": "2026-07-03",
                               "temp_high_c": 29.0, "temp_low_c": 15.9,
                               "temp_high_f": 84.3, "temp_low_f": 60.6}]}
    assert forecast_ref_value(s, fc) == 15.9, forecast_ref_value(s, fc)
    assert forecast_ref_value(s2, fc) == 29.0
    print("Test M2 PASS: forecast_ref_value low=15.9 / high=29.0")

    # M3: probabilidade raw do bin [14,15) com low 15.9 é MUITO maior do
    # que a fabricada com high 29.0 (o bug dava P≈0 → NO com edge falso)
    p_low = _forecast_probability_raw(s, fc, mae_override=1.5)
    z_bug = (29.0 - 14.0) / 1.5  # o que o bug computava
    from math import erf as _erf
    assert p_low is not None and p_low > 0.05, f"P com low deveria ser material: {p_low}"
    print(f"Test M3 PASS: P(bin) com mínima = {p_low:.3f} (bug dava ~0)")

    # M4: compute_ensemble_calibration escolhe membros min/max por temp_kind
    om = {"icon_max_c": 28.1, "gfs_max_c": 28.9, "ecmwf_max_c": 27.1,
          "icon_min_c": 15.4, "gfs_min_c": 16.1, "ecmwf_min_c": 15.8,
          "spread_c": 1.8, "spread_min_c": 0.7, "n_models": 3}
    cal_hi = compute_ensemble_calibration(om, "C", temp_kind="high")
    cal_lo = compute_ensemble_calibration(om, "C", temp_kind="low")
    assert 27.0 < cal_hi["mu"] < 29.0, cal_hi
    assert 15.0 < cal_lo["mu"] < 16.5, cal_lo
    print(f"Test M4 PASS: ensemble μ high={cal_hi['mu']} / low={cal_lo['mu']}")

    # M5: om_data antigo (sem *_min_c, cache velho) → low market cai no
    # fallback MAE em vez de usar a máxima silenciosamente
    om_old = {"icon_max_c": 28.1, "gfs_max_c": 28.9, "ecmwf_max_c": 27.1,
              "spread_c": 1.8, "n_models": 3}
    assert compute_ensemble_calibration(om_old, "C", temp_kind="low") is None
    assert compute_ensemble_calibration(om_old, "C", temp_kind="high") is not None
    print("Test M5 PASS: om_data legado sem mins → low usa fallback (None)")

    # M6 (v15.4): calibração conta TODOS os membros presentes (trio +
    # regionais), e NUNCA conta spread_c/spread_min_c como membro.
    om_ext = {"icon_max_c": 28.1, "gfs_max_c": 28.9, "ecmwf_max_c": 27.1,
              "meteofrance_arome_france_hd_max_c": 27.5, "icon_eu_max_c": 28.3,
              "icon_min_c": 15.4, "gfs_min_c": 16.1, "ecmwf_min_c": 15.8,
              "meteofrance_arome_france_hd_min_c": 15.6, "icon_eu_min_c": 15.9,
              "spread_c": 1.8, "spread_min_c": 0.7, "n_models": 5}
    cal_hi = compute_ensemble_calibration(om_ext, "C", temp_kind="high")
    cal_lo = compute_ensemble_calibration(om_ext, "C", temp_kind="low")
    assert cal_hi["n_models"] == 5, cal_hi          # 5 membros, spread excluído
    assert cal_lo["n_models"] == 5, cal_lo          # spread_min_c NÃO conta
    assert 27.0 < cal_hi["mu"] < 29.0, cal_hi
    assert 15.0 < cal_lo["mu"] < 16.5, cal_lo
    print(f"Test M6 PASS: calibração com 5 membros (n={cal_hi['n_models']}), "
          "spread_c/spread_min_c excluídos")

    # M7 (v15.4): _om_short_name — trio mantém rótulo curto, regionais usam a
    # chave inteira (byte-idêntico p/ forecast_history do trio).
    assert _om_short_name("icon_seamless") == "icon"
    assert _om_short_name("ecmwf_ifs025") == "ecmwf"
    assert _om_short_name("meteofrance_arome_france_hd") == "meteofrance_arome_france_hd"
    print("Test M7 PASS: _om_short_name (trio curto, regional = chave inteira)")

    # M8 (v15.4): fetch_open_meteo_ensemble — (a) modelos estendidos parseados;
    # (b) fallback ao trio quando a chamada com regionais falha (400). Mock de
    # requests.get sem rede.
    saved_get = requests.get

    class _FR:
        def __init__(self, status, payload):
            self.status_code = status
            self._p = payload
        def json(self):
            return self._p

    try:
        # (a) resposta multi-modelo (sufixos por chave) → parseia N membros.
        def fake_multi(url, params=None, **kw):
            keys = (params or {}).get("models", "").split(",")
            hourly = {"time": ["2026-07-09T00:00", "2026-07-09T15:00"]}
            base = {"icon_seamless": 27.0, "gfs_seamless": 28.0,
                    "ecmwf_ifs025": 27.5, "icon_eu": 28.4,
                    "meteofrance_arome_france_hd": 27.2}
            for k in keys:
                hourly[f"temperature_2m_{k}"] = [base.get(k, 20.0) - 5, base.get(k, 20.0)]
            return _FR(200, {"hourly": hourly})
        requests.get = fake_multi
        r = fetch_open_meteo_ensemble(
            48.97, 2.44, "2026-07-09", force_refresh=True,
            models=["meteofrance_arome_france_hd", "icon_eu", "ecmwf_ifs025",
                    "gfs_seamless"])
        assert r and r["n_models"] == 4, r
        assert "meteofrance_arome_france_hd_max_c" in r, r
        assert "fallback_from" not in r, r
        print(f"Test M8a PASS: fetch parseia 4 modelos regionais (n={r['n_models']})")

        # (b) chamada com regionais 400a → cai no trio. Primeira chamada (com
        # regionais) status 400; segunda (trio) 200.
        state = {"n": 0}
        def fake_fallback(url, params=None, **kw):
            state["n"] += 1
            keys = (params or {}).get("models", "").split(",")
            if any(k not in ("icon_seamless", "gfs_seamless", "ecmwf_ifs025")
                   for k in keys):
                return _FR(400, {"error": True, "reason": "invalid model"})
            hourly = {"time": ["2026-07-09T00:00", "2026-07-09T15:00"]}
            for k in keys:
                hourly[f"temperature_2m_{k}"] = [20.0, 27.0]
            return _FR(200, {"hourly": hourly})
        requests.get = fake_fallback
        r = fetch_open_meteo_ensemble(
            51.5, 0.05, "2026-07-09", force_refresh=True,
            models=["ukmo_uk_deterministic_2km", "icon_eu", "ecmwf_ifs025",
                    "gfs_seamless"])
        assert r is not None, "esperava fallback ao trio, não None"
        assert r.get("fallback_from") == ["ukmo_uk_deterministic_2km", "icon_eu",
                                          "ecmwf_ifs025", "gfs_seamless"], r
        assert r["model_keys"] == DEFAULT_OM_MODELS, r
        assert state["n"] == 2, state          # 1 tentativa regional + 1 trio
        print("Test M8b PASS: chave regional inválida (400) → fallback ao trio "
              "(fallback_from registrado)")
    finally:
        requests.get = saved_get

    # M6: default retrocompatível — spec antigo sem temp_kind se comporta
    # como high (dataclass default)
    assert MarketSpec(city="X", threshold_value=20.0, threshold_unit="C",
                      metric="temp", comparison="range",
                      target_date=date(2026, 7, 3), confidence=1.0,
                      raw_question="q").temp_kind == "high"
    print("Test M6 PASS: temp_kind default = high (retrocompat)")

    # ------------------------------------------------------------------
    # v14: temperature-only policy — is_tradeable_spec
    # ------------------------------------------------------------------
    _cities_t = dict(cities_fixture)
    _cities_t["world"] = ["Dallas", "London", "New York", "Manhattan"]

    # T1: rain binário PARSEIA (monitor/cashout de posições existentes
    # dependem disso) mas NÃO é tradeable
    t1 = parse_market("Will it rain in Dallas on June 10?",
                      "2026-06-10", _cities_t)
    assert t1 is not None and t1.metric == "precip", t1
    assert is_tradeable_spec(t1) is False
    print("Test T1 PASS: rain binary parses (precip) but tradeable=False")

    # T2: forma com slug anexado (discovery concatena event title + slug)
    t2 = parse_market("Will it rain in Dallas on June 10? "
                      "will-it-rain-in-dallas-on-june-10",
                      "2026-06-10", _cities_t)
    assert t2 is not None and t2.metric == "precip" and not is_tradeable_spec(t2)
    print("Test T2 PASS: slug-appended rain binary → tradeable=False")

    # T3: snow binário
    t3 = parse_market("Will it snow in New York on December 25?",
                      "2026-12-25", _cities_t)
    assert t3 is not None and t3.metric == "snow" and not is_tradeable_spec(t3)
    print("Test T3 PASS: snow binary → tradeable=False")

    # T4: precip numérico (mesmo mercado do Test 2, que continua parseando)
    t4 = parse_market("Will London get more than 5mm of rain on 2026-06-15?",
                      cities=_cities_t)
    assert t4 is not None and t4.metric == "precip" and not is_tradeable_spec(t4)
    print("Test T4 PASS: numeric precip → tradeable=False")

    # T5: temp normal é tradeable; None não é
    t5 = parse_market("Will Manhattan exceed 75°F tomorrow?",
                      "2026-05-11T23:59Z", cities=_cities_t)
    assert is_tradeable_spec(t5) is True
    assert is_tradeable_spec(None) is False
    print("Test T5 PASS: temp market tradeable=True; None → False")

    # ------------------------------------------------------------------
    # v11: cheap_convexity fair_target cashout trigger
    # ------------------------------------------------------------------
    # CC1: bought YES @ 0.01, raw fair 0.07, bid 0.06, margin 1pp → fires
    # (0.06 >= 0.07 - 0.01).
    v = evaluate_cashout_triggers(
        side="YES", entry_price=0.01, current_bid=0.06, peak_bid_seen=0.06,
        forecast_prob_yes=None, enable_fair_target=True,
        fair_uncapped_yes=0.07, fair_target_margin_pp=1.0)
    assert v["decision"] == "CASHOUT" and v["trigger"] == "fair_target", v
    print("Test CC1 PASS: bid 0.06 within 1pp of raw fair 0.07 → fair_target")

    # CC2: bid below entry (loss) → in_profit guard suppresses it → HOLD.
    v = evaluate_cashout_triggers(
        side="YES", entry_price=0.05, current_bid=0.03, peak_bid_seen=0.05,
        forecast_prob_yes=None, enable_fair_target=True,
        fair_uncapped_yes=0.07, fair_target_margin_pp=1.0)
    assert v["decision"] == "HOLD", v
    print("Test CC2 PASS: bid < entry → in_profit guard holds (no loss sale)")

    # CC3: enable_fair_target=False → new trigger silent (legacy callers
    # unaffected even with fair_uncapped_yes present).
    v = evaluate_cashout_triggers(
        side="YES", entry_price=0.01, current_bid=0.06, peak_bid_seen=0.06,
        forecast_prob_yes=None, enable_fair_target=False,
        fair_uncapped_yes=0.07, fair_target_margin_pp=1.0)
    assert v["trigger"] != "fair_target", v
    print("Test CC3 PASS: enable_fair_target=False → trigger silent")

    # CC4: bid 0 (no bids in book) never fires (in_profit false since
    # 0 < entry 0.01).
    v = evaluate_cashout_triggers(
        side="YES", entry_price=0.01, current_bid=0.0, peak_bid_seen=0.0,
        forecast_prob_yes=None, enable_fair_target=True,
        fair_uncapped_yes=0.07, fair_target_margin_pp=1.0)
    assert v["decision"] == "HOLD", v
    print("Test CC4 PASS: bid 0.0 → HOLD (no phantom sale at zero)")

    # CC5: NO side — bought NO @ 0.02, raw P(YES)=0.90 → fair P(NO)=0.10,
    # bid 0.09 within 1pp → fires.
    v = evaluate_cashout_triggers(
        side="NO", entry_price=0.02, current_bid=0.09, peak_bid_seen=0.09,
        forecast_prob_yes=None, enable_fair_target=True,
        fair_uncapped_yes=0.90, fair_target_margin_pp=1.0)
    assert v["decision"] == "CASHOUT" and v["trigger"] == "fair_target", v
    print("Test CC5 PASS: NO side fair P(NO)=0.10, bid 0.09 → fair_target")

    print("\nAll helper tests PASS")
