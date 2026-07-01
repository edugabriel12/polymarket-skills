"""Tests for weather-market detection. Run: `python test_weather_filter.py`."""
from __future__ import annotations

import weather_filter as wf


def test_keyword_matches():
    assert wf.matches_keywords("Highest temperature in NYC on July 2?")
    assert wf.matches_keywords("Will it rain in London tomorrow?")
    assert wf.matches_keywords("NYC snowfall over 3 inches?")
    assert wf.matches_keywords("Hurricane to make landfall in Florida?")
    print("ok test_keyword_matches")


def test_non_weather_rejected():
    assert not wf.matches_keywords("Will Arsenal beat Chelsea?")
    assert not wf.matches_keywords("Bitcoin above $100k by Friday?")
    assert not wf.matches_keywords("US presidential election winner?")
    print("ok test_non_weather_rejected")


def test_is_weather_by_text():
    trade = {"title": "Will it rain in NYC?", "slug": "rain-nyc", "eventSlug": "rain-nyc"}
    assert wf.is_weather(trade)
    print("ok test_is_weather_by_text")


def test_is_weather_by_slug_only():
    # No weather word in title, but the slug carries it.
    trade = {"title": "NYC July 2", "slug": "nyc-high-temperature-july-2", "eventSlug": ""}
    assert wf.is_weather(trade)
    print("ok test_is_weather_by_slug_only")


def test_is_weather_by_gamma_tag_fallback():
    # No keyword anywhere, but the Gamma event carries the 'weather' tag.
    trade = {"title": "NYC index", "slug": "nyc-index", "eventSlug": "nyc-index"}
    assert not wf.is_weather(trade)  # no tags -> not weather
    assert wf.is_weather(trade, tags=["nyc", "weather"])  # tag confirms
    assert not wf.is_weather(trade, tags=["politics", "nyc"])
    print("ok test_is_weather_by_gamma_tag_fallback")


def test_non_weather_trade():
    trade = {"title": "Arsenal vs Chelsea", "slug": "epl-ars-che", "eventSlug": "epl-ars-che"}
    assert not wf.is_weather(trade)
    assert not wf.is_weather(trade, tags=["soccer", "epl"])
    print("ok test_non_weather_trade")


def _run_all():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\nALL WEATHER-FILTER TESTS PASSED")


if __name__ == "__main__":
    _run_all()
