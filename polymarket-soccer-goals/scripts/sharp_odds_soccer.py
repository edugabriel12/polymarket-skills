#!/usr/bin/env python3
"""Sharp-line reference for soccer goals markets (Pinnacle / consensus, via The Odds API).

The same lesson as MLB: a predictive goals model (Elo + Dixon-Coles) does not beat an
efficient closing line — the sharp close IS the fair probability. So this supplies the sharp
reference that turns the model into a DIVERGENCE detector: bet a side only when Polymarket
prices it away from the sharp fair value.

Soccer differs from MLB in two ways, both handled here:
  - There is no single "all soccer" feed — each league is its own Odds-API sport key
    (`soccer_fifa_world_cup`, `soccer_epl`, …). `fetch_active_soccer_keys` lists the active
    ones; `fetch_sharp_soccer` queries each.
  - We anchor BOTH markets: TOTALS (over/under goals, the bulk `/odds` endpoint) and BTTS
    (an additional market, fetched per event).

Games are matched to Polymarket by NORMALIZED TEAM NAME + date (the Odds API gives full
names; the Polymarket question carries them too), so no fragile name->slug-code map is
needed. Pure parsing/normalization/devig is isolated for offline tests; the fetch is
best-effort (lazy `requests`, returns {} offline) and never logs the API key.
"""

from __future__ import annotations

import os
import re
import unicodedata
from datetime import datetime, timedelta

ODDS_API = "https://api.the-odds-api.com/v4"
_APIKEY_RE = re.compile(r"(apiKey=)[^&\s]+", re.IGNORECASE)


def _redact(text) -> str:
    return _APIKEY_RE.sub(r"\1***", str(text))


# ---------------------------------------------------------------------------
# Devig (shared shape with the MLB module; kept local so the skill is self-contained)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Team-name normalization + question parsing (match sharp <-> Polymarket by NAME)
# ---------------------------------------------------------------------------

# Cross-source spelling variants -> one canonical key. Mostly national teams (the World Cup
# slate); extend as club-name mismatches surface in the logs.
NAME_ALIASES: dict[str, str] = {
    "usa": "united states", "united states of america": "united states", "us": "united states",
    "dr congo": "congo dr", "democratic republic of congo": "congo dr", "rd congo": "congo dr",
    "south korea": "korea republic", "korea south": "korea republic", "republic of korea": "korea republic",
    "north korea": "korea dpr", "ivory coast": "cote divoire", "cote d ivoire": "cote divoire",
    "czech republic": "czechia", "cabo verde": "cape verde", "turkiye": "turkey",
    "iran": "ir iran", "bosnia": "bosnia and herzegovina",
    "republic of ireland": "ireland", "uae": "united arab emirates",
}


def norm_name(s: str) -> str:
    """Lowercase, strip accents/punctuation, collapse spaces, apply the alias map."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    s = re.sub(r"\s+", " ", s).strip()
    return NAME_ALIASES.get(s, s)


_VS_RE = re.compile(r"\s+(?:vs?\.?|x|-|@)\s+", re.IGNORECASE)


def extract_teams_from_question(question: str) -> tuple[str, str] | None:
    """('portugal','uzbekistan') from a Polymarket question like 'Portugal vs Uzbekistan: O/U 2.5'.

    Splits on the matchup separator (vs/x/-/@), drops any trailing market phrase after a colon.
    Returns NORMALIZED names, or None if it can't find two sides.
    """
    if not question:
        return None
    head = question.split(":", 1)[0]            # drop "...: O/U 2.5" / "...: Both teams..."
    parts = _VS_RE.split(head, maxsplit=1)
    if len(parts) != 2:
        return None
    a, b = norm_name(parts[0]), norm_name(parts[1])
    return (a, b) if a and b else None


def _key(date: str, a: str, b: str) -> tuple:
    return (date, frozenset((norm_name(a), norm_name(b))))


def _adjacent_dates(date: str) -> list[str]:
    """The target date AND the next UTC day.

    A game listed on Polymarket for a US/Europe local date (e.g. a late kickoff) can have a
    UTC commence_time on the FOLLOWING calendar day, so the sharp event is keyed one day ahead.
    Matching both recovers those games instead of dropping them as "no sharp reference".
    """
    out = [date]
    try:
        out.append((datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d"))
    except (ValueError, TypeError):
        pass
    return out


def _find_rec(lookup: dict, date: str, a: str, b: str) -> dict | None:
    """Look up a game's sharp record by team pair, trying `date` then the next UTC day."""
    for d in _adjacent_dates(date):
        rec = lookup.get(_key(d, a, b))
        if rec:
            return rec
    return None


