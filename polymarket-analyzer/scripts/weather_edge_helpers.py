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
import re
import statistics
import unicodedata
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


def _strip_accents(s: str) -> str:
    """Remove diacritics so 'São Paulo' matches 'Sao Paulo'."""
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if not unicodedata.combining(c))


# ---------------------------------------------------------------------------
# Constants — calibrated MAEs for normal-CDF probability conversion
# ---------------------------------------------------------------------------

MAE_TEMP_F = 5.0      # OpenWeather 3-5d temp forecast MAE in °F
MAE_TEMP_C = 2.78     # = MAE_TEMP_F converted
MAE_PRECIP_MM = 3.0   # precip total MAE
MAE_WIND_KPH = 8.0    # wind speed MAE


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
                "us_top50": [], "aliases": {}}
    return json.loads(path.read_text())


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
    """Find the canonical city name in `text` if any. Accent-insensitive."""
    lookup = _all_known_cities(cities)
    text_lower = _strip_accents(text).lower()
    # Try longest matches first to avoid "York" matching when "New York" should
    for name in sorted(lookup, key=len, reverse=True):
        if re.search(r"\b" + re.escape(name) + r"\b", text_lower):
            return lookup[name]
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


def forecast_probability(spec: MarketSpec, forecast: dict) -> Optional[float]:
    """Compute P(YES) for `spec` given OpenWeather forecast JSON.

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
        mae = MAE_TEMP_F if spec.threshold_unit == "F" else MAE_TEMP_C
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
        z = (forecast_mm - threshold_mm) / MAE_PRECIP_MM
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

    print("\nAll helper tests PASS")
