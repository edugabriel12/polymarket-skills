"""Weather-market detection for the copy-trader.

Only trades on WEATHER markets are copied. Detection mirrors the existing weather
edge bot (`polymarket-analyzer/scripts/weather_edge_bot.py::_WEATHER_KEYWORDS`),
which filters client-side by keyword because Gamma's `tag_slug=weather` query param
is silently ignored. We use the same keyword set as the primary signal and treat a
Gamma "weather" event tag as an extra positive signal when available.

Pure functions (regex + set membership) → offline-testable.
"""
from __future__ import annotations

import re

# Mirrors weather_edge_bot._WEATHER_KEYWORDS (kept local to avoid importing that
# heavier module, which wires repo paths and forecast scripts at import time).
_WEATHER_KEYWORDS = re.compile(
    r"\b(weather|temperature|temp|rain|rainfall|snow|snowfall|"
    r"precipitation|precip|hurricane|storm|wind|fahrenheit|celsius|"
    r"hottest|coldest|warmest|coolest|degrees|°[fc]|inches\s+of|"
    r"mm\s+of|cm\s+of)\b",
    re.IGNORECASE,
)

WEATHER_TAG = "weather"


def market_text(trade: dict) -> str:
    """Classification blob from a trade/position: title + slug + eventSlug."""
    return " ".join(str(trade.get(k, "")) for k in ("title", "slug", "eventSlug"))


def matches_keywords(text: str | None) -> bool:
    return bool(_WEATHER_KEYWORDS.search(text or ""))


def is_weather(trade: dict, tags: list[str] | None = None) -> bool:
    """True when the trade's market is a weather market.

    Primary signal: weather keywords in title/slug/eventSlug. Secondary: a Gamma
    event tag equal to 'weather' (pass the already-fetched `tags` list, lowercased
    slugs as returned by analyze_wallet.fetch_event_tags)."""
    if matches_keywords(market_text(trade)):
        return True
    if tags and any((t or "").lower() == WEATHER_TAG for t in tags):
        return True
    return False
