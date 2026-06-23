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
import re
from datetime import datetime, timezone

import ballparks

# The Odds API takes the key as an `apiKey=` query param, so requests' own error
# strings (HTTPError "...for url: ...apiKey=SECRET...") and any echoed URL would leak
# it. Redact the value from anything we log (CLAUDE.md: never echo secrets).
_APIKEY_RE = re.compile(r"(apiKey=)[^&\s]+", re.IGNORECASE)


def _redact(text) -> str:
    return _APIKEY_RE.sub(r"\1***", str(text))

ODDS_API = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"

# Full team names / cities / nicknames -> the lowercase abbreviation Polymarket uses
# in its game slugs (mlb-chc-nym-...). Sharp sources disagree on team identifiers: a
# CSV (e.g. oddsDataMLB) uses abbreviations ("CHC","NYM"), while The Odds API uses full
# names ("Chicago Cubs"). Normalizing BOTH to the Polymarket abbreviation is what lets a
# sharp game key MATCH its Polymarket market (the cause of the earlier "0 of N matched").
MLB_TEAM_ABBR: dict[str, str] = {
    "diamondbacks": "ari", "arizona": "ari", "arizona diamondbacks": "ari", "dbacks": "ari",
    "braves": "atl", "atlanta": "atl", "atlanta braves": "atl",
    "orioles": "bal", "baltimore": "bal", "baltimore orioles": "bal",
    "red sox": "bos", "boston": "bos", "boston red sox": "bos", "redsox": "bos",
    "cubs": "chc", "chicago cubs": "chc",
    "white sox": "cws", "chicago white sox": "cws", "whitesox": "cws",
    "reds": "cin", "cincinnati": "cin", "cincinnati reds": "cin",
    "guardians": "cle", "cleveland": "cle", "cleveland guardians": "cle", "indians": "cle",
    "rockies": "col", "colorado": "col", "colorado rockies": "col",
    "tigers": "det", "detroit": "det", "detroit tigers": "det",
    "astros": "hou", "houston": "hou", "houston astros": "hou",
    "royals": "kc", "kansas city": "kc", "kansas city royals": "kc",
    "angels": "laa", "los angeles angels": "laa", "la angels": "laa", "anaheim": "laa",
    "dodgers": "lad", "los angeles dodgers": "lad", "la dodgers": "lad",
    "marlins": "mia", "miami": "mia", "miami marlins": "mia",
    "brewers": "mil", "milwaukee": "mil", "milwaukee brewers": "mil",
    "twins": "min", "minnesota": "min", "minnesota twins": "min",
    "mets": "nym", "new york mets": "nym", "ny mets": "nym",
    "yankees": "nyy", "new york yankees": "nyy", "ny yankees": "nyy",
    "athletics": "oak", "oakland": "oak", "oakland athletics": "oak", "a's": "oak", "as": "oak",
    "phillies": "phi", "philadelphia": "phi", "philadelphia phillies": "phi",
    "pirates": "pit", "pittsburgh": "pit", "pittsburgh pirates": "pit",
    "padres": "sd", "san diego": "sd", "san diego padres": "sd",
    "giants": "sf", "san francisco": "sf", "san francisco giants": "sf",
    "mariners": "sea", "seattle": "sea", "seattle mariners": "sea",
    "cardinals": "stl", "st louis": "stl", "st. louis": "stl",
    "st louis cardinals": "stl", "st. louis cardinals": "stl",
    "rays": "tb", "tampa bay": "tb", "tampa bay rays": "tb",
    "rangers": "tex", "texas": "tex", "texas rangers": "tex",
    "blue jays": "tor", "toronto": "tor", "toronto blue jays": "tor", "bluejays": "tor",
    "nationals": "wsh", "washington": "wsh", "washington nationals": "wsh", "nats": "wsh",
}


def normalize_team(token: str) -> str:
    """Normalize any sharp team token to the lowercase Polymarket slug abbreviation.

    Handles abbreviations ("CHC"), full names ("Chicago Cubs"), and the ballparks
    alias table ("chw"->"cws", "az"->"ari"). Falls back to the cleaned token so an
    unknown team still keys consistently (away/home stay symmetric).
    """
    t = (token or "").strip().lower()
    if not t:
        return t
    if t in MLB_TEAM_ABBR:
        return MLB_TEAM_ABBR[t]
    canon = ballparks._canon(t)          # abbrev aliases (chw->cws, az->ari, ...)
    if ballparks.park_for(canon):
        return canon
    last = t.replace(".", "").split()[-1] if t.split() else t   # "n.y. yankees" -> "yankees"
    if last in MLB_TEAM_ABBR:
        return MLB_TEAM_ABBR[last]
    return canon


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
    # Normalize team identifiers to Polymarket abbreviations so a sharp game keyed
    # from "Chicago Cubs"/"CHC" matches a Polymarket slug game keyed from "chc".
    return (date, frozenset((normalize_team(away), normalize_team(home))))


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


