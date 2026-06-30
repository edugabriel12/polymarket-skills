#!/usr/bin/env python3
"""Run the soccer statistical model and turn each suggestion into a unified entry.
This orchestration moved here from Polymarket Sports — the brain now owns the model
calc; Sports only receives entries.

Every model entry is **1U (Alta), PRÉ-LIVE** (the model only bets pregame). The skill
itself (`polymarket-soccer-goals`) stays where it is; we just drive it via subprocess,
as Sports used to.
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

SOCCER_DB = os.environ.get("SOCCER_PREDICTIONS_DB")
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


_PRETTY = __import__("re").compile(r"^[a-z0-9]+-([a-z0-9]+)-([a-z0-9]+)-", __import__("re").I)


def _event_name(game: str) -> str:
    m = _PRETTY.match(game or "")
    return f"{m.group(1).upper()} vs {m.group(2).upper()}" if m else (game or "")


def suggestion_to_entry(sug: dict) -> dict:
    """Map one soccer model suggestion to a unified entry: 1U / Alta / PRÉ-LIVE."""
    category = "Soccer"
    game = sug.get("game") or ""
    market = (sug.get("market") or "").upper()
    side = (sug.get("side") or "").upper()
    price = float(sug.get("recommendation", {}).get("price") or 0.0)
    odds = (1.0 / price) if price > 0 else 0.0
    subcat = sc.classify(category, game, game, "")     # slug suffix → Over/Under gols / BTTS / etc.
    # Prefer the full-name event the model now provides ("Cape Verde vs Saudi Arabia");
    # fall back to the slug's team codes. market_url may be top-level or in the recommendation.
    event = sug.get("event") or _event_name(game)
    market_url = sug.get("market_url") or (sug.get("recommendation") or {}).get("market_url")
    return en.make_entry(
        key=en.make_key("model", category, game, market, side),
        event=event, category=category, subcategory=subcat, side=side,
        odds=odds, entry_price=price, unit=1.0, confidence="Alta", live=en.PRELIVE,
        market_url=market_url, source="model")


def model_entries(date: str) -> list[dict]:
    """Run the soccer model for `date` and return its entries (1U/Alta/PRÉ-LIVE)."""
    out: list[dict] = []
    res = run_soccer(date)
    for sug in res.get("suggestions", []):
        if sug.get("game"):
            out.append(suggestion_to_entry(sug))
    return out
