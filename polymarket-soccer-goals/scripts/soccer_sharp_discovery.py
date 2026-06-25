#!/usr/bin/env python3
"""Sharp-source-driven discovery of the day's soccer goals/BTTS markets.

Why this exists — the same coverage bug confirmed for MLB. The soccer Gamma tags are NOT
honored: Gamma returns the global volume-ranked mix and pagination is capped (HTTP 422 past
offset ~2100). During a busy slate (e.g. a World Cup) the high-volume games fill the top and
low-volume leagues (Brazilian Série B, etc.) fall past the cut — they are never discovered, so
the model never sees them even though Polymarket may list them.

This inverts the flow: the SHARP slate (The Odds API) carries the FULL daily card, so it is the
authoritative game list. For each sharp game NOT already discovered we fetch its Polymarket
markets directly by event slug, bypassing the volume rank. A game on Polymarket is recovered;
a game truly absent (or with no goals market) is logged explicitly — so the output finally
distinguishes "truncated by the offset cap" from "Polymarket doesn't list it".

Slug construction is the hard part (soccer has many leagues and non-standard abbreviations), so
it is pure and offline-tested; the Gamma fetch is best-effort and returns [] offline.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  (wires sys.path for the reused category-watcher module)

import leagues
import soccer_market as sm
from category_common import GAMMA_API, parse_market
from sharp_odds_soccer import norm_name


def _is_goals_market(market: dict) -> bool:
    """True if a recovered market is a total-goals or BTTS market (what the model prices)."""
    slug = (market.get("slug") or "").lower()
    return bool(sm.GAME_TOTAL_RE.search(slug) or sm.GAME_BTTS_RE.search(slug))

# National-team -> Polymarket/FIFA 3-letter code. Polymarket writes World Cup / international
# slugs with these codes (fifwc-ecu-ger-…), which are NOT simple truncations of the name
# (netherlands->nld, not 'net'). Club slugs, by contrast, tend to use a short prefix of the
# club name (cuiaba->cui), handled by the generic token forms below.
NATIONAL_CODES = {
    "argentina": "arg", "australia": "aus", "austria": "aut", "belgium": "bel",
    "bolivia": "bol", "brazil": "bra", "cameroon": "cmr", "canada": "can", "chile": "chi",
    "china": "chn", "colombia": "col", "costa rica": "crc", "croatia": "cro",
    "cote divoire": "civ", "ivory coast": "civ", "czech republic": "cze", "czechia": "cze",
    "denmark": "den", "ecuador": "ecu", "egypt": "egy", "england": "eng", "france": "fra",
    "germany": "ger", "ghana": "gha", "greece": "gre", "iran": "irn", "italy": "ita",
    "japan": "jpn", "mexico": "mex", "morocco": "mar", "netherlands": "nld",
    "new zealand": "nzl", "nigeria": "nga", "norway": "nor", "paraguay": "par", "peru": "per",
    "poland": "pol", "portugal": "por", "qatar": "qat", "saudi arabia": "ksa",
    "scotland": "sco", "senegal": "sen", "serbia": "srb", "south korea": "kor",
    "korea republic": "kor", "spain": "esp", "sweden": "swe", "switzerland": "sui",
    "tunisia": "tun", "turkey": "tur", "turkiye": "tur", "ukraine": "ukr",
    "united states": "usa", "usa": "usa", "uruguay": "uru", "wales": "wal",
}

# The Odds API sport key -> Polymarket slug prefix. The sharp record carries its odds-api
# league (threaded by sharp_odds_soccer); we map it to the prefix Polymarket uses for slugs.
# Falls back to slugifying the key's tail when unmapped (best-effort).
ODDSAPI_TO_PREFIX = {
    "soccer_fifa_world_cup": "fifwc",
    "soccer_uefa_european_championship": "euro",
    "soccer_uefa_nations_league": "nations-league",
    "soccer_conmebol_copa_america": "copa",
    "soccer_conmebol_copa_libertadores": "libertadores",
    "soccer_conmebol_copa_sudamericana": "sudamericana",
    "soccer_uefa_champs_league": "ucl",
    "soccer_uefa_europa_league": "uel",
    "soccer_uefa_europa_conference_league": "uecl",
    "soccer_epl": "epl",
    "soccer_england_efl_champ": "elc",
    "soccer_england_league1": "eng1",
    "soccer_england_league2": "eng2",
    "soccer_spain_la_liga": "laliga",
    "soccer_spain_segunda_division": "es2",
    "soccer_italy_serie_a": "sea",
    "soccer_italy_serie_b": "it2",
    "soccer_germany_bundesliga": "bundesliga",
    "soccer_germany_bundesliga2": "ger2",
    "soccer_france_ligue_one": "ligue-1",
    "soccer_france_ligue_two": "fr2",
    "soccer_netherlands_eredivisie": "eredivisie",
    "soccer_portugal_primeira_liga": "por",
    "soccer_usa_mls": "mls",
    "soccer_mexico_ligamx": "liga-mx",
    "soccer_brazil_campeonato": "bra",
    "soccer_brazil_serie_b": "bra2",
    "soccer_argentina_primera_division": "argentina",
    "soccer_chile_campeonato": "chile",
    "soccer_china_superleague": "csl",
    "soccer_japan_j_league": "j-league",
    "soccer_korea_kleague1": "k-league",
    "soccer_spl": "saudi",
    "soccer_sweden_allsvenskan": "allsvenskan",
    "soccer_sweden_superettan": "superettan",
    "soccer_norway_eliteserien": "eliteserien",
    "soccer_turkey_super_league": "super-lig",
    "soccer_finland_veikkausliiga": "veikkausliiga",
    "soccer_league_of_ireland": "ireland",
}

_MAX_TOKENS = 3        # token forms tried per team
_MAX_CANDIDATES = 14   # hard cap on slugs fetched per game (bounds API calls)


def prefix_for_league(league_key: str | None) -> str | None:
    """Polymarket slug prefix for an odds-api sport key, or a best-effort slugified tail."""
    if not league_key:
        return None
    if league_key in ODDSAPI_TO_PREFIX:
        return ODDSAPI_TO_PREFIX[league_key]
    tail = league_key.replace("soccer_", "").replace("_", "-")
    return tail or None


def team_tokens(name: str) -> list[str]:
    """Ordered, unique candidate slug tokens for a team name (already norm_name'd upstream).

    National teams resolve via their FIFA code first (Polymarket's international slugs use it);
    clubs fall back to a 3-letter prefix and the collapsed full name (Polymarket club slugs are
    usually a short prefix of the name, e.g. cuiaba->cui).
    """
    n = norm_name(name)
    if not n:
        return []
    collapsed = n.replace(" ", "")
    out: list[str] = []
    if n in NATIONAL_CODES:
        out.append(NATIONAL_CODES[n])
    out.append(collapsed[:3])          # short prefix (clubs)
    out.append(collapsed)              # full collapsed name
    out.append(n.split(" ")[0])        # first word (multi-word clubs)
    seen: set[str] = set()
    uniq = []
    for t in out:
        if t and t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq[:_MAX_TOKENS]


def candidate_event_slugs(prefix: str, a: str, b: str, date: str) -> list[str]:
    """Bounded, de-duplicated candidate slugs for a game (home/away order unknown -> both)."""
    if not prefix:
        return []
    ta, tb = team_tokens(a), team_tokens(b)
    slugs: list[str] = []
    for x in ta:
        for y in tb:
            slugs.append(f"{prefix}-{x}-{y}-{date}")
            slugs.append(f"{prefix}-{y}-{x}-{date}")
    return list(dict.fromkeys(slugs))[:_MAX_CANDIDATES]


# Standard soccer goals/BTTS market slug suffixes hung off a base game slug. Polymarket
# splits these out as their OWN markets (NOT always nested under the base event's markets
# array), so they must be fetched by explicit slug. Totals use half-lines (…-total-2pt5).
_TOTAL_LINE_SUFFIXES = tuple(f"total-{i}pt5" for i in range(0, 7))   # 0.5 … 6.5
_BTTS_SUFFIXES = ("btts", "both-teams-to-score")


def goals_market_slugs(base_slug: str) -> list[str]:
    """Explicit total-goals + BTTS market slugs for a base game slug (…-total-Xpt5 / …-btts)."""
    return ([f"{base_slug}-{s}" for s in _TOTAL_LINE_SUFFIXES]
            + [f"{base_slug}-{s}" for s in _BTTS_SUFFIXES])


def _parse_rows(rows, fallback_slug: str, category_key: str) -> list[dict]:
    out: list[dict] = []
    for m in (rows or []):
        if not isinstance(m, dict):
            continue
        # Group each market by its OWN slug — a total/BTTS market slug encodes the type
        # (…-total-2pt5 / …-btts) that downstream classification keys off.
        m["eventSlug"] = m.get("slug") or fallback_slug
        out.append(parse_market(m, category_key))
    return out


def fetch_event_markets(api, event_slug: str, category_key: str = "soccer") -> list[dict]:
    """Parsed markets nested under a Polymarket event slug via Gamma /events. [] on miss."""
    try:
        events = api.get(f"{GAMMA_API}/events", params={"slug": event_slug})
    except Exception:  # noqa: BLE001 - discovery is best-effort
        return []
    if not isinstance(events, list) or not events:
        return []
    out: list[dict] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        out += _parse_rows(ev.get("markets"), ev.get("slug") or event_slug, category_key)
    return out


def fetch_markets_by_slug(api, market_slug: str, category_key: str = "soccer") -> list[dict]:
    """Parsed market(s) for an exact market slug via Gamma /markets?slug=. [] on miss.

    The base event often nests only moneyline/spreads, so the goals/BTTS markets — separate
    Polymarket markets keyed by their own slug — must be fetched this way.
    """
    try:
        rows = api.get(f"{GAMMA_API}/markets", params={"slug": market_slug})
    except Exception:  # noqa: BLE001 - discovery is best-effort
        return []
    if not isinstance(rows, list):
        return []
    return _parse_rows(rows, market_slug, category_key)


def backfill_goals_markets(api, base_slugs, *, vlog=None, max_games: int = 40) -> list[dict]:
    """Fetch total-goals/BTTS markets by slug for DISCOVERED games missing them.

    A game's moneyline can surface via the volume-ranked tag while its total/BTTS markets
    (separate, lower-volume slugs) get truncated past the offset cap. Unlike `discover_from_sharp`
    (driven by the sharp slate), this is driven by the games already discovered — so it recovers
    goals markets for ANY league, including ones the sharp feed doesn't cover (e.g. Morocco
    Botola). Each game is cheaply existence-probed (the 2.5 line or BTTS) before fetching all
    lines, so moneyline-only games cost ~2 calls. Returns parsed goals markets; [] if none.
    """
    vlog = vlog or (lambda *a, **k: None)
    bases = list(dict.fromkeys(s for s in base_slugs if s))
    if not bases:
        return []
    capped = bases[:max_games]
    markets: list[dict] = []
    recovered = 0
    for base in capped:
        probe = (fetch_markets_by_slug(api, f"{base}-total-2pt5")
                 or fetch_markets_by_slug(api, f"{base}-btts"))
        if not probe:                        # no goals market on Polymarket (or wrong base)
            continue
        found = [m for gs in goals_market_slugs(base)
                 for m in fetch_markets_by_slug(api, gs) if _is_goals_market(m)]
        if found:
            recovered += 1
            markets.extend(found)
            vlog(f"  [goals-backfill] {base}: +{len(found)} goals/BTTS market(s)")
    note = f" ({len(bases) - len(capped)} more not probed, cap {max_games})" if len(bases) > max_games else ""
    vlog(f"  [goals-backfill] probed {len(capped)} game(s) missing goals markets, "
         f"recovered {recovered}{note}")
    return markets


def _games_from_lookup(sharp_lookup: dict, target: str):
    """Yield (teams_frozenset, sorted_team_list, league_key) for each sharp game dated target."""
    for key, rec in sharp_lookup.items():
        try:
            date, teams = key
        except (TypeError, ValueError):
            continue
        if date != target:
            continue
        ts = sorted(t for t in teams if t)
        if len(ts) == 2:
            league = rec.get("league") if isinstance(rec, dict) else None
            yield frozenset(teams), ts, league


def discover_from_sharp(api, sharp_lookup: dict, target: str,
                        existing_team_sets: set | None = None, *, vlog=None) -> list[dict]:
    """Recover sharp games the volume-ranked tag truncated by fetching them by event slug.

    Only games NOT already in `existing_team_sets` (the discovered Polymarket events) are
    probed, so we spend API calls only on the gap. Returns parsed markets (discover_markets
    shape). Per game it logs RECOVERED (with the winning slug) or NOT-FOUND (with the slugs
    tried) — turning the silent coverage gap into an auditable truncated-vs-absent signal.
    """
    vlog = vlog or (lambda *a, **k: None)
    existing = existing_team_sets or set()
    games = [g for g in _games_from_lookup(sharp_lookup, target) if g[0] not in existing]
    if not games:
        return []
    vlog(f"  [sharp-discovery] probing {len(games)} sharp game(s) the tag missed ...")
    markets: list[dict] = []
    seen_slugs: set[str] = set()
    found_event = 0          # event exists on Polymarket
    with_goals = 0           # ...and it actually carries a total-goals/BTTS market
    for _teams, (a, b), league in games:
        prefix = prefix_for_league(league)
        cands = candidate_event_slugs(prefix, a, b, target)
        hit = None
        ev_markets: list[dict] = []
        for slug in cands:
            ev_markets = fetch_event_markets(api, slug)
            if ev_markets:
                hit = slug
                break
        if not hit:
            tried = f" (prefix={prefix or '?'}, tried {len(cands)})" if cands else " (no prefix)"
            vlog(f"  [sharp-discovery] {a} v {b}: NOT FOUND on Polymarket{tried}")
            continue
        found_event += 1
        # The base event usually nests only moneyline/spreads — fetch the goals/BTTS markets
        # explicitly by their own slugs (…-total-Xpt5 / …-btts), or they'd be missed.
        ev_markets = ev_markets + [m for gs in goals_market_slugs(hit)
                                   for m in fetch_markets_by_slug(api, gs)]
        new = goals = 0
        for m in ev_markets:
            slug = m.get("slug") or ""
            if slug and slug in seen_slugs:
                continue
            if slug:
                seen_slugs.add(slug)
            markets.append(m)
            new += 1
            if _is_goals_market(m):
                goals += 1
        if goals:
            with_goals += 1
            vlog(f"  [sharp-discovery] {a} v {b}: RECOVERED {goals} goals/BTTS market(s) "
                 f"(+{new - goals} other) via {hit}")
        else:
            # The event exists but Polymarket lists NO goals/BTTS market for it (typically
            # moneyline-only on lower leagues) — the goals model has nothing to price here.
            vlog(f"  [sharp-discovery] {a} v {b}: event found ({hit}) but has NO goals/BTTS "
                 f"market — Polymarket lists only {new} other market(s); goals model can't price it")
    vlog(f"  [sharp-discovery] {found_event}/{len(games)} event(s) found, {with_goals} with a "
         f"goals/BTTS market ({len(markets)} market(s) added)")
    return markets
