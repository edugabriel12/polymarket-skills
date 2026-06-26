#!/usr/bin/env python3
"""Run the statistical models (soccer/tennis) and turn each suggestion into a
unified entry. This orchestration moved here from Polymarket Sports — the brain
now owns the model calc; Sports only receives entries.

Every model entry is **1U (Alta), PRÉ-LIVE** (the models only bet pregame). The
skills themselves (`polymarket-soccer-goals`, `polymarket-tennis`) stay where
they are; we just drive them via subprocess, as Sports used to.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import entries as en
import subcategory as sc

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_BACKEND_DIR, "..", ".."))
_SOCCER_SUGGEST = os.path.join(_REPO_ROOT, "polymarket-soccer-goals", "scripts", "suggest_soccer.py")
_TENNIS_SUGGEST = os.path.join(_REPO_ROOT, "polymarket-tennis", "scripts", "suggest_tennis.py")

SOCCER_DB = os.environ.get("SOCCER_PREDICTIONS_DB")
TENNIS_DB = os.environ.get("TENNIS_PREDICTIONS_DB")
TENNIS_TOUR = os.environ.get("TENNIS_TOUR", "atp")
SOCCER_SHARP_BOOK = os.environ.get("SOCCER_SHARP_BOOK", "pinnacle,betfair_ex_eu,matchbook")


def _run(cmd: list[str], timeout: int) -> dict:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return {"error": f"model failed: {e}", "suggestions": []}
    if proc.stderr:
        print(proc.stderr, end="" if proc.stderr.endswith("\n") else "\n",
              file=sys.stderr, flush=True)
    if proc.returncode != 0:
        return {"error": (proc.stderr or "")[-400:], "suggestions": []}
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {"error": "bad model output", "suggestions": []}


def run_soccer(date: str) -> dict:
    cmd = [sys.executable, _SOCCER_SUGGEST, "--date", date, "--output", "json"]
    if SOCCER_DB:
        cmd += ["--predictions-db", SOCCER_DB]
    cmd += ["--sharp-min-reserve", "0", "--sharp-book", SOCCER_SHARP_BOOK]
    return _run(cmd, 240)


def run_tennis(date: str) -> dict:
    cmd = [sys.executable, _TENNIS_SUGGEST, "--date", date, "--output", "json", "--tour", TENNIS_TOUR]
    if TENNIS_DB:
        cmd += ["--predictions-db", TENNIS_DB]
    cmd += ["--sharp-min-reserve", "0"]
    raw = _run(cmd, 300)
    # tennis suggestions use {match, side, opponent, surface, price, edge} — normalize keys.
    norm = []
    for s in raw.get("suggestions", []):
        norm.append({"game": s.get("match"), "event": s.get("event"), "market": "MATCH",
                     "side": s.get("side"), "line": None, "edge": s.get("edge"),
                     "recommendation": {"price": s.get("price", 0.0)},
                     "market_url": s.get("market_url")})
    raw["suggestions"] = norm
    return raw


_PRETTY = __import__("re").compile(r"^[a-z0-9]+-([a-z0-9]+)-([a-z0-9]+)-", __import__("re").I)


def _event_name(game: str) -> str:
    m = _PRETTY.match(game or "")
    return f"{m.group(1).upper()} vs {m.group(2).upper()}" if m else (game or "")


def suggestion_to_entry(sug: dict, sport: str) -> dict:
    """Map one model suggestion to a unified entry: 1U / Alta / PRÉ-LIVE."""
    category = "Tennis" if sport == "tennis" else "Soccer"
    game = sug.get("game") or ""
    market = (sug.get("market") or "").upper()
    side = (sug.get("side") or "").upper()
    price = float(sug.get("recommendation", {}).get("price") or 0.0)
    odds = (1.0 / price) if price > 0 else 0.0
    subcat = sc.classify(category, game, game, "")     # slug suffix → Over/Under gols / BTTS / etc.
    # Prefer the full-name event the models now provide ("Cape Verde vs Saudi Arabia");
    # fall back to the slug's team codes. market_url may be top-level or in the recommendation.
    event = sug.get("event") or _event_name(game)
    market_url = sug.get("market_url") or (sug.get("recommendation") or {}).get("market_url")
    return en.make_entry(
        key=en.make_key("model", category, game, market, side),
        event=event, category=category, subcategory=subcat, side=side,
        odds=odds, entry_price=price, unit=1.0, confidence="Alta", live=en.PRELIVE,
        market_url=market_url, source="model")


def model_entries(date: str) -> list[dict]:
    """Run both models for `date` and return their entries (1U/Alta/PRÉ-LIVE)."""
    out: list[dict] = []
    for sport, runner in (("soccer", run_soccer), ("tennis", run_tennis)):
        res = runner(date)
        for sug in res.get("suggestions", []):
            if sug.get("game"):
                out.append(suggestion_to_entry(sug, sport))
    return out
