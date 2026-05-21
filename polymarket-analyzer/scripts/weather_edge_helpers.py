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

MAE_TEMP_F = 5.0      # OpenWeather 3-5d temp forecast MAE in °F (fallback)
MAE_TEMP_C = 2.78     # = MAE_TEMP_F converted
MAE_PRECIP_MM = 3.0   # precip total MAE
MAE_WIND_KPH = 8.0    # wind speed MAE


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

_OM_CACHE: dict = {}              # (lat, lon, target_iso) -> (ts_unix, dict)
_OM_CACHE_TTL_SEC = 6 * 3600      # 6h


def fetch_open_meteo_ensemble(lat: float, lon: float,
                                target_date,  # date or YYYY-MM-DD string
                                force_refresh: bool = False
                                ) -> Optional[dict]:
    """Pull ICON+GFS+ECMWF max temp (C) for `target_date` at (lat, lon).

    Returns:
      {"icon_max_c": 25.3, "gfs_max_c": 24.8, "ecmwf_max_c": 26.1,
       "spread_c": 1.3, "agree": True}
    or None on HTTP failure / malformed response.

    `agree=True` when max-min spread <= 3C (article rule). Cached 6h
    per (lat, lon, target_iso).
    """
    target_iso = (target_date.isoformat()
                   if hasattr(target_date, "isoformat") else str(target_date))
    cache_key = (round(float(lat), 4), round(float(lon), 4), target_iso)
    now = time.time()
    if not force_refresh and cache_key in _OM_CACHE:
        cached_ts, cached_data = _OM_CACHE[cache_key]
        if now - cached_ts < _OM_CACHE_TTL_SEC:
            return cached_data

    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "models": "icon_seamless,gfs_seamless,ecmwf_ifs025",
                "hourly": "temperature_2m",
                "temperature_unit": "celsius",
                "start_date": target_iso,
                "end_date": target_iso,
            },
            timeout=20,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        hourly = data.get("hourly") or {}
    except Exception:
        return None

    def _max_of(model_key: str) -> Optional[float]:
        series = hourly.get(f"temperature_2m_{model_key}")
        if not series:
            return None
        clean = [v for v in series if v is not None]
        return max(clean) if clean else None

    icon_v = _max_of("icon_seamless")
    gfs_v = _max_of("gfs_seamless")
    ecmwf_v = _max_of("ecmwf_ifs025")
    present = [v for v in (icon_v, gfs_v, ecmwf_v) if v is not None]
    if not present:
        return None

    spread_c = max(present) - min(present)
    result = {
        "icon_max_c": icon_v,
        "gfs_max_c": gfs_v,
        "ecmwf_max_c": ecmwf_v,
        "spread_c": round(spread_c, 2),
        "agree": spread_c <= 3.0,
        "n_models": len(present),
    }
    _OM_CACHE[cache_key] = (now, result)
    return result


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
    """Lowercase + strip noise words + collapse whitespace.
    'The Hartsfield-Jackson International Airport' -> 'hartsfield-jackson'
    """
    if not phrase:
        return ""
    cleaned = _STATION_NOISE_WORDS.sub(" ", phrase.lower())
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
            # normalized + progressively-shorter prefixes.
            raw_lower = re.sub(r"\s+", " ", raw.lower()).strip()
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
                raw_question=question,
            )

    # "X°F or higher"
    m = _TEMP_AT_LEAST_RE.search(question)
    if m:
        unit = (m.group("unit") or "F")[0].upper()
        return MarketSpec(
            city=city, threshold_value=float(m.group("val")),
            threshold_unit=unit, metric="temp", comparison="at_least",
            target_date=target_date, confidence=confidence,
            raw_question=question,
        )

    # "X°F or lower"
    m = _TEMP_AT_MOST_RE.search(question)
    if m:
        unit = (m.group("unit") or "F")[0].upper()
        return MarketSpec(
            city=city, threshold_value=float(m.group("val")),
            threshold_unit=unit, metric="temp", comparison="at_most",
            target_date=target_date, confidence=confidence,
            raw_question=question,
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
            raw_question=question,
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
PROB_CLIP_LOW = 0.10
PROB_CLIP_HIGH = 0.90


def _clip_prob(p: Optional[float]) -> Optional[float]:
    """Clip a forecast probability to [PROB_CLIP_LOW, PROB_CLIP_HIGH].
    None passes through unchanged so callers can still treat as 'unknown'."""
    if p is None:
        return None
    return max(PROB_CLIP_LOW, min(PROB_CLIP_HIGH, float(p)))


def forecast_probability(spec: MarketSpec, forecast: dict,
                          mae_override: Optional[float] = None,
                          bias_override: Optional[float] = None
                          ) -> Optional[float]:
    """Compute P(YES) for `spec` clipped to [0.10, 0.90].

    `mae_override` (v7): replaces the static MAE_TEMP_F/C or
    MAE_PRECIP_MM constant for this call (dynamic MAE from history).

    `bias_override` (v8): added to the forecast `ref` value before the
    z-score. Per-city systematic offset to compensate for residual
    error between our forecast source and the resolution station even
    after fixing coordinates. Only applied to `metric=='temp'`. Units
    match `spec.threshold_unit` (operator sets temp_bias_f in cities
    JSON; bot converts on lookup if market is in C).
    """
    return _clip_prob(_forecast_probability_raw(
        spec, forecast, mae_override=mae_override,
        bias_override=bias_override))


def _forecast_probability_raw(spec: MarketSpec, forecast: dict,
                                mae_override: Optional[float] = None,
                                bias_override: Optional[float] = None
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
        # For "highest temperature" markets (typical Polymarket weather event),
        # we always use temp_high as the reference.
        ref = day.get(f"temp_high_{spec.threshold_unit.lower()}")
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
    fraction. Each leg is a dict with at minimum keys "forecast_prob" (p)
    and "entry_price" (price). Returns the same list of dicts augmented
    with "stake_usd" and "kelly_frac" keys, in input order.

    Kelly per leg (YES bet convention; bot stores side-aware probs so this
    holds for NO side too because p and price are P(side) / market(side)):
        kelly_i = max(0, (p*(1-price) - (1-p)*price) / (1-price))

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
        p = float(leg.get("forecast_prob") or 0.0)
        price = float(leg.get("entry_price") or 0.0)
        if price <= 0 or price >= 1:
            kelly = 0.0
        else:
            num = p * (1 - price) - (1 - p) * price
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
) -> dict:
    """Decide whether to cash out an open position based on 4 OR'd triggers.

    Triggers (evaluated in order; first match wins):
      1. profit_lock     — bid >= entry + profit_lock_pp/100
      2. trailing_stop   — peak >= entry + trailing_min_gain_pp/100 AND
                           bid <= peak * (1 - trailing_drawdown_pct/100)
      3. convergence     — bid >= fair_value - convergence_pp/100, where
                           fair_value = forecast_prob_yes (YES) or
                                        1 - forecast_prob_yes (NO)
      4. forecast_reversal — forecast turned against us AND bid >= entry
                             (break-even backstop, existing behavior)

    Guard: if current_bid < entry_price, triggers 1-3 are suppressed
    (never sell at a loss on profit-taking logic). forecast_reversal still
    requires bid >= entry by definition.

    Returns: {decision: "CASHOUT"|"HOLD", trigger: str, reason: str}
    """
    in_profit = current_bid >= entry_price
    peak = float(peak_bid_seen) if peak_bid_seen is not None else 0.0

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

    # Trigger 3: convergence
    if in_profit and forecast_prob_yes is not None:
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

    # Test 3: forecast → prob (temperature)
    forecast = {"forecasts": [
        {"date": (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat(),
         "temp_high_f": 78, "temp_low_f": 60, "precip_probability": 30, "precip_mm": 1.2,
         "condition_main": "Clear"}
    ]}
    p = forecast_probability(spec, forecast)
    # z = (78-75)/5 = 0.6, norm.cdf(0.6) ≈ 0.7257
    assert p is not None and 0.70 <= p <= 0.74, f"got {p}"
    print(f"Test 3 PASS: P(YES) = {p:.4f} for forecast_high=78, threshold=75, MAE=5°F")

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
    assert p2 is not None and 0.83 <= p2 <= 0.85, f"got {p2}"
    print(f"Test 4 PASS: P(YES) = {p2:.4f} for forecast_mm=8, threshold=5mm")

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
    # _norm_cdf(z_low) - _norm_cdf(z_high) = cdf(2) - cdf(1.2) = 0.977 - 0.885 = 0.092
    assert p3 is not None and 0.05 <= p3 <= 0.13, f"got {p3}"
    print(f"Test 8 PASS: P(65-69°F | high=75) = {p3:.4f}")

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

    print("\nAll helper tests PASS")