def _commence_utc(ev: dict) -> datetime | None:
    ts = ev.get("commence_time") or ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def oddsapi_rows(events: list, book: str = "pinnacle", now: datetime | None = None) -> list[dict]:
    """Extract one MAIN totals row per event from a The Odds API /odds response.

    Returns [{date, away, home, line, over_fair, under_fair, book, n_lines}] with team
    names PRESERVED (unlike the frozenset lookup). A totals market can bundle several
    points (alternate lines); the MAIN line is the one whose Over price is closest to even
    (0.5 implied) — the balanced-juice line. Picking that, rather than the last outcome
    seen, avoids grabbing a stray alternate line.

    When `now` (UTC) is given, events whose game has already started are SKIPPED: once a
    game is live The Odds API serves the LIVE total (remaining runs), not the pregame line,
    which would otherwise pollute the sharp anchor (e.g. a Coors game showing a total of 2.5).
    """
    rows: list[dict] = []
    for ev in events or []:
        home, away = (ev.get("home_team") or ""), (ev.get("away_team") or "")
        date = (ev.get("commence_time") or "")[:10]
        if now is not None:
            ct = _commence_utc(ev)
            if ct is not None and ct <= now:
                continue   # in-progress/finished -> live total, not the pregame line
        books = ev.get("bookmakers") or []
        chosen = next((b for b in books if b.get("key") == book), None) or (books[0] if books else None)
        if not chosen or not home or not away:
            continue
        mk = next((m for m in chosen.get("markets", []) if m.get("key") == "totals"), None)
        if not mk:
            continue
        by_point: dict = {}
        for o in mk.get("outcomes", []):
            name = (o.get("name") or "").lower()
            pt = o.get("point")
            if pt is None or name not in ("over", "under"):
                continue
            by_point.setdefault(pt, {})[name] = american_to_implied(o.get("price"))
        complete = {pt: v for pt, v in by_point.items()
                    if v.get("over") and v.get("under")}
        if not complete:
            continue
        main_pt = min(complete, key=lambda pt: abs(complete[pt]["over"] - 0.5))
        fair = devig(complete[main_pt]["over"], complete[main_pt]["under"])
        if fair:
            rows.append({"date": date, "away": away, "home": home, "line": float(main_pt),
                         "over_fair": fair[0], "under_fair": fair[1],
                         "book": chosen.get("key"), "n_lines": len(complete)})
    return rows


def parse_oddsapi(events: list, book: str = "pinnacle", vlog=None, now: datetime | None = None) -> dict:
    """Parse a The Odds API /odds response into {(date,{teams}): {line,over_fair,under_fair}}.

    Pure: pass the decoded JSON list. Prefers `book` (Pinnacle); falls back to the first
    bookmaker that prices a totals market. Devigs the MAIN Over/Under to fair probabilities.
    Pass `vlog` to surface duplicate-game collisions (e.g. a doubleheader maps both games to
    the same (date, team-set) key, so the second silently overwrites the first). Pass `now`
    to drop in-progress games (live totals) — see oddsapi_rows.
    """
    out: dict = {}
    for r in oddsapi_rows(events, book, now=now):
        k = _key(r["date"], r["away"], r["home"])
        if k in out and vlog:
            vlog(f"  [odds-api] ⚠️ duplicate game {r['away']} @ {r['home']} {r['date']} "
                 f"(doubleheader?) — line {out[k]['line']:g} overwritten by {r['line']:g}; "
                 f"the (date, team-set) key can't hold both")
        out[k] = {"line": r["line"], "over_fair": r["over_fair"], "under_fair": r["under_fair"]}
    return out


def _mask_key(key: str | None) -> str:
    """A non-revealing fingerprint of an API key for logs (never the key itself)."""
    if not key:
        return "(none)"
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}…{key[-4:]} (len={len(key)})"


