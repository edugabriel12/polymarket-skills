#!/usr/bin/env python3
"""Sharp-line reference for MLB totals (Pinnacle / market consensus).

The deep research (references/edge-pathways-deep-research.md) is unambiguous: a
predictive run-total model does NOT beat the MLB closing line — the sharp close
(Pinnacle, devigged) IS the efficient "true" probability. So the model's job is not
to out-predict but to act on DIVERGENCE: bet a side only when the Polymarket price is
cheaper than the sharp fair probability.

This module supplies that sharp reference, devigged to a fair (no-vig) Over/Under
probability per game, from either:
  - a CSV (date,away,home,total_line,over_odds,under_odds[,close_over_odds,close_under_odds])
  - The Odds API (the-odds-api.com), which includes Pinnacle — best-effort, lazy
    `requests`, returns {} offline so the pipeline degrades to the anti-fabrication
    (Polymarket-anchored, zero-edge) behaviour.

Pure parsing/devig is isolated for offline tests.
"""

from __future__ import annotations

import csv
import os

ODDS_API = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"


def american_to_implied(value) -> float | None:
    """American (-110/+120), decimal (1.91), or implied prob (0.524) -> implied prob."""
    if value in (None, ""):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if 0.0 < v < 1.0:
        return v
    if v >= 100 or v <= -100:
        return 100.0 / (v + 100.0) if v > 0 else (-v) / (-v + 100.0)
    if v > 1.0:
        return 1.0 / v
    return None


def devig(over_imp: float | None, under_imp: float | None) -> tuple[float, float] | None:
    """Two raw implied probs -> fair (no-vig) probs summing to 1. None if unusable."""
    if not over_imp or not under_imp or over_imp <= 0 or under_imp <= 0:
        return None
    s = over_imp + under_imp
    return over_imp / s, under_imp / s


def _key(date: str, away: str, home: str) -> tuple:
    return (date, frozenset((away.lower().strip(), home.lower().strip())))


# ---------------------------------------------------------------------------
# CSV source (offline-capable; also what the backtest already consumes)
# ---------------------------------------------------------------------------


def load_sharp_csv(path: str) -> dict:
    """{(date, {away,home}): {line, over_fair, under_fair, close_over_fair, ...}} from a CSV."""
    out: dict = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
            away = row.get("away") or row.get("team")
            home = row.get("home") or row.get("opponent")
            line = row.get("total_line") or row.get("total")
            if not away or not home or not line:
                continue
            fair = devig(american_to_implied(row.get("over_odds") or row.get("over_price")),
                         american_to_implied(row.get("under_odds") or row.get("under_price")))
            close = devig(american_to_implied(row.get("close_over_odds")),
                          american_to_implied(row.get("close_under_odds")))
            rec = {"line": float(line)}
            if fair:
                rec["over_fair"], rec["under_fair"] = fair
            if close:
                rec["close_over_fair"], rec["close_under_fair"] = close
            if "over_fair" in rec or "close_over_fair" in rec:
                out[_key(row.get("date", ""), away, home)] = rec
    return out


# ---------------------------------------------------------------------------
# The Odds API (network; best-effort)
# ---------------------------------------------------------------------------


def parse_oddsapi(events: list, book: str = "pinnacle") -> dict:
    """Parse a The Odds API /odds response into {(date,{teams}): {line,over_fair,under_fair}}.

    Pure: pass the decoded JSON list. Prefers `book` (Pinnacle); falls back to the first
    bookmaker that prices a totals market. Devigs the Over/Under to fair probabilities.
    """
    out: dict = {}
    for ev in events or []:
        home, away = (ev.get("home_team") or ""), (ev.get("away_team") or "")
        date = (ev.get("commence_time") or "")[:10]
        books = ev.get("bookmakers") or []
        chosen = next((b for b in books if b.get("key") == book), None) or (books[0] if books else None)
        if not chosen or not home or not away:
            continue
        mk = next((m for m in chosen.get("markets", []) if m.get("key") == "totals"), None)
        if not mk:
            continue
        over = under = line = None
        for o in mk.get("outcomes", []):
            name = (o.get("name") or "").lower()
            if name == "over":
                over = american_to_implied(o.get("price")); line = o.get("point")
            elif name == "under":
                under = american_to_implied(o.get("price"))
        fair = devig(over, under)
        if fair and line is not None:
            rec = {"line": float(line), "over_fair": fair[0], "under_fair": fair[1]}
            out[_key(date, away, home)] = rec
    return out


def fetch_sharp(api_key: str | None, date: str, book: str = "pinnacle", timeout: int = 10) -> dict:
    """Best-effort sharp totals for a date via The Odds API. {} on any failure."""
    api_key = api_key or os.environ.get("ODDS_API_KEY")
    if not api_key:
        return {}
    try:
        import requests  # lazy
        resp = requests.get(ODDS_API, params={
            "apiKey": api_key, "regions": "us,eu", "markets": "totals",
            "oddsFormat": "american", "bookmakers": book}, timeout=timeout)
        resp.raise_for_status()
        events = resp.json()
    except Exception:  # noqa: BLE001
        return {}
    return {k: v for k, v in parse_oddsapi(events, book).items() if k[0] == date} or parse_oddsapi(events, book)


def sharp_over_prob(lookup: dict, date: str, away: str, home: str, line: float | None = None,
                    use_close: bool = False) -> float | None:
    """Resolve the sharp fair P(Over) for a game from a lookup, or None.

    use_close pulls the CLOSING fair prob (for CLV); else the entry/open fair prob.
    Line is matched leniently (sharp line within 0.5 of ours), since books may differ.
    """
    rec = lookup.get(_key(date, away, home))
    if not rec:
        return None
    if line is not None and rec.get("line") is not None and abs(float(rec["line"]) - float(line)) > 0.51:
        return None
    return rec.get("close_over_fair" if use_close else "over_fair") or rec.get("over_fair")
