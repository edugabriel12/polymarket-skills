#!/usr/bin/env python3
"""Sharp-line reference for tennis match-winner markets (Pinnacle, via The Odds API).

Same divergence-detector logic as MLB/soccer: the sharp close is the efficient probability,
so anchor the model to it and bet only when Polymarket diverges. Tennis is the simplest case
— a single h2h (match-winner) market per match, no totals/BTTS — so it's also the cheapest:
a couple of tour keys (`tennis_atp_*`, `tennis_wta_*`), one bulk `/odds?markets=h2h` call each.

Players are matched by SURNAME + date (the Odds API gives full names; Polymarket outcome
labels carry the surname), which is the most stable key across sources. Pure parsing/devig/
normalization is offline-testable; the fetch is best-effort (lazy `requests`, {} offline) and
never logs the API key.
"""

from __future__ import annotations

import os
import re
import unicodedata

ODDS_API = "https://api.the-odds-api.com/v4"
_APIKEY_RE = re.compile(r"(apiKey=)[^&\s]+", re.IGNORECASE)


def _redact(text) -> str:
    return _APIKEY_RE.sub(r"\1***", str(text))


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


def devig(a_imp: float | None, b_imp: float | None) -> tuple[float, float] | None:
    """Two raw implied probs -> fair (no-vig) probs summing to 1. None if unusable."""
    if not a_imp or not b_imp or a_imp <= 0 or b_imp <= 0:
        return None
    s = a_imp + b_imp
    return a_imp / s, b_imp / s


def norm_player(name: str) -> str:
    """Normalize a player name to a SURNAME key (last token, accent/punct-stripped).

    The Odds API gives 'Carlos Alcaraz'; Polymarket labels carry 'Alcaraz' — both collapse
    to 'alcaraz'. Best-effort for compound surnames (uses the last whitespace token).
    """
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    s = re.sub(r"\s+", " ", s).strip()
    return s.split()[-1] if s.split() else s


def _key(date: str, a: str, b: str) -> tuple:
    return (date, frozenset((norm_player(a), norm_player(b))))


def parse_h2h(events: list, book: str = "pinnacle") -> dict:
    """{(date,{surnames}): {surname: fair_p, ...}} from a tennis h2h /odds response."""
    out: dict = {}
    for ev in events or []:
        home, away = ev.get("home_team") or "", ev.get("away_team") or ""
        date = (ev.get("commence_time") or "")[:10]
        books = ev.get("bookmakers") or []
        chosen = next((b for b in books if b.get("key") == book), None) or (books[0] if books else None)
        if not chosen or not home or not away:
            continue
        mk = next((m for m in chosen.get("markets", []) if m.get("key") == "h2h"), None)
        if not mk:
            continue
        prices = {}
        for o in mk.get("outcomes", []):
            nm = o.get("name")
            if nm:
                prices[nm] = american_to_implied(o.get("price"))
        if home not in prices or away not in prices:
            continue
        fair = devig(prices[home], prices[away])
        if fair:
            out[_key(date, home, away)] = {norm_player(home): fair[0], norm_player(away): fair[1]}
    return out


def sharp_win_ref(lookup: dict, date: str, player: str, opponent: str) -> float | None:
    """Sharp fair P(`player` beats `opponent`) for the match, or None."""
    rec = lookup.get(_key(date, player, opponent))
    if not rec:
        return None
    return rec.get(norm_player(player))


# ---------------------------------------------------------------------------
# The Odds API (network; best-effort; key never logged)
# ---------------------------------------------------------------------------


def fetch_active_tennis_keys(api_key: str | None, timeout: int = 10, vlog=None) -> list[str]:
    """Active tennis sport keys (excluding outright/futures). [] on failure."""
    vlog = vlog or (lambda *a, **k: None)
    api_key = api_key or os.environ.get("ODDS_API_KEY")
    if not api_key:
        vlog("  [odds-api] no API key -> cannot list tennis tours")
        return []
    try:
        import requests  # lazy
        resp = requests.get(f"{ODDS_API}/sports", params={"apiKey": api_key}, timeout=timeout)
        resp.raise_for_status()
        sports = resp.json()
    except Exception as e:  # noqa: BLE001
        vlog(f"  [odds-api] /sports failed: {_redact(e)}")
        return []
    keys = [s.get("key") for s in (sports or [])
            if str(s.get("key", "")).startswith("tennis_")
            and s.get("active") and not s.get("has_outrights")]
    vlog(f"  [odds-api] {len(keys)} active tennis tour(s)")
    return [k for k in keys if k]


def _rem_int(resp) -> int | None:
    try:
        return int(resp.headers.get("x-requests-remaining"))
    except (TypeError, ValueError):
        return None


def fetch_sharp_tennis(api_key: str | None, keys: list[str], *, date: str | None = None,
                       book: str = "pinnacle", regions: str = "eu", timeout: int = 10,
                       vlog=None, min_quota_reserve: int = 0) -> dict:
    """Sharp h2h win probs across the given tours -> lookup. {} offline.

    One bulk `/odds?markets=h2h` call per tour. `min_quota_reserve` stops fetching once the
    Odds-API remaining quota hits the floor, preserving credits for other sports (e.g. MLB).
    """
    vlog = vlog or (lambda *a, **k: None)
    api_key = api_key or os.environ.get("ODDS_API_KEY")
    if not api_key or not keys:
        return {}
    import requests  # lazy
    base = {"apiKey": api_key, "regions": regions, "oddsFormat": "american", "bookmakers": book}
    events_all: list = []
    remaining: int | None = None
    for key in keys:
        if min_quota_reserve and remaining is not None and remaining <= min_quota_reserve:
            vlog(f"  [odds-api] quota reserve reached (remaining={remaining} <= "
                 f"{min_quota_reserve}) -> stopping tennis fetch to preserve quota for MLB")
            break
        try:
            r = requests.get(f"{ODDS_API}/sports/{key}/odds",
                             params={**base, "markets": "h2h"}, timeout=timeout)
            remaining = _rem_int(r) if _rem_int(r) is not None else remaining
            used = r.headers.get("x-requests-used")
            r.raise_for_status()
            events = r.json() or []
        except Exception as e:  # noqa: BLE001
            vlog(f"  [odds-api] {key} h2h failed: {_redact(e)}")
            continue
        if date:
            events = [e for e in events if (e.get("commence_time") or "")[:10] == date] or events
        events_all += events
        vlog(f"  [odds-api] {key}: {len(events)} match(es) (quota used={used} remaining={remaining})")
    lookup = parse_h2h(events_all, book)
    vlog(f"  [odds-api] sharp tennis lookup: {len(lookup)} match(es)")
    return lookup