def fetch_sharp(api_key: str | None, date: str, book: str = "pinnacle",
                timeout: int = 10, vlog=None) -> dict:
    """Best-effort sharp totals for a date via The Odds API. {} on any failure.

    Pass `vlog` (a print-like callback) to trace key resolution and the HTTP call
    (source, masked key fingerprint, status, quota headers, event/parse counts, and
    any error). The API key is NEVER logged in full — only a masked fingerprint
    (CLAUDE.md: never echo secrets).
    """
    vlog = vlog or (lambda *a, **k: None)
    source = ("argument (--odds-api-key)" if api_key
              else "env $ODDS_API_KEY" if os.environ.get("ODDS_API_KEY") else "none")
    api_key = api_key or os.environ.get("ODDS_API_KEY")
    vlog(f"  [odds-api] key source: {source}; key {_mask_key(api_key)}")
    if not api_key:
        vlog("  [odds-api] no API key found — set $ODDS_API_KEY or pass --odds-api-key; "
             "skipping live sharp fetch")
        return {}
    try:
        import requests  # lazy
        resp = requests.get(ODDS_API, params={
            "apiKey": api_key, "regions": "us,eu", "markets": "totals",
            "oddsFormat": "american", "bookmakers": book}, timeout=timeout)
        # The Odds API reports quota in headers — invaluable for confirming the key works.
        used, remaining = resp.headers.get("x-requests-used"), resp.headers.get("x-requests-remaining")
        vlog(f"  [odds-api] GET {ODDS_API} (book={book}) -> HTTP {resp.status_code} "
             f"(quota: used={used} remaining={remaining})")
        if resp.status_code >= 400:
            # 401=bad key, 422=bad params, 429=quota exhausted. Body carries the reason.
            vlog(f"  [odds-api] error body: {_redact((resp.text or '')[:200])}")
        resp.raise_for_status()
        events = resp.json()
    except Exception as e:  # noqa: BLE001
        vlog(f"  [odds-api] request FAILED: {type(e).__name__}: {_redact(e)}")
        return {}
    n_events = len(events) if isinstance(events, list) else 0
    now = datetime.now(timezone.utc)
    parsed = parse_oddsapi(events, book, vlog=vlog, now=now)   # drops live games + flags dups
    dated = {k: v for k, v in parsed.items() if k[0] == date}
    n_live = len(oddsapi_rows(events, book)) - len(oddsapi_rows(events, book, now=now))
    vlog(f"  [odds-api] response: {n_events} event(s); {len(parsed)} pregame with a totals "
         f"line; {len(dated)} dated {date}"
         + (f"; skipped {n_live} in-progress game(s) (live total, not pregame)" if n_live else ""))
    # Dump the raw pregame sharp line per game so a bad line is visible/auditable at source.
    rows = oddsapi_rows(events, book, now=now)
    for r in sorted((x for x in rows if x["date"] == date) or rows,
                    key=lambda x: (x["away"], x["home"])):
        multi = f" ⚠️ {r['n_lines']} lines bundled — picked the balanced one" if r["n_lines"] > 1 else ""
        vlog(f"  [odds-api] {r['away']} @ {r['home']} {r['date']}: line={r['line']:g} "
             f"over_fair={r['over_fair']:.3f} (book={r['book']}){multi}")
    if n_events == 0:
        vlog("  [odds-api] ⚠️ 0 events returned — verify the key is valid/active and that "
             "games are scheduled (off-season/no-slate days return empty)")
    elif not dated:
        vlog(f"  [odds-api] ⚠️ {n_events} event(s) but none dated {date} — the slate may be "
             "for another day (timezone); using all parsed games as a fallback")
    return dated or parsed


def sharp_over_prob(lookup: dict, date: str, away: str, home: str, line: float | None = None,
                    use_close: bool = False) -> float | None:
    """Resolve the sharp fair P(Over) for a game from a lookup, or None.

    use_close pulls the CLOSING fair prob (for CLV); else the entry/open fair prob.
    Line is matched leniently (sharp line within 0.5 of ours), since books may differ.
    Used by CLV scoring, which compares at the bet's own line.
    """
    rec = lookup.get(_key(date, away, home))
    if not rec:
        return None
    if line is not None and rec.get("line") is not None and abs(float(rec["line"]) - float(line)) > 0.51:
        return None
    return rec.get("close_over_fair" if use_close else "over_fair") or rec.get("over_fair")


def sharp_ref(lookup: dict, date: str, away: str, home: str,
              use_close: bool = False) -> tuple[float, float] | None:
    """Return the sharp's own (line, fair P(over)) for a game, or None.

    Unlike sharp_over_prob (which gates on the bet line, for CLV), this returns the
    sharp's NATIVE line and fair P(over) at that line — with NO line-tolerance gate. The
    model anchors its expected total (mu) to the sharp line via that prob, then prices
    whatever Polymarket line is on offer off that mu. This makes the divergence detector
    robust to line drift: the sharp book's main line (e.g. 8.5) and Polymarket's possibly
    alternate line (e.g. 7.5 or 11.5) no longer have to match for the ref to attach.
    """
    rec = lookup.get(_key(date, away, home))
    if not rec:
        return None
    over = rec.get("close_over_fair" if use_close else "over_fair") or rec.get("over_fair")
    line = rec.get("line")
    if over is None or line is None:
        return None
    return float(line), float(over)
