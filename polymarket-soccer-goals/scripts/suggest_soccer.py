#!/usr/bin/env python3
"""Suggest Over/Under TOTAL-GOALS and BTTS entries for the day's soccer games.

Pipeline: today's soccer games on Polymarket (via the category scanner) -> for
each full-game total-goals or BTTS market -> Dixon-Coles model of P(Over)/P(BTTS)
-> edge vs the Polymarket price -> 1.50x-3.0x payout filter -> pre-game decision
tree -> half-Kelly capped per CLAUDE.md -> recommendation(s). Records each
prediction (status PENDENTE) for later analysis.

Paper-first; read/analysis only. Not financial advice.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import _bootstrap  # noqa: F401

import dixon_coles as dc
import forecast_soccer as fcs
import congruence as cg
import leagues
import soccer_sharp_discovery
import soccer_market as sm
import data_inputs
import sharp_odds_soccer as sosh
import baselines_source as bsrc
import soccer_predictions as spdb

from category_common import (
    APIClient, discover_markets, fetch_midpoint, game_date, log, sanitize_text,
)

STRATEGY = "soccer-goals-dc"
CAP_MODEL = 0.02
CAP_FIRST_TRADE = 0.01

# On a near-efficient goals market (e.g. a liquid World Cup total/BTTS), an edge this large
# signals MODEL error — the Elo-derived λ over/under-shooting — not real value. Such a side
# is flagged implausible and skipped, exactly like the MLB model's MAX_PLAUSIBLE_EDGE. This
# is a blunt safety net until the model is anchored to a sharp reference (divergence detector).
MAX_PLAUSIBLE_EDGE = 0.15

# A date-anchored game key: the slug up to and including YYYY-MM-DD. Collapses every
# market type of a game (total/BTTS/spread/double-chance/correct-score/…) to one game.
_GAME_DATE_RE = re.compile(r"^(.*?-\d{4}-\d{2}-\d{2})")


def _load_sharp_lookup(args, target, vlog) -> dict:
    """Load the sharp reference (Pinnacle totals + BTTS via The Odds API) for the day.

    With it the model becomes a divergence detector (anchor to sharp; edge = Polymarket
    vs sharp); without it the model stays predictive (Elo), protected only by the cap.
    Lists the active soccer leagues, then queries each. Best-effort / {} offline.
    """
    if getattr(args, "no_sharp", False):
        return {}
    key = getattr(args, "odds_api_key", None) or os.environ.get("ODDS_API_KEY")
    if not key:
        vlog("  sharp source: none (no ODDS_API_KEY) -> predictive model (edge-capped)")
        return {}
    keys = sosh.fetch_active_soccer_keys(key, vlog=vlog)
    only = getattr(args, "sharp_leagues", None)
    if only:
        wanted = {x.strip().lower() for x in only.split(",") if x.strip()}
        keys = [k for k in keys if any(w in k.lower() for w in wanted)]
        vlog(f"  sharp leagues filtered to {only!r}: {len(keys)} key(s)")
    lookup = sosh.fetch_sharp_soccer(key, keys, date=target,
                                     with_btts=getattr(args, "sharp_btts", True),
                                     book=getattr(args, "sharp_book", None) or "pinnacle,betfair_ex_eu",
                                     min_quota_reserve=int(getattr(args, "sharp_min_reserve", 0) or 0),
                                     vlog=vlog)
    if lookup:
        # k = (date, frozenset(teams)); count how many are dated for today's slate so a
        # date-boundary mismatch (a late kickoff crossing UTC midnight) is visible in the log.
        dated = sum(1 for k in lookup if k[0] == target)
        vlog(f"  sharp reference loaded: {len(lookup)} game(s) "
             f"({dated} dated {target}) (divergence-detector mode)")
    else:
        vlog("  sharp reference EMPTY — divergence detector OFF "
             "(model stays predictive/Elo, edge-capped). Check the [odds-api] lines above: "
             "0 leagues, quota reserve hit, or no games parsed.")
    return lookup


def _game_names(gmarkets) -> tuple[str, str] | None:
    """Normalized (team_a, team_b) for a game, parsed from any of its market questions."""
    for x in gmarkets or []:
        t = sosh.extract_teams_from_question(x.get("question") or "")
        if t:
            return t
    return None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def date_window_params(target: str) -> dict | None:
    """Gamma date-filter params bracketing the target day (±a small margin).

    Discovery is ranked by 24h volume and capped at an offset, so low-volume
    markets (e.g. Série B) are lost in the tail. Filtering by date instead shrinks
    the candidate set to the day's games, where volume ranking no longer matters.
    """
    try:
        d = datetime.strptime(target, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return {"start_date_min": (d - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z"),
            "end_date_max": (d + timedelta(days=2)).strftime("%Y-%m-%dT00:00:00Z")}


def fee_for(price: float, fee_rate: float) -> float:
    return 0.0 if (not fee_rate or price is None) else fee_rate * min(price, 1.0 - price)


def group_by_event(markets: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for m in markets:
        key = m.get("event_slug") or m.get("slug")
        if key:
            out.setdefault(key, []).append(m)
    return out


# Real Polymarket soccer tag slugs (= the leagues' URL paths). The generic "soccer"/
# "football" tags are NOT real Gamma tags, so Gamma ignores them and returns ALL
# categories (esports, etc.) volume-ranked — burying the actual games past the offset
# cap. Querying each real league tag keeps every page on-topic.
SOCCER_TAGS = sorted(set(leagues.LEAGUE_URL_PATH.values()))

# Discovery tuning. A REAL (honored) league tag returns a small, ~100%-soccer set; an
# UNKNOWN tag makes Gamma return the global volume-ranked mix (mostly non-soccer). We
# probe one page to tell them apart, fully paginate honored tags, and never deep-paginate
# the global mix (which only re-fetches the same markets 50x and starves the offset cap).
PROBE_MAX = 100             # one page, to classify a tag honored vs global-mix
PER_TAG_MAX = 600           # full pull for an honored league tag
DEEP_MAX = 2500             # fallback broad pass when no tag is honored
HONORED_FRAC = 0.60         # >= this soccer fraction in the probe => honored tag


def _is_soccer(m) -> bool:
    return leagues.is_soccer_slug(m.get("event_slug") or m.get("slug") or "")


def discover_soccer(api, vlog) -> list[dict]:
    """Union the day's soccer markets across the league tags (deduped, soccer-only).

    Per tag: probe one page and measure its soccer fraction. Honored tags (~all soccer)
    are paginated in full; tags that return the global mix are NOT deep-paginated (their
    soccer subset from the probe is still kept, deduped). If Gamma honors none, fall back
    to one DEEP broad pass so low-volume leagues aren't lost behind a 1-page probe.
    """
    markets, seen = [], set()
    honored, mixed = [], []

    def _add(ms) -> int:
        n = 0
        for m in ms:
            if not _is_soccer(m):
                continue
            key = m.get("condition_id") or m.get("slug")
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            markets.append(m)
            n += 1
        return n

    for tag in SOCCER_TAGS:
        try:
            _t, probe = discover_markets(api, "soccer", [tag], min_volume=0.0,
                                         max_markets=PROBE_MAX, include_closed=False)
        except Exception as e:  # noqa: BLE001
            vlog(f"  tag {tag!r} failed: {e}")
            continue
        if not probe:
            continue
        frac = sum(1 for m in probe if _is_soccer(m)) / len(probe)
        if frac >= HONORED_FRAC:
            try:
                _t, full = discover_markets(api, "soccer", [tag], min_volume=0.0,
                                            max_markets=PER_TAG_MAX, include_closed=False)
            except Exception:  # noqa: BLE001
                full = probe
            added = _add(full)
            honored.append(tag)
            vlog(f"  tag {tag!r}: HONORED +{added} soccer (probe {frac:.0%})")
        else:
            _add(probe)            # global mix: keep its soccer subset, don't paginate deeper
            mixed.append(tag)

    if mixed:
        vlog(f"  {len(honored)} honored tag(s); {len(mixed)} returned the global mix")
    if not honored:
        # Gamma honored no tag -> one deep broad pass so low-volume leagues aren't lost.
        try:
            _t, deep = discover_markets(api, "soccer", ["soccer"], min_volume=0.0,
                                        max_markets=DEEP_MAX, include_closed=False)
            added = _add(deep)
            vlog(f"  no honored tags -> deep broad pass: +{added} soccer "
                 f"(of {len(deep)} scanned)")
        except Exception as e:  # noqa: BLE001
            vlog(f"  deep broad pass failed: {e}")
    return markets


def derive_total_supremacy(inputs: dict, baseline: float, neutral: bool):
    """Return (total, supremacy, used_external) from data inputs, or (None,None,False)."""
    if "total_xg" in inputs and "supremacy_xg" in inputs:
        return float(inputs["total_xg"]), float(inputs["supremacy_xg"]), True
    used = False
    total = dc.adjust_total(baseline, att_home=inputs.get("att_home"),
                            att_away=inputs.get("att_away"), def_home=inputs.get("def_home"),
                            def_away=inputs.get("def_away"))
    if any(k in inputs for k in ("att_home", "att_away", "def_home", "def_away")):
        used = True
    supremacy = 0.0
    if "home_elo" in inputs and "away_elo" in inputs:
        supremacy = dc.supremacy_from_elo(inputs["home_elo"], inputs["away_elo"],
                                          home_adv_elo=0.0 if neutral else 65.0)
        used = True
    if not used:
        return None, None, False
    return total, supremacy, True


def pick_side(sides, fee_rate, odds_min, odds_max, max_edge=MAX_PLAUSIBLE_EDGE):
    """sides = [(name, token, price, p_model)]. Return (chosen|None, notes).

    A side whose post-fee edge exceeds `max_edge` is flagged `implausible` and excluded —
    on a near-efficient goals market a huge edge signals model error, not value.
    """
    notes, candidates = [], []
    for name, token, price, p in sides:
        if price is None:
            continue
        edge = p - price - fee_for(price, fee_rate)
        in_band = dc.passes_odds_filter(price, odds_min, odds_max)
        implausible = edge > max_edge
        notes.append({"side": name, "price": round(price, 4), "p_model": round(p, 4),
                      "edge": round(edge, 4), "in_odds_band": in_band, "implausible": implausible})
        if edge > 0 and in_band and not implausible:
            candidates.append({"side": name, "token": token, "price": price, "p_model": p, "edge": edge})
    if not candidates:
        return None, notes
    candidates.sort(key=lambda c: c["edge"], reverse=True)
    return candidates[0], notes


def decision_tree(chosen, market, book_sum, price_sane, *, min_volume, min_edge,
                  min_hours, max_spread=0.10):
    vol = float(market.get("volume_24h") or 0)
    if vol < min_volume:
        return False, f"volume ${vol:,.0f}/24h < ${min_volume:,.0f}"
    spread = market.get("spread")
    if spread is None:
        spread = abs(1.0 - book_sum)
    if spread >= max_spread:
        return False, f"spread {spread:.1%} >= {max_spread:.0%}"
    end = market.get("end_date")
    if end:
        try:
            dt = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
            hours = (dt - now_utc()).total_seconds() / 3600.0
            if hours < min_hours:
                return (False, f"game already started ({-hours:.1f}h ago)" if hours < 0
                        else f"game starts in {hours:.1f}h < {min_hours:.0f}h")
        except ValueError:
            pass
    if market.get("accepting_orders") is False:
        return False, "not accepting orders"
    if chosen["edge"] < min_edge:
        return False, f"edge {chosen['edge']:.1%} < {min_edge:.0%} after fees"
    if not price_sane:
        return False, f"price sanity failed (book_sum={book_sum})"
    return True, None


def size_position(p_model, price, portfolio_value, first_trade, kelly_half):
    kelly = kelly_half(p_model, price, "YES")
    cap = CAP_FIRST_TRADE if first_trade else CAP_MODEL
    size_pct = min(kelly, cap)
    return size_pct, portfolio_value * size_pct, kelly


def advisor_kelly_half():
    from advisor import kelly_half
    return kelly_half


def run(args) -> dict:
    api = APIClient(rate_limit_ms=args.rate_limit, debug=args.debug)
    vlog = log if getattr(args, "verbose", True) else (lambda *a, **k: None)
    target = args.date or now_utc().date().isoformat()
    vlog(f"=== Soccer goals/BTTS analysis for {target} ===")

    try:
        # Per real league tag (world-cup, epl, bra2, …) and union: the generic
        # "soccer" tag isn't a real Gamma tag and returns all categories.
        markets = discover_soccer(api, vlog)
    except Exception as e:  # noqa: BLE001
        return {"date": target, "error": f"discovery failed: {e}", "suggestions": [], "skipped": []}

    on_day = [m for m in markets if game_date(m) == target]
    games = group_by_event(on_day)
    _tag = f"{len(SOCCER_TAGS)} league tags"
    vlog(f"Discovery: {_tag} -> {len(markets)} markets; {len(on_day)} dated {target}, {len(games)} events")

    # The Gamma tags are volume-ranked and truncated (offset-2100 cap), so low-volume leagues
    # never surface. Load the sharp slate now — it carries the FULL daily card — and fetch each
    # game the tag missed directly by event slug, unioning it in (sharp-driven discovery). Also
    # logs recovered-vs-not-found per game, distinguishing "truncated" from "not on Polymarket".
    sharp_lookup = _load_sharp_lookup(args, target, vlog)
    require_sharp = bool(sharp_lookup) and getattr(args, "require_sharp", True)
    if sharp_lookup and getattr(args, "sharp_discovery", True):
        existing_sets = {frozenset(t) for v in games.values() if (t := _game_names(v))}
        extra = soccer_sharp_discovery.discover_from_sharp(
            api, sharp_lookup, target, existing_sets, vlog=vlog)
        existing_slugs = {m.get("slug") for m in markets if m.get("slug")}
        added = [m for m in extra if m.get("slug") and m.get("slug") not in existing_slugs]
        if added:
            markets = markets + added
            on_day = [m for m in markets if game_date(m) == target]
            games = group_by_event(on_day)
            vlog(f"  sharp-driven discovery added {len(added)} market(s) the tag missed "
                 f"(total now {len(markets)}, {len(games)} events dated {target})")

    # Keep only soccer total-goals and BTTS markets. Gamma groups every market of a
    # game under one event_slug (no -total-/-btts suffix), so classify by the per-MARKET
    # slugs inside each event, not the event key — and stay goals-specific (the suffix
    # regex excludes corners/cards totals).
    def _has_market(gmarkets, slug_re) -> bool:
        return any(slug_re.search((x.get("slug") or "").lower()) for x in gmarkets)

    # Goals-market backfill (any league): a game's moneyline can surface while its total/BTTS
    # markets are truncated by the volume cap. For each soccer game dated today with an event but
    # NO goals market, fetch the goals slugs directly — recovers leagues OUTSIDE the sharp slate
    # (e.g. Morocco Botola), which sharp-driven discovery never probes.
    if getattr(args, "sharp_discovery", True):
        def _gk(slug):
            m = _GAME_DATE_RE.match(slug or "")
            return m.group(1) if m else (slug or "")
        soccer_games = {_gk(k): k for k in games if leagues.is_soccer_slug(k)}
        with_goals = {_gk(k) for k in games if leagues.is_soccer_slug(k)
                      and (_has_market(games[k], sm.GAME_TOTAL_RE)
                           or _has_market(games[k], sm.GAME_BTTS_RE))}
        missing = sorted(g for g in soccer_games if g not in with_goals)
        if missing:
            existing_slugs = {m.get("slug") for m in markets if m.get("slug")}
            bf = soccer_sharp_discovery.backfill_goals_markets(api, missing, vlog=vlog)
            added2 = [m for m in bf if m.get("slug") and m.get("slug") not in existing_slugs]
            if added2:
                markets = markets + added2
                on_day = [m for m in markets if game_date(m) == target]
                games = group_by_event(on_day)
                vlog(f"  goals-backfill added {len(added2)} market(s) (total now {len(markets)}, "
                     f"{len(games)} events dated {target})")

    total_evts = {k: v for k, v in games.items()
                  if leagues.is_soccer_slug(k) and _has_market(v, sm.GAME_TOTAL_RE)}
    btts_evts = {k: v for k, v in games.items()
                 if leagues.is_soccer_slug(k) and _has_market(v, sm.GAME_BTTS_RE)}
    # An event can carry BOTH a total and a BTTS market, so count distinct events
    # to avoid double-subtracting (which made "dropped" go negative).
    classified = set(total_evts) | set(btts_evts)
    filtered_non_soccer = len(games) - len(classified)
    vlog(f"  {len(total_evts)} total-goals + {len(btts_evts)} BTTS soccer markets "
         f"({filtered_non_soccer} other events dropped)")

    # Competition coverage diagnostic: per league prefix, how many DISTINCT games today
    # carry a total/BTTS market vs how many games appeared at all. Makes it visible whether
    # a day is genuinely one-competition (e.g. only `fifwc` during a World Cup) vs a league
    # present-but-without-goals-markets vs possible truncation (see the offset-cap note above).
    # Use a DATE-ANCHORED game key (everything up to YYYY-MM-DD) so non-goals market types
    # (-double-chance/-correct-score/-draw-no-bet/...) collapse to the same game instead of
    # inflating the denominator (base_game_slug only strips total/BTTS/spread suffixes).
    from collections import Counter
    def _game_key(slug):
        m = _GAME_DATE_RE.match(slug or "")
        return m.group(1) if m else (slug or "")
    def _comp(slug):
        return leagues.league_prefix(slug) or "?"
    all_games = {_game_key(k) for k in games}
    cls_games = {_game_key(k) for k in classified}
    all_by_comp, cls_by_comp = Counter(map(_comp, all_games)), Counter(map(_comp, cls_games))
    breakdown = ", ".join(f"{c}={cls_by_comp.get(c, 0)}/{n}"
                          for c, n in all_by_comp.most_common())
    vlog(f"  competitions dated {target} ({len(cls_games)}/{len(all_games)} games with "
         f"total|BTTS) -> {breakdown}")

    # Diagnostic: when nothing classifies, dump a sample of the real market shapes so
    # the slug/outcome format can be inspected from the logs (Gamma changes formats).
    if not classified and games:
        soccer_evts = [(k, v) for k, v in games.items() if leagues.is_soccer_slug(k)]
        vlog(f"  [diag] 0 classified; {len(soccer_evts)}/{len(games)} events pass "
             f"is_soccer_slug. Sample soccer events + their markets:")
        for k, v in soccer_evts[:8]:
            vlog(f"  [diag] event={k!r} markets={len(v)}")
            for x in v[:6]:
                vlog(f"  [diag]    slug={x.get('slug')!r} outcomes={x.get('outcomes')} "
                     f"q={(x.get('question') or '')[:70]!r}")

    portfolio_value = float(args.portfolio_value)
    first_trade = data_inputs.is_first_trade(STRATEGY, args.portfolio_db)
    ratings = data_inputs.load_ratings(args.ratings_csv) if args.ratings_csv else {}
    kh = advisor_kelly_half()

    # Calibrate league baselines from the live results feed (best-effort; falls back
    # to the static LEAGUE_BASELINES per league when unavailable). One req/league.
    calibrated = {}
    if getattr(args, "calibrate_baselines", True):
        prefixes = {leagues.league_prefix(k) for k in (set(total_evts) | set(btts_evts))}
        token = getattr(args, "football_data_token", None)
        calibrated = bsrc.calibrate_baselines(prefixes, token, date=target, debug=args.debug)
        if calibrated:
            vlog("  baselines calibrated: " + ", ".join(
                f"{p}={v:.2f}(was {leagues.LEAGUE_BASELINES.get(p, leagues.DEFAULT_BASELINE):.2f})"
                for p, v in sorted(calibrated.items())))


    # Cross-check the sharp slate against ALL discovered Polymarket events (not just goals
    # markets): tells whether the sharp's other-league games are on Polymarket without a
    # goals market, vs absent/truncated entirely. Matched by normalized team-set + date.
    if sharp_lookup:
        def _nk(gm):
            t = _game_names(gm)
            return frozenset(t) if t else None
        poly_all = {nk for v in games.values() if (nk := _nk(v))}
        poly_goals = {nk for k in classified if (nk := _nk(games[k]))}
        sharp_today = {teams for (d, teams) in sharp_lookup if d == target}
        on_poly, with_goals = sharp_today & poly_all, sharp_today & poly_goals
        not_on_poly = sharp_today - poly_all
        vlog(f"  sharp×Polymarket: {len(sharp_today)} sharp game(s) dated {target} -> "
             f"{len(on_poly)} on Polymarket ({len(with_goals)} with goals markets), "
             f"{len(not_on_poly)} NOT on Polymarket")
        if not_on_poly:
            sample = "; ".join(" v ".join(sorted(t)) for t in list(not_on_poly)[:10])
            vlog(f"    not on Polymarket (sample): {sample}")

    suggestions, skipped, cand_rows, analyses = [], [], [], []

    def _skip(slug, reason, **extra):
        rec = {"game": slug, "reason": reason}; rec.update(extra)
        skipped.append(rec); vlog(f"  [{slug}] SKIP — {reason}")

    def _inputs_for(slug, gmarkets):
        home, away = leagues.parse_teams(slug, home_first=args.home_first)
        # Full club names (from the market question) resolve strength by name across leagues.
        # Align each name to its slug abbr by prefix so home/away can't be swapped (sign-safe).
        names = _game_names(gmarkets) or ()
        def _pick(abbr):
            a = (abbr or "").lower()
            if not a:
                return None
            for nm in names:
                if (nm or "").lower().replace(" ", "").startswith(a):
                    return nm
            return None
        home_name = _pick(home) or (names[0] if len(names) > 0 else None)
        away_name = _pick(away) or (names[1] if len(names) > 1 else None)
        inp = data_inputs.get_match_inputs(api, home, away, leagues.league_prefix(slug),
                                           ratings=ratings, auto=args.auto_ratings,
                                           international=leagues.is_international(slug),
                                           date=target, debug=args.debug,
                                           home_name=home_name, away_name=away_name)
        total, sup, used = derive_total_supremacy(inp, bsrc.baseline_for(slug, calibrated),
                                                  leagues.is_neutral(slug))
        return inp, total, sup, used

    # --- TOTAL-GOALS markets ---
    for slug, gmarkets in total_evts.items():
        # All goals-total lines for this game (the event groups them); each is modeled,
        # then best-line-per-game keeps only the highest-edge one below.
        tmarkets = [x for x in gmarkets
                    if sm.GAME_TOTAL_RE.search((x.get("slug") or "").lower()) and sm.is_total_market(x)]
        if not tmarkets:
            _skip(slug, "no total-goals market"); continue
        _inp, total, sup, used = _inputs_for(slug, gmarkets)  # game-level inputs: compute once
        names = _game_names(gmarkets)
        sharp_tot = (sosh.sharp_total_ref(sharp_lookup, target, names[0], names[1])
                     if (sharp_lookup and names) else None)
        # In divergence mode a missing sharp ref BLOCKS the bet — but we still model the
        # game so the analysis output covers it (the model read, not just a skip reason).
        bet_blocked = require_sharp and sharp_tot is None
        if bet_blocked:
            _skip(slug, "no sharp total reference (divergence mode bets only on a sharp anchor)")
        for m in tmarkets:
            line = sm.parse_total_line(m)
            ou = sm.over_under_tokens(m)
            if line is None or not ou or ou["over_price"] is None or ou["under_price"] is None:
                if not bet_blocked:
                    _skip(slug, "could not parse total line/tokens")
                continue
            if sharp_tot is not None:
                # Pure divergence anchor: invert the sharp (line, P(over)) to an expected total
                # (mu) at the SHARP line, re-split by Elo supremacy, then price the POLYMARKET
                # line off that mu (robust to line drift between books).
                s_line, s_over = sharp_tot
                lh0, la0 = dc.market_implied_lambdas(s_line, s_over)
                lam_h, lam_a = dc.lambdas_from_total_supremacy(lh0 + la0, sup if used else 0.0)
            elif used:
                lam_h, lam_a = dc.lambdas_from_total_supremacy(total, sup)
            else:
                lam_h, lam_a = dc.market_implied_lambdas(line, ou["over_price"])
            probs = dc.prob_over(line, dc.score_matrix(lam_h, lam_a, args.rho))
            # Congruence: independent Dixon-Coles P(over) (from Elo supremacy) vs the sharp anchor.
            cong = dict(cg.NEUTRAL)
            if sharp_tot is not None and used:
                ml_h, ml_a = dc.lambdas_from_total_supremacy(total, sup)
                model_p_over = dc.prob_over(line, dc.score_matrix(ml_h, ml_a, args.rho))["p_over_eff"]
                cong = cg.assess(model_p_over, probs["p_over_eff"])
            chosen, notes = pick_side([("OVER", ou["over_token"], ou["over_price"], probs["p_over_eff"]),
                                       ("UNDER", ou["under_token"], ou["under_price"], probs["p_under_eff"])],
                                      args.fee_rate, args.odds_min, args.odds_max,
                                      getattr(args, "max_edge", MAX_PLAUSIBLE_EDGE))
            vlog(f"  [{slug}] model(TOTAL {line}): λh={lam_h:.2f} λa={lam_a:.2f} ρ={args.rho} "
                 f"P(over)={probs['p_over_eff']:.3f} P(under)={probs['p_under_eff']:.3f} external={used}"
                 + (f" sharp_over={sharp_tot[1]:.3f}@{sharp_tot[0]:g}" if sharp_tot
                    else (" (no sharp ref)" if sharp_lookup else ""))
                 + (f" inputs={_inp}" if _inp else ""))
            vlog(f"  [{slug}] edges(TOTAL): " + "; ".join(
                f"{n['side']} price={n['price']} p={n['p_model']} edge={n['edge']:+.3f} "
                f"band={n['in_odds_band']}" for n in notes))
            analyses.append(_analysis_row("TOTAL", slug, names, line, lam_h, lam_a, used,
                                          probs, notes, sharp_tot, bet_blocked))
            if bet_blocked:
                continue   # analysis recorded; bet skipped (one skip per game logged above)
            c = _evaluate("TOTAL", slug, m, line, chosen, notes, lam_h, lam_a, used,
                          ou["book_sum"], ou["price_sane"], args, portfolio_value, first_trade, kh,
                          _skip, target, ref_token=ou.get("over_token"), cong=cong)
            if c:
                cand_rows.append(c)

    # --- BTTS markets ---
    for slug, gmarkets in btts_evts.items():
        m = next((x for x in gmarkets
                  if sm.GAME_BTTS_RE.search((x.get("slug") or "").lower()) and sm.is_btts_market(x)), None)
        if not m:
            _skip(slug, "no BTTS market"); continue
        bt = sm.btts_tokens(m)
        if not bt or bt["yes_price"] is None or bt["no_price"] is None:
            _skip(slug, "could not map BTTS tokens"); continue
        _inp, total, sup, used = _inputs_for(slug, gmarkets)
        names = _game_names(gmarkets)
        sharp_btts = (sosh.sharp_btts_ref(sharp_lookup, target, names[0], names[1])
                      if (sharp_lookup and names) else None)
        # Missing sharp ref blocks the BTTS bet, but we still model it for the analysis output.
        bet_blocked = require_sharp and sharp_btts is None
        if bet_blocked:
            _skip(slug, "no sharp BTTS reference (divergence mode bets only on a sharp anchor)")
        if used:
            lam_h, lam_a = dc.lambdas_from_total_supremacy(total, sup)
        else:
            lam_h, lam_a = dc.market_implied_from_btts(bt["yes_price"])
        probs = dc.prob_btts(dc.score_matrix(lam_h, lam_a, args.rho))
        model_p_yes = probs["p_yes"]             # independent Dixon-Coles P(BTTS) before any anchor
        cong = dict(cg.NEUTRAL)
        if sharp_btts is not None:
            cong = cg.assess(model_p_yes if used else None, sharp_btts)  # model vs sharp agreement
            probs = {"p_yes": sharp_btts, "p_no": 1.0 - sharp_btts}   # pure sharp anchor
        chosen, notes = pick_side([("YES", bt["yes_token"], bt["yes_price"], probs["p_yes"]),
                                   ("NO", bt["no_token"], bt["no_price"], probs["p_no"])],
                                  args.fee_rate, args.odds_min, args.odds_max,
                                  getattr(args, "max_edge", MAX_PLAUSIBLE_EDGE))
        vlog(f"  [{slug}] model(BTTS): λh={lam_h:.2f} λa={lam_a:.2f} ρ={args.rho} "
             f"P(yes)={probs['p_yes']:.3f} P(no)={probs['p_no']:.3f} external={used}"
             + (f" sharp_yes={sharp_btts:.3f}" if sharp_btts is not None
                else (" (no sharp ref)" if sharp_lookup else ""))
             + (f" inputs={_inp}" if _inp else ""))
        vlog(f"  [{slug}] edges(BTTS): " + "; ".join(
            f"{n['side']} price={n['price']} p={n['p_model']} edge={n['edge']:+.3f} "
            f"band={n['in_odds_band']}" for n in notes))
        analyses.append(_analysis_row("BTTS", slug, names, None, lam_h, lam_a, used,
                                      probs, notes, sharp_btts, bet_blocked))
        if bet_blocked:
            continue   # analysis recorded; bet skipped (one skip per game logged above)
        c = _evaluate("BTTS", slug, m, None, chosen, notes, lam_h, lam_a, used,
                      bt["book_sum"], bt["price_sane"], args, portfolio_value, first_trade, kh,
                      _skip, target, ref_token=bt.get("yes_token"), cong=cong)
        if c:
            cand_rows.append(c)

    # Best-line-per-game: record/score only the highest-edge line per (game, market); the
    # other passing lines are reported as skipped (not bet) so we never place correlated bets.
    groups: dict = {}
    for c in cand_rows:
        groups.setdefault((sm.GAME_TOTAL_RE.sub("", c["slug"]), c["market_type"]), []).append(c)
    final = []
    for cs in groups.values():
        cs.sort(key=lambda c: c["chosen"]["edge"], reverse=True)
        winners = cs[:1] if args.best_line_only else cs
        for c in (cs[1:] if args.best_line_only else []):
            _shadow_log(c["market_type"], c["slug"], c["line"], c["notes"], c["chosen"],
                        c["lam_h"], c["lam_a"], c["used"], args, target, 0,
                        "not best line for this game", c["ref_token"])
            _skip(c["slug"], "not best line for this game (best_line_only)",
                  market=c["market_type"], side=c["chosen"]["side"])
        final.extend(winners)

    recorded_ids: dict = {}
    for c in final:
        pred_id = _record_soccer(c, args, target)
        if pred_id is not None:
            recorded_ids.setdefault(c["slug"], set()).add(pred_id)
        _shadow_log(c["market_type"], c["slug"], c["line"], c["notes"], c["chosen"],
                    c["lam_h"], c["lam_a"], c["used"], args, target, 1, None, c["ref_token"])
        suggestions.append({"game": c["slug"], "market": c["market_type"],
                            "side": c["chosen"]["side"], "line": c["line"],
                            "edge": round(c["chosen"]["edge"], 4),
                            "lam_home": round(c["lam_h"], 3), "lam_away": round(c["lam_a"], 3),
                            "forecast": c["stats"].get("forecast"),
                            "congruence": c["stats"].get("congruence"),
                            "prediction_id": pred_id, "recommendation": c["rec"], "_text": c["text"]})

    # A re-run may pick a different best line per (game, market); void this game's
    # now-stale PENDENTE entries so it never carries two open positions per market.
    # keep_ids spans TOTAL+BTTS, so a stale total line won't void the BTTS bet.
    superseded = 0
    if args.record:
        for game_slug, keep in recorded_ids.items():
            n = spdb.supersede_pending(args.predictions_db, game_slug, keep)
            if n:
                superseded += n
                vlog(f"  [{game_slug}] superseded {n} stale PENDENTE entry(ies) from an earlier run")
    suggestions.sort(key=lambda s: s["edge"], reverse=True)

    # Mark which analyses became live suggestions (best-line winners) so the output makes the
    # bet/no-bet outcome explicit per game, alongside the model read for EVERY game found.
    bet_keys = {(s["game"], s["market"], s["line"]) for s in suggestions}
    for a in analyses:
        a["suggested"] = (a["slug"], a["market"], a["line"]) in bet_keys
    analyses.sort(key=lambda a: (a["best_edge"] is None, -(a["best_edge"] or 0)))

    texts = [s.pop("_text") for s in suggestions]

    # Full-slate analysis log: EVERY game found + its model read (not just bets/skips).
    vlog(f"=== Analysis of all {len(analyses)} game-market(s) found ===")
    for a in analyses:
        head = f"  [{a['game']}] {a['market']}" + (f" {a['line']:g}" if a["line"] is not None else "")
        teams = f"{a['home']} v {a['away']}" if a["home"] else "?"
        prob = (f"P(over)={a['p_over']:.3f}" if a["market"] == "TOTAL"
                else f"P(yes)={a['p_yes']:.3f}")
        sharp = (f" sharp={a['sharp_over']:.3f}@{a['sharp_line']:g}"
                 if a["market"] == "TOTAL" and a.get("sharp_over") is not None
                 else f" sharp_yes={a['sharp_yes']:.3f}"
                 if a["market"] == "BTTS" and a.get("sharp_yes") is not None else "")
        outcome = ("SUGGEST" if a["suggested"] else
                   "no-sharp" if a["bet_blocked_no_sharp"] else "no-bet")
        vlog(f"{head}: {teams} λ={a['lam_home']:g}/{a['lam_away']:g} {prob}{sharp} "
             f"best={a['best_side'] or '—'} edge={(a['best_edge'] or 0)*100:+.1f}% "
             f"ext={a['used_external']} -> {outcome}")
    vlog(f"=== Done: {len(analyses)} analyzed, {len(suggestions)} suggestion(s), "
         f"{len(skipped)} skipped ===")

    result = {
        "date": target,
        "counts": {"total_markets": len(total_evts), "btts_markets": len(btts_evts),
                   "analyzed": len(analyses),
                   "suggestions": len(suggestions), "skipped": len(skipped),
                   "superseded": superseded},
        "suggestions": suggestions, "skipped": skipped, "analyses": analyses,
        "disclaimer": "Paper-trading simulation — not financial advice. Without live "
                      "inputs the Dixon-Coles engine returns zero edge (market-implied).",
        "_texts": texts,
    }
    return result


def _analysis_row(market_type, slug, names, line, lam_h, lam_a, used, probs, notes, sharp,
                  bet_blocked):
    """One structured analysis record for a modeled game-market (bet or not).

    Returned in the result's `analyses` array so the output carries EVERY discovered
    game with its full model read — λ, P(over)/P(BTTS), per-side edges, sharp ref — not
    just the bets and skip reasons.
    """
    best = max((n for n in notes), key=lambda n: n["edge"], default=None)
    row = {
        "game": spdb.model_log_base(slug), "slug": slug,
        "home": names[0] if names else None, "away": names[1] if names else None,
        "competition": leagues.league_prefix(slug),
        "market": market_type, "line": line,
        "lam_home": round(lam_h, 3), "lam_away": round(lam_a, 3),
        "used_external": used,
        "best_side": best["side"] if best else None,
        "best_edge": round(best["edge"], 4) if best else None,
        "in_odds_band": bool(best and best["in_odds_band"]),
        "bet_blocked_no_sharp": bet_blocked,
        "edges": [{"side": n["side"], "price": n["price"],
                   "p_model": round(n["p_model"], 4), "edge": round(n["edge"], 4),
                   "in_odds_band": n["in_odds_band"]} for n in notes],
    }
    if market_type == "TOTAL":
        row["p_over"] = round(probs["p_over_eff"], 4)
        row["p_under"] = round(probs["p_under_eff"], 4)
        row["sharp_over"] = round(sharp[1], 4) if sharp else None
        row["sharp_line"] = sharp[0] if sharp else None
    else:
        row["p_yes"] = round(probs["p_yes"], 4)
        row["p_no"] = round(probs["p_no"], 4)
        row["sharp_yes"] = round(sharp, 4) if sharp is not None else None
    return row


def _shadow_log(market_type, slug, line, notes, chosen, lam_h, lam_a, used, args, target,
                bet, skip_reason, ref_token=None):
    """Shadow-log this modeled market (bet or not) for later calibration."""
    if not args.record:
        return
    ref_side = "OVER" if market_type == "TOTAL" else "YES"
    ref = next((n for n in notes if n["side"] == ref_side), None)
    try:
        spdb.record_model_log({
            "game_slug": slug, "game_date": target, "league": leagues.league_prefix(slug),
            "market": market_type, "line": line, "ref_side": ref_side,
            "ref_prob": ref["p_model"] if ref else None,
            "ref_price": ref["price"] if ref else None,
            "ref_token": ref_token,
            "pick_side": chosen["side"] if chosen else None,
            "pick_edge": round(chosen["edge"], 4) if chosen else None,
            "used_external": used,
            "model_params": {"lam_home": round(lam_h, 4), "lam_away": round(lam_a, 4),
                             "rho": args.rho},
            "bet": bet, "skip_reason": skip_reason, "market_url": leagues.game_url(slug),
        }, args.predictions_db)
    except Exception as e:  # noqa: BLE001
        if args.debug:
            print(f"[model_log] failed: {e}", file=sys.stderr)


def _evaluate(market_type, slug, m, line, chosen, notes, lam_h, lam_a, used,
              book_sum, price_sane, args, portfolio_value, first_trade, kh, _skip, target,
              ref_token=None, cong=None):
    """Return a candidate dict for a passing market, or None (skip handled inline).

    Recording is deferred to the caller's best-line selection so we never place
    correlated multi-line bets on one game.
    """
    def _shadow(bet, skip_reason):
        _shadow_log(market_type, slug, line, notes, chosen, lam_h, lam_a, used, args,
                    target, bet, skip_reason, ref_token)

    if not chosen:
        impl = next((n for n in notes
                     if n.get("implausible") and n["edge"] > 0 and n["in_odds_band"]), None)
        reason = (f"edge {impl['edge']:.1%} implausibly large (> {MAX_PLAUSIBLE_EDGE:.0%} cap) "
                  f"— likely model error, skipped" if impl else
                  f"no positive-edge side within {args.odds_min:.2f}x-{args.odds_max:.1f}x band")
        _shadow(0, reason)
        _skip(slug, reason, market=market_type, sides=notes)
        return None
    passed, reason = decision_tree(chosen, m, book_sum, price_sane,
                                   min_volume=args.min_volume, min_edge=args.min_edge,
                                   min_hours=args.min_hours)
    if not passed:
        _shadow(0, reason)
        _skip(slug, reason, market=market_type, side=chosen["side"]); return None
    size_pct, size_usd, kelly = size_position(chosen["p_model"], chosen["price"],
                                              portfolio_value, first_trade, kh)
    # Congruence: shrink size when our independent Dixon-Coles disagrees with the sharp anchor
    # (factor 0 → size 0 → tripped by the $10 minimum below). Never moves the edge.
    cong = cong or dict(cg.NEUTRAL)
    if cong.get("applied") and not getattr(args, "no_congruence", False):
        size_pct *= cong["factor"]; size_usd = portfolio_value * size_pct
    if kelly <= 0:
        _shadow(0, "Kelly <= 0")
        _skip(slug, "Kelly <= 0", market=market_type); return None
    if size_usd < 10:
        _shadow(0, f"size ${size_usd:.2f} below $10 minimum")
        _skip(slug, f"size ${size_usd:.2f} below $10 minimum", market=market_type); return None

    confidence = max(0.5, min(0.5 + chosen["edge"], 0.65)) if used else 0.5
    if cong.get("applied") and not getattr(args, "no_congruence", False):
        confidence = cg.apply_confidence(confidence, cong["factor"])
    price = chosen["price"]
    odds = dc.decimal_odds(price)
    market_url = leagues.game_url(slug)
    desc = (f"{market_type} {chosen['side']}" + (f" {line}" if line is not None else "")
            + f" @ {price:.3f} (payout {odds:.2f}x) edge {chosen['edge']*100:+.1f}% "
            + ("Dixon-Coles" if used else "market-implied (fallback)"))
    forecast = fcs.forecast_block(lam_h, lam_a, args.rho, line, market_type)
    stats = {
        "model": "dixon_coles", "market": market_type, "chosen_side": chosen["side"],
        "lam_home": round(lam_h, 4),
        "lam_away": round(lam_a, 4), "rho": args.rho, "line": line,
        "model_prob": round(chosen["p_model"], 4), "entry_price": price,
        "decimal_odds": round(odds, 4), "edge_after_fee": round(chosen["edge"], 4),
        "used_external": used, "book_sum": book_sum, "price_sane": price_sane,
        "kelly_fraction": round(kelly, 5), "size_pct": round(size_pct, 5),
        "size_usd": round(size_usd, 2), "confidence": round(confidence, 3),
        "forecast": forecast, "congruence": cong if cong.get("applied") else None,
        "sides": notes,
    }
    rec = {"token_id": chosen["token"], "side": "YES", "action": "BUY",
           "size_pct": round(size_pct, 4), "price": round(price, 4),
           "confidence": round(confidence, 3), "reasoning": sanitize_text(desc),
           "strategy": STRATEGY, "fee_rate": args.fee_rate}
    fc_line = (f"\nForecast: ~{forecast['mean_goals']} goals "
               f"(80% PI {forecast['pi80'][0]}-{forecast['pi80'][1]}, "
               f"entropy {forecast['entropy_bits']:.2f} bits, "
               f"BTTS {forecast['p_btts']*100:.0f}%)")
    text = (f"Market: {sanitize_text(m.get('question',''))}  [{slug}]\n"
            f"Edge type: news-driven (Dixon-Coles model)\n{desc}{fc_line}\n"
            f"Size: ${size_usd:,.2f} ({size_pct*100:.2f}%)  Confidence: {confidence:.2f}")
    return {"market_type": market_type, "slug": slug, "m": m, "line": line, "chosen": chosen,
            "notes": notes, "lam_h": lam_h, "lam_a": lam_a, "used": used, "confidence": confidence,
            "price": price, "odds": odds, "market_url": market_url, "size_pct": size_pct,
            "size_usd": size_usd, "kelly": kelly, "stats": stats, "rec": rec, "text": text,
            "ref_token": ref_token}


def _record_soccer(c, args, target):
    """Persist a winning candidate to the predictions DB; returns the row id."""
    if not args.record:
        return None
    try:
        return spdb.record_prediction({
            "game_slug": c["slug"], "game_date": target, "league": leagues.league_prefix(c["slug"]),
            "market": c["market_type"], "market_question": sanitize_text(c["m"].get("question", "")),
            "condition_id": c["m"].get("condition_id"), "token_id": c["chosen"]["token"],
            "line": c["line"], "side": c["chosen"]["side"], "entry_price": c["price"],
            "decimal_odds": c["odds"], "model_prob": c["chosen"]["p_model"], "edge": c["chosen"]["edge"],
            "lam_home": c["lam_h"], "lam_away": c["lam_a"], "rho": args.rho,
            "confidence": c["confidence"], "size_pct": c["size_pct"], "size_usd": c["size_usd"],
            "kelly_fraction": c["kelly"], "used_external": c["used"], "fee_rate": args.fee_rate,
            "strategy": STRATEGY, "market_url": c["market_url"], "stats": c["stats"],
        }, args.predictions_db)
    except Exception as e:  # noqa: BLE001
        if args.debug:
            print(f"[record] failed: {e}", file=sys.stderr)
        return None


def render_text(result: dict) -> str:
    c = result["counts"]
    lines = [f"Soccer goals/BTTS suggestions — {result['date']}", "=" * 64,
             f"Total markets: {c['total_markets']}  BTTS: {c['btts_markets']}  "
             f"Suggestions: {c['suggestions']}  Skipped: {c['skipped']}", ""]
    if not result.get("_texts"):
        lines.append("No actionable edge found.")
    for t in result.get("_texts", []):
        lines.append(t); lines.append("-" * 64)
    lines += ["", result["disclaimer"]]
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description="Suggest soccer total-goals/BTTS entries on Polymarket.")
    p.add_argument("--date", default=None, help="Target day YYYY-MM-DD (default today UTC)")
    p.add_argument("--min-volume", type=float, default=0.0,
                   help="Min 24h volume to consider a game (default 0 = no volume filter; "
                        "pass a value to re-enable)")
    p.add_argument("--no-congruence", action="store_true",
                   help="Disable model↔sharp congruence sizing (default on: shrink size/confidence "
                        "when the Dixon-Coles disagrees with the sharp anchor)")
    p.add_argument("--min-edge", type=float, default=0.05)
    p.add_argument("--max-edge", type=float, default=MAX_PLAUSIBLE_EDGE,
                   help=f"Reject a side whose edge exceeds this as likely model error "
                        f"(default {MAX_PLAUSIBLE_EDGE})")
    p.add_argument("--min-hours", type=float, default=0.0, help="0 = pre-game only (not started)")
    p.add_argument("--odds-min", type=float, default=dc.ODDS_MIN_DEFAULT)
    p.add_argument("--odds-max", type=float, default=dc.ODDS_MAX_DEFAULT)
    p.add_argument("--rho", type=float, default=dc.DEFAULT_RHO, help="Dixon-Coles dependence (default -0.10)")
    p.add_argument("--ratings-csv", default=None, help="CSV of team elo/att_factor/def_factor (overrides auto)")
    p.add_argument("--no-auto-ratings", dest="auto_ratings", action="store_false", default=True,
                   help="Disable automatic ratings (national Elo / Club Elo / xG) -> market-implied")
    p.add_argument("--no-calibrate-baselines", dest="calibrate_baselines", action="store_false",
                   default=True, help="Disable football-data.org league-baseline calibration "
                                      "(keep the static LEAGUE_BASELINES)")
    p.add_argument("--football-data-token", default=None,
                   help="football-data.org key for baseline calibration (default $FOOTBALL_DATA_TOKEN)")
    p.add_argument("--odds-api-key", default=None,
                   help="The Odds API key (or $ODDS_API_KEY) -> sharp anchor (divergence detector)")
    p.add_argument("--no-sharp", action="store_true",
                   help="Disable the sharp anchor entirely (predictive model, edge-capped)")
    p.add_argument("--sharp-book", default=None,
                   help="Sharp bookmaker priority chain (comma-separated Odds-API keys), first "
                        "with the market wins. Default 'pinnacle,betfair_ex_eu' — Betfair Exchange "
                        "fills sharp markets Pinnacle lacks (e.g. lower-league BTTS). Both are sharp.")
    p.add_argument("--no-sharp-btts", dest="sharp_btts", action="store_false", default=True,
                   help="Skip the per-event BTTS sharp fetch (cheaper; BTTS stays predictive)")
    p.add_argument("--sharp-leagues", default=None,
                   help="Comma substrings to limit sharp leagues (e.g. 'world_cup,epl'); default all active")
    p.add_argument("--sharp-min-reserve", type=int, default=0,
                   help="Stop the sharp fetch once Odds-API remaining quota hits this floor "
                        "(reserves credits for other sports, e.g. MLB; 0 = no reserve)")
    p.add_argument("--no-require-sharp", dest="require_sharp", action="store_false", default=True,
                   help="With a sharp slate loaded, still model games with NO sharp match (default OFF: "
                        "skip them — bet only on a sharp anchor)")
    p.add_argument("--no-sharp-discovery", dest="sharp_discovery", action="store_false", default=True,
                   help="Disable sharp-driven discovery (fetching games the volume-ranked Gamma "
                        "tag truncated directly by event slug). On by default when a sharp slate loads.")
    p.add_argument("--home-first", dest="home_first", action="store_true", default=True,
                   help="Slug lists home team first (default)")
    p.add_argument("--away-first", dest="home_first", action="store_false",
                   help="Slug lists away team first")
    p.add_argument("--all-lines", dest="best_line_only", action="store_false", default=True)
    p.add_argument("--fee-rate", type=float, default=0.0)
    p.add_argument("--portfolio-value", type=float, default=10000.0)
    p.add_argument("--portfolio-db", default=None)
    p.add_argument("--record", dest="record", action="store_true", default=True)
    p.add_argument("--no-record", dest="record", action="store_false")
    p.add_argument("--predictions-db", default=spdb.DEFAULT_DB)
    p.add_argument("--output", choices=["json", "text"], default="json")
    p.add_argument("--rate-limit", type=int, default=100)
    p.add_argument("--quiet", dest="verbose", action="store_false", default=True)
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()

    try:
        result = run(args)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": str(e)}), file=sys.stderr); sys.exit(1)

    if args.output == "text":
        print(render_text(result))
    else:
        result.pop("_texts", None)
        print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