# ---------------------------------------------------------------------------
# Parse The Odds API responses -> a sharp lookup
# ---------------------------------------------------------------------------


def _main_totals(market: dict) -> tuple[float, float, float] | None:
    """(line, over_imp, under_imp) for the BALANCED main total line of a totals market.

    A totals market can bundle alternate points; the main line is the one whose Over implied
    prob is closest to even (avoids grabbing a stray alternate)."""
    by_point: dict = {}
    for o in market.get("outcomes", []):
        name, pt = (o.get("name") or "").lower(), o.get("point")
        if pt is None or name not in ("over", "under"):
            continue
        by_point.setdefault(pt, {})[name] = american_to_implied(o.get("price"))
    complete = {p: v for p, v in by_point.items() if v.get("over") and v.get("under")}
    if not complete:
        return None
    pt = min(complete, key=lambda p: abs(complete[p]["over"] - 0.5))
    return float(pt), complete[pt]["over"], complete[pt]["under"]


def _chosen_book(ev: dict, book: str):
    books = ev.get("bookmakers") or []
    return next((b for b in books if b.get("key") == book), None) or (books[0] if books else None)


def parse_totals(events: list, book: str = "pinnacle") -> dict:
    """{(date,{teams}): {total_line, over_fair, under_fair, home, away}} from a totals response."""
    out: dict = {}
    for ev in events or []:
        home, away = ev.get("home_team") or "", ev.get("away_team") or ""
        date = (ev.get("commence_time") or "")[:10]
        chosen = _chosen_book(ev, book)
        if not chosen or not home or not away:
            continue
        mk = next((m for m in chosen.get("markets", []) if m.get("key") == "totals"), None)
        main = _main_totals(mk) if mk else None
        fair = devig(main[1], main[2]) if main else None
        if fair:
            out[_key(date, home, away)] = {"total_line": main[0], "over_fair": fair[0],
                                           "under_fair": fair[1], "home": norm_name(home),
                                           "away": norm_name(away)}
    return out


def parse_btts(events: list, book: str = "pinnacle") -> dict:
    """{(date,{teams}): {btts_yes_fair, btts_no_fair}} from a btts (per-event) response."""
    out: dict = {}
    for ev in events or []:
        home, away = ev.get("home_team") or "", ev.get("away_team") or ""
        date = (ev.get("commence_time") or "")[:10]
        chosen = _chosen_book(ev, book)
        if not chosen or not home or not away:
            continue
        mk = next((m for m in chosen.get("markets", []) if m.get("key") == "btts"), None)
        if not mk:
            continue
        yes = no = None
        for o in mk.get("outcomes", []):
            name = (o.get("name") or "").lower()
            if name == "yes":
                yes = american_to_implied(o.get("price"))
            elif name == "no":
                no = american_to_implied(o.get("price"))
        fair = devig(yes, no)
        if fair:
            out[_key(date, home, away)] = {"btts_yes_fair": fair[0], "btts_no_fair": fair[1]}
    return out


def merge_lookup(totals: dict, btts: dict | None = None) -> dict:
    """Combine the totals + BTTS lookups into one keyed by (date, {teams})."""
    out = {k: dict(v) for k, v in (totals or {}).items()}
    for k, v in (btts or {}).items():
        out.setdefault(k, {}).update(v)
    return out


# ---------------------------------------------------------------------------
# Resolve a game's sharp reference (matched by team name + date)
# ---------------------------------------------------------------------------


def sharp_total_ref(lookup: dict, date: str, a: str, b: str) -> tuple[float, float] | None:
    """(total_line, fair P(over)) for a game, or None. Names are normalized internally;
    the date matches `date` or the next UTC day (late kickoffs)."""
    rec = _find_rec(lookup, date, a, b)
    if not rec or rec.get("over_fair") is None or rec.get("total_line") is None:
        return None
    return float(rec["total_line"]), float(rec["over_fair"])


def sharp_btts_ref(lookup: dict, date: str, a: str, b: str) -> float | None:
    """Fair P(BTTS yes) for a game, or None (date matches `date` or the next UTC day)."""
    rec = _find_rec(lookup, date, a, b)
    return float(rec["btts_yes_fair"]) if rec and rec.get("btts_yes_fair") is not None else None


# ---------------------------------------------------------------------------
# The Odds API (network; best-effort; key never logged)
# ---------------------------------------------------------------------------


def fetch_active_soccer_keys(api_key: str | None, timeout: int = 10, vlog=None) -> list[str]:
    """Active soccer sport keys (excluding outright/futures markets). [] on failure."""
    vlog = vlog or (lambda *a, **k: None)
    api_key = api_key or os.environ.get("ODDS_API_KEY")
    if not api_key:
        vlog("  [odds-api] no API key -> cannot list soccer leagues")
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
            if str(s.get("key", "")).startswith("soccer_")
            and s.get("active") and not s.get("has_outrights")]
    vlog(f"  [odds-api] {len(keys)} active soccer league(s)")
    return [k for k in keys if k]


def _rem_int(resp) -> int | None:
    try:
        return int(resp.headers.get("x-requests-remaining"))
    except (TypeError, ValueError):
        return None


def fetch_sharp_soccer(api_key: str | None, keys: list[str], *, date: str | None = None,
                       with_btts: bool = True, book: str = "pinnacle", regions: str = "eu",
                       timeout: int = 10, vlog=None, min_quota_reserve: int = 0) -> dict:
    """Sharp totals (+ optional BTTS) across the given leagues -> merged lookup. {} offline.

    Totals come from the bulk per-league `/odds` endpoint (one call/league). BTTS is an
    additional market fetched per event (`/events/{id}/odds`), so it costs more calls — kept
    behind `with_btts`. `date` (YYYY-MM-DD) filters to that day's games when given.

    `min_quota_reserve`: stop fetching once The Odds API's remaining quota drops to this
    floor, leaving credits for other consumers (e.g. the MLB divergence detector). All-leagues
    + BTTS can otherwise exhaust a free plan in days and starve MLB of its sharp anchor.
    """
    vlog = vlog or (lambda *a, **k: None)
    api_key = api_key or os.environ.get("ODDS_API_KEY")
    if not api_key or not keys:
        return {}
    import requests  # lazy
    base = {"apiKey": api_key, "regions": regions, "oddsFormat": "american", "bookmakers": book}
    totals_all: list = []
    btts_all: list = []
    remaining: int | None = None

    def _reserve_hit() -> bool:
        if min_quota_reserve and remaining is not None and remaining <= min_quota_reserve:
            vlog(f"  [odds-api] quota reserve reached (remaining={remaining} <= "
                 f"{min_quota_reserve}) -> stopping soccer fetch to preserve quota for MLB")
            return True
        return False

    for key in keys:
        if _reserve_hit():
            break
        try:
            r = requests.get(f"{ODDS_API}/sports/{key}/odds",
                             params={**base, "markets": "totals"}, timeout=timeout)
            remaining = _rem_int(r) if _rem_int(r) is not None else remaining
            used = r.headers.get("x-requests-used")
            r.raise_for_status()
            events = r.json() or []
        except Exception as e:  # noqa: BLE001
            vlog(f"  [odds-api] {key} totals failed: {_redact(e)}")
            continue
        if date:
            events = [e for e in events if (e.get("commence_time") or "")[:10] == date] or events
        totals_all += events
        vlog(f"  [odds-api] {key}: {len(events)} game(s) (quota used={used} remaining={remaining})")
        if with_btts:
            for ev in events:
                if _reserve_hit():
                    break
                eid = ev.get("id")
                if not eid:
                    continue
                try:
                    rb = requests.get(f"{ODDS_API}/sports/{key}/events/{eid}/odds",
                                      params={**base, "markets": "btts"}, timeout=timeout)
                    remaining = _rem_int(rb) if _rem_int(rb) is not None else remaining
                    rb.raise_for_status()
                    btts_all.append(rb.json())
                except Exception as e:  # noqa: BLE001
                    vlog(f"  [odds-api] {key}/{eid} btts failed: {_redact(e)}")
            if _reserve_hit():
                break
    lookup = merge_lookup(parse_totals(totals_all, book), parse_btts(btts_all, book))
    vlog(f"  [odds-api] sharp soccer lookup: {len(lookup)} game(s) "
         f"({sum('btts_yes_fair' in v for v in lookup.values())} with BTTS)")
    return lookup
