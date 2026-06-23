#!/usr/bin/env python3
"""Suggest Over/Under total-runs entries for the day's MLB games on Polymarket.

Pipeline: today's MLB games (reusing the category-watcher scanner) -> find each
game's total-runs Over/Under market -> model P(Over)/P(Under) with a Negative
Binomial run distribution -> edge vs the Polymarket price -> pick a side ->
filter to a 1.50x-3.0x payout (price in [0.3333, 0.667]) -> entry decision tree
-> half-Kelly size capped per CLAUDE.md -> emit recommendation(s). Optionally
pipe into the paper trader with --paper (dry-run unless --paper-execute).

Paper trading is the default (CLAUDE.md rule #2). This is a simulation, not
financial advice; real trading involves risk of loss.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

import _bootstrap  # noqa: F401  (wires sys.path for the reused skills)

import run_distribution as rd
import forecast as fc
import park_factors as pf
import totals_market as tm
import data_inputs
import predictions_db
import sharp_odds
import sharp_discovery

from category_common import (
    APIClient,
    discover_markets,
    fetch_midpoint,
    game_date,
    log,
    resolve_category,
    sanitize_text,
)

STRATEGY = "mlb-totals-negbin"

# Full-GAME total-runs market slug: "...-YYYY-MM-DD-total-<line>[pt5]".
# This excludes moneyline, spreads, first-5-innings (-f5-), strikeout props (-k-),
# NRFI, etc., so the run model never evaluates a non-run-total market.
_GAME_TOTAL_RE = re.compile(r"-\d{4}-\d{2}-\d{2}-total-\d{1,2}(?:pt5)?$")


def matchup_key(slug: str) -> str:
    """Strip the -total-<line> suffix to get the base matchup (for best-line dedupe)."""
    return _GAME_TOTAL_RE.sub("", slug)

# CLAUDE.md per-trade caps for this model/news-driven edge.
CAP_MODEL = 0.02
CAP_FIRST_TRADE = 0.01


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def fee_for(price: float, fee_rate: float) -> float:
    """Parabolic taker fee = fee_rate * min(p, 1-p). fee_rate default 0 (sports)."""
    if not fee_rate or price is None:
        return 0.0
    return fee_rate * min(price, 1.0 - price)


def group_by_event(markets: list[dict]) -> dict[str, list[dict]]:
    """Group rich parsed markets by event_slug (keeps decision-tree fields)."""
    out: dict[str, list[dict]] = {}
    for m in markets:
        key = m.get("event_slug") or m.get("slug")
        if key:
            out.setdefault(key, []).append(m)
    return out


# Inputs strong enough to justify overriding the market's expected total. Weather
# (temp/wind) and the park factor are second-order and largely already in the line,
# so they do NOT on their own move mu off the market — otherwise the model
# manufactures fake edges (see the col-oak post-mortem).
STRONG_INPUT_KEYS = ("home_off", "away_off", "home_sp", "away_sp")


def model_probabilities(line, over_price, park_factor, inputs, *,
                        league_baseline, dispersion, sharp_over_price=None, sharp_line=None):
    """Model the total-runs distribution and return the full math as a dict.

    The FAIR-VALUE anchor is the SHARP reference when one is supplied
    (`sharp_over_price`, the devigged Pinnacle/consensus P(Over)) — because the deep
    research shows the sharp close, not our model, is the efficient probability. The
    edge (computed later in pick_side as p_model − over_price) then measures how far the
    POLYMARKET price diverges from the sharp fair value: a true mispricing detector.

    Crucially the sharp anchor is interpreted AT THE SHARP'S OWN LINE (`sharp_line`): we
    invert the sharp prob to an expected total (mu) at the sharp line, then evaluate the
    distribution at whatever Polymarket line is on offer. So a sharp main line of 8.5 can
    correctly price a Polymarket alternate line of 7.5/11.5 — line drift no longer breaks
    the anchor. When `sharp_line` is omitted the sharp prob is taken at the Polymarket line.

    In divergence mode mu is anchored PURELY to the sharp line — the factor model is NOT
    blended in. The 10-season backtest showed the factor model has no predictive edge, so
    nudging mu off the efficient sharp value with factors only adds noise AND masks bad
    sharp data (a wrong sharp line, blended halfway to the factor mu, can slip past the
    implausible-edge cap as a false signal). With a pure sharp anchor, a corrupt sharp line
    instead produces an implausibly large edge that the cap correctly rejects. `model_mu`
    is still computed and reported for transparency, but it does not move mu.

    Without a sharp price, the anchor falls back to the Polymarket price itself, so the
    model stays market-implied (edge ≈ 0 — anti-fabrication); only there do the factors
    nudge mu (the legacy no-sharp path). `over_price` is always the Polymarket price we
    trade against; both the sharp and Polymarket mu are reported.
    """
    poly_mu = rd.market_implied_mu(line, over_price, dispersion)
    if sharp_over_price is not None:
        anchor_price = sharp_over_price
        anchor_line = sharp_line if sharp_line is not None else line
    else:
        anchor_price = over_price
        anchor_line = line
    anchor_mu = rd.market_implied_mu(anchor_line, anchor_price, dispersion)
    used_external = any(k in inputs for k in STRONG_INPUT_KEYS)
    model_mu = None
    if used_external:
        base = rd.baseline_mu(park_factor, league_baseline)
        model_mu = rd.adjust_mu(base, **inputs)
    if sharp_over_price is not None:
        mu = anchor_mu                                  # pure sharp anchor (divergence detector)
    elif used_external:
        mu = rd.anchor_to_market(model_mu, anchor_mu)   # no sharp: factors nudge off poly anchor
    else:
        mu = anchor_mu
    var = rd.variance_from_mu(mu, dispersion)
    pmf = rd.negbin_total_runs_pmf(mu, var)
    probs = rd.prob_over(line, pmf)
    r, p = rd.negbin_params_from_moments(mu, var)
    return {
        "p_over": probs["p_over_eff"], "p_under": probs["p_under_eff"],
        "mu": mu, "market_mu": anchor_mu, "model_mu": model_mu, "poly_mu": poly_mu,
        "sharp_anchored": sharp_over_price is not None, "var": var,
        "used_external": used_external,
        "p_push": probs["p_push"], "need": probs["need"],
        "negbin_r": r, "negbin_p": p,
    }


def forecast_block(m, line) -> dict:
    """Layer 1 + 3 per-prediction confidence: the full distribution summarized.

    From the game's (mu, var) NegBin distribution: the expected total + most-likely
    total, the 50% / 80% PREDICTION INTERVALS (the honest range the total will land in
    — wide, because a single MLB game is irreducibly uncertain), and the predictive
    entropy (spread). This turns "P(Over)=0.62" into a forecast with stated confidence.
    The heavy pmf is dropped; only the human-readable summary is stored.
    """
    s = fc.forecast_summary(m["mu"], m["var"], line)
    return {
        "mean_total": round(s["mean"], 2),
        "median_total": s["median"],
        "most_likely_total": s["mode"],
        "pi50": list(s["pi50"]),
        "pi80": list(s["pi80"]),
        "pi80_mass": round(s["pi80_mass"], 4),
        "entropy_bits": round(s["entropy_bits"], 3),
        "p_over": round(s["p_over"], 4),
        "p_under": round(s["p_under"], 4),
    }


def pick_side(line, ou, p_over, p_under, fee_rate, odds_min, odds_max,
              max_edge=rd.MAX_PLAUSIBLE_EDGE):
    """Choose Over/Under by post-fee edge among odds-filter-eligible sides.

    A side whose post-fee edge exceeds `max_edge` is flagged `implausible` and
    excluded — on a near-efficient market that signals model error, not value.
    Returns a dict for the chosen side or None, plus a list of per-side notes.
    """
    sides = [
        ("OVER", ou["over_token"], ou["over_price"], p_over),
        ("UNDER", ou["under_token"], ou["under_price"], p_under),
    ]
    notes = []
    candidates = []
    for name, token, price, p_model in sides:
        if price is None:
            continue
        edge = p_model - price - fee_for(price, fee_rate)
        in_odds = rd.passes_odds_filter(price, odds_min, odds_max)
        implausible = edge > max_edge
        notes.append({"side": name, "price": round(price, 4),
                      "p_model": round(p_model, 4), "edge": round(edge, 4),
                      "in_odds_band": in_odds, "implausible": implausible})
        if edge > 0 and in_odds and not implausible:
            candidates.append({"side": name, "token": token, "price": price,
                               "p_model": p_model, "edge": edge})
    if not candidates:
        return None, notes
    candidates.sort(key=lambda c: c["edge"], reverse=True)
    return candidates[0], notes


def decision_tree(chosen, totals_market, ou, *, min_volume, min_edge,
                  max_spread=0.10, min_hours=0.0):
    """Run the entry decision tree. Returns (passed: bool, reason: str|None).

    For daily MLB totals, min_hours defaults to 0: the game must not have started
    yet (end_date in the future), rather than the generic >24h horizon rule.
    """
    vol = float(totals_market.get("volume_24h") or 0)
    if vol < min_volume:
        return False, f"volume ${vol:,.0f}/24h < ${min_volume:,.0f}"

    # Spread: prefer a real value if present, else a book-overround proxy.
    spread = totals_market.get("spread")
    if spread is None:
        spread = abs(1.0 - ou["book_sum"])
    if spread >= max_spread:
        return False, f"spread {spread:.1%} >= {max_spread:.0%}"

    end = totals_market.get("end_date")
    if end:
        try:
            dt = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
            hours = (dt - now_utc()).total_seconds() / 3600.0
            if hours < min_hours:
                if hours < 0:
                    return False, f"game already started ({-hours:.1f}h ago)"
                return False, f"game starts in {hours:.1f}h < {min_hours:.0f}h lead"
        except ValueError:
            pass

    if totals_market.get("accepting_orders") is False:
        return False, "not accepting orders"

    if chosen["edge"] < min_edge:
        return False, f"edge {chosen['edge']:.1%} < {min_edge:.0%} after fees"

    if not ou["price_sane"]:
        return False, f"price sanity failed (book_sum={ou['book_sum']})"

    return True, None


def size_position(p_model, price, portfolio_value, first_trade, kelly_half):
    """Half-Kelly fraction with model/news caps. Returns (size_pct, size_usd, kelly)."""
    kelly = kelly_half(p_model, price, "YES")
    cap = CAP_FIRST_TRADE if first_trade else CAP_MODEL
    size_pct = min(kelly, cap)
    size_usd = portfolio_value * size_pct
    return size_pct, size_usd, kelly


def build_recommendation(game_slug, totals, line, chosen, mu, used_external,
                         size_pct, size_usd, confidence, fee_rate, ou, forecast=None):
    """Assemble the execute_paper-compatible recommendation + a human text block."""
    side_word = chosen["side"].capitalize()
    price = chosen["price"]
    reasoning = (
        f"NegBin model mu={mu:.2f} vs line {line}; "
        f"P({side_word})={chosen['p_model']:.3f} vs price {price:.3f}; "
        f"edge {chosen['edge']*100:+.1f}pts after fee. "
        f"Payout {rd.decimal_odds(price):.2f}x. "
        f"{'real inputs' if used_external else 'market-implied (zero-edge fallback)'}."
    )
    rec = {
        "token_id": chosen["token"],
        "side": "YES",  # betting Over/Under = BUY YES on that side's own token
        "action": "BUY",
        "size_pct": round(size_pct, 4),
        "price": round(price, 4),
        "confidence": round(confidence, 3),
        "reasoning": sanitize_text(reasoning),
        "strategy": STRATEGY,
        "fee_rate": fee_rate,
    }
    text = (
        f"Market: {sanitize_text(totals.get('question',''))}  [{game_slug}]\n"
        f"Edge type: news-driven (statistical model)\n"
        f"Side: {chosen['side']} total {line} at {price:.3f}  "
        f"(payout {rd.decimal_odds(price):.2f}x)\n"
        f"Size: ${size_usd:,.2f} ({size_pct*100:.2f}% of portfolio)\n"
        f"Confidence: {confidence:.2f}\n"
        + (f"Forecast: ~{forecast['mean_total']} runs "
           f"(80% PI {forecast['pi80'][0]}-{forecast['pi80'][1]}, "
           f"entropy {forecast['entropy_bits']:.2f} bits)\n" if forecast else "")
        + f"Edge: {chosen['edge']*100:+.1f}% after fees\n"
        f"Reasoning: {sanitize_text(reasoning)}"
    )
    return rec, text


def record_prediction_row(db_path, game_slug, game_date, totals, line, chosen, m,
                          ou, inputs, park_factor, size_pct, size_usd, kelly,
                          confidence, side_notes, args) -> int | None:
    """Persist the prediction + its full statistical/mathematical audit log.

    Returns the prediction row id, or None on failure (recording never blocks a
    suggestion). status starts as PENDENTE; settle later via track_predictions.py.
    """
    price = chosen["price"]
    odds = rd.decimal_odds(price)
    forecast = forecast_block(m, line)
    # Link to the game event, not the specific total line (strip "-total-9pt5").
    event = predictions_db.model_log_base(game_slug or totals.get("slug") or "")
    market_url = (f"https://polymarket.com/event/{event}" if event
                  else f"https://polymarket.com/sports/mlb/{game_slug}")
    stats = {
        "model": "negative_binomial",
        "mu": round(m["mu"], 4), "market_mu": round(m["market_mu"], 4),
        "model_mu": round(m["model_mu"], 4) if m.get("model_mu") is not None else None,
        "poly_mu": round(m["poly_mu"], 4) if m.get("poly_mu") is not None else None,
        "sharp_anchored": m.get("sharp_anchored", False),
        "variance": round(m["var"], 4),
        "dispersion": args.dispersion,
        "negbin_r": round(m["negbin_r"], 4), "negbin_p": round(m["negbin_p"], 4),
        "league_baseline": args.league_baseline, "park_factor": park_factor,
        "used_external": m["used_external"], "inputs": inputs,
        "line": line, "need": m["need"],
        "p_over_eff": round(m["p_over"], 4), "p_under_eff": round(m["p_under"], 4),
        "p_push": round(m["p_push"], 4),
        "forecast": forecast,
        "chosen_side": chosen["side"], "entry_price": price,
        "decimal_odds": round(odds, 4), "model_prob": round(chosen["p_model"], 4),
        "fee_rate": args.fee_rate, "fee_applied": round(fee_for(price, args.fee_rate), 5),
        "edge_after_fee": round(chosen["edge"], 4),
        "book_sum": ou["book_sum"], "price_sane": ou["price_sane"],
        "kelly_fraction": round(kelly, 5),
        "cap": CAP_FIRST_TRADE if size_pct <= CAP_FIRST_TRADE else CAP_MODEL,
        "size_pct": round(size_pct, 5), "size_usd": round(size_usd, 2),
        "confidence": round(confidence, 3),
        "side_notes": side_notes,
    }
    row = {
        "game_slug": game_slug, "game_date": game_date,
        "market_question": sanitize_text(totals.get("question", "")),
        "condition_id": totals.get("condition_id"),
        "token_id": chosen["token"], "line": line, "side": chosen["side"],
        "entry_price": price, "decimal_odds": odds,
        "model_prob": chosen["p_model"], "edge": chosen["edge"],
        "mu": m["mu"], "variance": m["var"], "dispersion": args.dispersion,
        "park_factor": park_factor, "confidence": confidence,
        "size_pct": size_pct, "size_usd": size_usd, "kelly_fraction": kelly,
        "used_external": m["used_external"], "fee_rate": args.fee_rate,
        "strategy": STRATEGY, "market_url": market_url, "stats": stats,
    }
    try:
        return predictions_db.record_prediction(row, db_path)
    except Exception as e:  # noqa: BLE001 - recording must never block a suggestion
        if args.debug:
            print(f"[record] failed: {e}", file=sys.stderr)
        return None


def _shadow_log_mlb(args, target, slug, line, side_notes, chosen, m, bet, skip_reason, totals, ou):
    """Shadow-log one modeled run-total (bet or not) for later calibration."""
    if not args.record:
        return
    ref = next((n for n in side_notes if n["side"] == "OVER"), None)
    # Link to the game event, not the specific total line (strip "-total-9pt5").
    event = predictions_db.model_log_base(slug or totals.get("slug") or "")
    url = (f"https://polymarket.com/event/{event}" if event
           else f"https://polymarket.com/sports/mlb/{slug}")
    try:
        predictions_db.record_model_log({
            "game_slug": slug, "game_date": target, "league": None, "market": "TOTAL",
            "line": line, "ref_side": "OVER",
            "ref_prob": ref["p_model"] if ref else None,
            "ref_price": ref["price"] if ref else None,
            "ref_token": (ou or {}).get("over_token"),
            "pick_side": chosen["side"] if chosen else None,
            "pick_edge": round(chosen["edge"], 4) if chosen else None,
            "used_external": m["used_external"],
            "model_params": {"mu": round(m["mu"], 4), "market_mu": round(m["market_mu"], 4),
                             "model_mu": round(m["model_mu"], 4) if m.get("model_mu") is not None else None,
                             "variance": round(m["var"], 4)},
            "bet": bet, "skip_reason": skip_reason, "market_url": url,
        }, args.predictions_db)
    except Exception as e:  # noqa: BLE001
        if args.debug:
            print(f"[model_log] failed: {e}", file=sys.stderr)


def _load_sharp_lookup(args, target, vlog) -> dict:
    """Load the sharp reference (Pinnacle/consensus, devigged) -> the fair-value anchor.

    CSV first, then The Odds API. With it the model is a divergence detector (edge =
    Polymarket price vs sharp fair); without it the model stays Polymarket-anchored
    (zero edge). It also doubles as the authoritative game list for sharp-driven
    discovery, so it is loaded BEFORE market discovery.
    """
    sharp_lookup: dict = {}
    if getattr(args, "sharp_odds_csv", None):
        vlog(f"  sharp source: CSV {args.sharp_odds_csv}")
        try:
            sharp_lookup = sharp_odds.load_sharp_csv(args.sharp_odds_csv)
        except OSError as e:
            vlog(f"  sharp CSV load failed: {e}")
    elif getattr(args, "odds_api_key", None) or os.environ.get("ODDS_API_KEY"):
        vlog("  sharp source: The Odds API (live)")
        sharp_lookup = sharp_odds.fetch_sharp(
            getattr(args, "odds_api_key", None), target, vlog=vlog)
    else:
        vlog("  sharp source: NONE (no --sharp-odds-csv and no --odds-api-key/$ODDS_API_KEY) "
             "-> Polymarket-anchored, zero-edge fallback")
    if sharp_lookup:
        dated = sum(1 for k in sharp_lookup if k[0] == target)
        vlog(f"  sharp reference loaded: {len(sharp_lookup)} game(s) "
             f"({dated} dated {target}) (divergence-detector mode)")
    else:
        vlog("  sharp reference EMPTY — divergence detector OFF "
             "(model stays Polymarket-anchored, zero edge)")
    return sharp_lookup


def run(args) -> dict:
    api = APIClient(rate_limit_ms=args.rate_limit, debug=args.debug)
    # Robust terminal logging (stderr -> shows in the uvicorn console). On unless --quiet.
    vlog = log if getattr(args, "verbose", True) else (lambda *a, **k: None)
    target = args.date or now_utc().date().isoformat()
    vlog(f"=== MLB totals analysis for {target} ===")
    category_key, candidates = resolve_category("baseball")
    # Prefer the tighter "mlb" tag; Gamma may ignore an unknown tag_slug and
    # return ALL sports, so we also hard-filter by slug prefix below.
    candidates = ["mlb"] + [c for c in candidates if c != "mlb"]

    try:
        _tag, markets = discover_markets(api, category_key, candidates,
                                         min_volume=0.0, include_closed=False)
    except Exception as e:  # noqa: BLE001 - network failure -> empty, reported
        return {"date": target, "error": f"discovery failed: {e}",
                "suggestions": [], "skipped": []}

    # Coverage diagnostics: the `mlb` tag is NOT honored by Gamma — it returns the
    # global volume-ranked mix (crypto/politics/other sports), and pagination is capped
    # (HTTP 422 past offset ~2100). So low-volume MLB games can fall past the cut and be
    # invisible. Surface the MLB-vs-mix split + a truncation flag so this is auditable.
    # The truncation check uses DATED MLB GAMES (distinct events on the target day), not
    # the raw count of mlb- markets — a fat backlog of future/other-day games masks the
    # fact that today's low-volume games were cut.
    def _is_mlb(m):
        return (m.get("event_slug") or m.get("slug") or "").lower().startswith("mlb-")
    mlb_all = [m for m in markets if _is_mlb(m)]
    mlb_events_today = {(m.get("event_slug") or m.get("slug"))
                        for m in mlb_all if game_date(m) == target}
    vlog(f"Discovery: tag '{_tag}' -> {len(markets)} active market(s); "
         f"{len(mlb_all)} are MLB (mlb- prefix), {len(mlb_events_today)} MLB event(s) "
         f"dated {target}; {len(markets) - len(mlb_all)} other (the tag's global mix)")
    if len(markets) >= 2000 and len(mlb_events_today) < 6:
        vlog(f"  ⚠️ COVERAGE WARNING: only {len(mlb_events_today)} MLB game(s) dated "
             f"{target} surfaced in the top {len(markets)} by volume — low-volume games "
             f"are being cut by the offset cap. Sharp-driven discovery will recover them.")

    # Load the sharp reference EARLY: it doubles as the authoritative game list.
    sharp_lookup = _load_sharp_lookup(args, target, vlog)

    # Sharp-source-driven discovery: the sharp slate carries the FULL daily card, so we
    # fetch each sharp game's Polymarket markets by event slug and UNION them in. This
    # fixes coverage (every game found regardless of volume rank) AND matching (every
    # added game already has a sharp ref). On by default when a sharp slate is loaded.
    if sharp_lookup and getattr(args, "sharp_discovery", True):
        existing = {m.get("slug") for m in markets if m.get("slug")}
        extra = sharp_discovery.discover_from_sharp(api, sharp_lookup, target, vlog=vlog)
        added = [m for m in extra if m.get("slug") and m.get("slug") not in existing]
        if added:
            markets = markets + added
            vlog(f"  sharp-driven discovery added {len(added)} market(s) the tag missed "
                 f"(total now {len(markets)})")

    on_day = [m for m in markets if game_date(m) == target]
    games = group_by_event(on_day)
    vlog(f"  {len(on_day)} market(s) dated {target} across {len(games)} event(s)")

    # Keep only MLB games. Polymarket game slugs are league-prefixed
    # (mlb-..., fifwc-..., cs2-...); this guarantees we never model a soccer or
    # esports total as baseball runs even if discovery returned mixed sports.
    prefix = (args.league_prefix or "").lower()
    filtered_non_league = 0
    if prefix:
        before = len(games)
        games = {k: v for k, v in games.items() if k.lower().startswith(prefix)}
        filtered_non_league = before - len(games)
        vlog(f"  filtered out {filtered_non_league} non-'{prefix}' event(s); "
             f"{len(games)} MLB event(s) dated {target}: "
             + (", ".join(sorted(games)) if games else "(none)"))

    # Keep only full-GAME total-runs markets (drop moneyline/spread/F5/K-prop/NRFI).
    before_total = len(games)
    games = {k: v for k, v in games.items() if _GAME_TOTAL_RE.search(k.lower())}
    filtered_non_total = before_total - len(games)
    vlog(f"  dropped {filtered_non_total} non-run-total event(s) "
         f"(moneyline/spread/F5/K-prop/NRFI); {len(games)} run-total market(s) to analyze")
    if games:
        vlog("  run-total markets: " + ", ".join(sorted(games)))

    portfolio_value = float(args.portfolio_value)
    first_trade = data_inputs.is_first_trade(STRATEGY, args.portfolio_db)

    suggestions, texts, skipped = [], [], []
    candidates = []  # passing lines; recorded after best-line selection (fix: one bet/game)

    def _skip(slug, reason, **extra):
        rec = {"game": slug, "reason": reason}
        rec.update(extra)
        skipped.append(rec)
        vlog(f"  [{slug}] SKIP — {reason}")

    for event_slug, gmarkets in games.items():
        game = {"markets": gmarkets}
        vlog(f"[{event_slug}] {len(gmarkets)} market(s): "
             + " | ".join(sanitize_text(mm.get("question", "")) for mm in gmarkets))
        totals = tm.find_totals_market(game)
        if not totals:
            _skip(event_slug, "no total-runs market")
            continue
        line = tm.parse_total_line(totals)
        if line is None:
            _skip(event_slug, "could not parse total line")
            continue
        ou = tm.over_under_tokens(totals)
        if not ou or ou["over_price"] is None or ou["under_price"] is None:
            _skip(event_slug, "could not map Over/Under tokens")
            continue
        vlog(f"  [{event_slug}] totals line={line} Over={ou['over_price']} "
             f"Under={ou['under_price']} book_sum={ou['book_sum']} sane={ou['price_sane']}")

        # Optional live price refresh (CLOB midpoint), fallback to Gamma price.
        if args.refresh_prices:
            for key, tok in (("over_price", ou["over_token"]), ("under_price", ou["under_token"])):
                mid = fetch_midpoint(api, tok)
                if mid is not None:
                    ou[key] = mid
            ou["book_sum"] = round((ou["over_price"] or 0) + (ou["under_price"] or 0), 4)
            ou["price_sane"] = 0.90 <= ou["book_sum"] <= 1.10

        park_factor = pf.park_factor_for_slug(event_slug)
        inputs = {}
        if args.use_external:
            inputs = data_inputs.get_game_inputs(
                api, event_slug, target, projections_csv=args.projections_csv,
                debug=args.debug) or {}

        away, home = pf.parse_slug_teams(event_slug)
        sharp = sharp_odds.sharp_ref(sharp_lookup, target, away, home) if sharp_lookup else None
        sharp_line, sharp_over = (sharp if sharp else (None, None))
        # Divergence mode: with a sharp slate loaded, bet ONLY on a sharp anchor. A game
        # with no sharp match must be skipped, not fall back to the factor model (which the
        # backtest proved is -EV) — otherwise the loophole reintroduces factor-noise bets.
        if sharp_lookup and sharp is None:
            vlog(f"  [{event_slug}] ⚠️ no sharp match for parsed teams "
                 f"away={away} home={home} — not in the {len(sharp_lookup)}-game sharp slate "
                 f"(check team-abbrev mapping)")
            if getattr(args, "require_sharp", True):
                _skip(event_slug, "no sharp reference (divergence mode bets only on a sharp anchor)",
                      line=line)
                continue
        m = model_probabilities(
            line, ou["over_price"], park_factor, inputs,
            league_baseline=args.league_baseline, dispersion=args.dispersion,
            sharp_over_price=sharp_over, sharp_line=sharp_line)
        vlog(f"  [{event_slug}] model: park={park_factor} mu={m['mu']:.2f} "
             f"P(over)={m['p_over']:.3f} P(under)={m['p_under']:.3f} "
             f"external_inputs={m['used_external']}"
             + (f" sharp_over={sharp_over:.3f}@line{sharp_line:g} "
                f"(sharp_mu={m['market_mu']:.2f} vs poly_mu={m['poly_mu']:.2f})"
                if sharp_over is not None else " (no sharp ref)")
             + (f" inputs={inputs}" if inputs else ""))

        chosen, side_notes = pick_side(line, ou, m["p_over"], m["p_under"],
                                       args.fee_rate, args.odds_min, args.odds_max)
        vlog(f"  [{event_slug}] edges: " + "; ".join(
            f"{n['side']} price={n['price']} p={n['p_model']} edge={n['edge']:+.3f} "
            f"band={n['in_odds_band']}" for n in side_notes))

        if not chosen:
            implausible = next((n for n in side_notes
                                if n.get("implausible") and n["edge"] > 0 and n["in_odds_band"]), None)
            reason = (f"edge {implausible['edge']:.1%} implausibly large "
                      f"(> {rd.MAX_PLAUSIBLE_EDGE:.0%} cap) — likely model error, skipped"
                      if implausible else
                      f"no positive-edge side within "
                      f"{args.odds_min:.2f}x-{args.odds_max:.1f}x band")
            _shadow_log_mlb(args, target, event_slug, line, side_notes, chosen, m, 0, reason, totals, ou)
            _skip(event_slug, reason, line=line, sides=side_notes)
            continue

        passed, reason = decision_tree(chosen, totals, ou,
                                       min_volume=args.min_volume, min_edge=args.min_edge,
                                       min_hours=args.min_hours)
        if not passed:
            _shadow_log_mlb(args, target, event_slug, line, side_notes, chosen, m, 0, reason, totals, ou)
            _skip(event_slug, reason, line=line, side=chosen["side"])
            continue

        size_pct, size_usd, kelly = size_position(
            chosen["p_model"], chosen["price"], portfolio_value, first_trade,
            advisor_kelly_half())
        if kelly <= 0:
            _shadow_log_mlb(args, target, event_slug, line, side_notes, chosen, m, 0, "Kelly <= 0", totals, ou)
            _skip(event_slug, "Kelly <= 0", line=line, side=chosen["side"])
            continue
        if size_usd < 10:
            r = f"size ${size_usd:.2f} below $10 minimum"
            _shadow_log_mlb(args, target, event_slug, line, side_notes, chosen, m, 0, r, totals, ou)
            _skip(event_slug, r, line=line, side=chosen["side"])
            continue

        confidence = (max(0.5, min(0.5 + chosen["edge"], 0.65))
                      if m["used_external"] else 0.5)
        rec, text = build_recommendation(event_slug, totals, line, chosen, m["mu"],
                                         m["used_external"], size_pct, size_usd,
                                         confidence, args.fee_rate, ou,
                                         forecast_block(m, line))
        # Defer recording until best-line selection (avoids correlated multi-line bets).
        candidates.append({
            "event_slug": event_slug, "line": line, "totals": totals, "ou": ou,
            "inputs": inputs, "park_factor": park_factor, "m": m, "chosen": chosen,
            "side_notes": side_notes, "size_pct": size_pct, "size_usd": size_usd,
            "kelly": kelly, "confidence": confidence, "rec": rec, "text": text,
        })

    # Best-line-per-game: record/score only the highest-edge line per matchup; the other
    # passing lines are reported as skipped (not bet) so we never place correlated bets.
    by_match: dict[str, list] = {}
    for c in candidates:
        by_match.setdefault(matchup_key(c["event_slug"]), []).append(c)
    final = []
    for cs in by_match.values():
        cs.sort(key=lambda c: c["chosen"]["edge"], reverse=True)
        winners = cs[:1] if args.best_line_only else cs
        for c in (cs[1:] if args.best_line_only else []):
            _shadow_log_mlb(args, target, c["event_slug"], c["line"], c["side_notes"],
                            c["chosen"], c["m"], 0, "not best line for this game", c["totals"], c["ou"])
            _skip(c["event_slug"], "not best line for this game (best_line_only)",
                  line=c["line"], side=c["chosen"]["side"])
        final.extend(winners)

    recorded_ids: dict[str, set] = {}
    for c in final:
        prediction_id = None
        if args.record:
            prediction_id = record_prediction_row(
                args.predictions_db, c["event_slug"], target, c["totals"], c["line"],
                c["chosen"], c["m"], c["ou"], c["inputs"], c["park_factor"],
                c["size_pct"], c["size_usd"], c["kelly"], c["confidence"], c["side_notes"], args)
            if prediction_id is not None:
                recorded_ids.setdefault(c["event_slug"], set()).add(prediction_id)
        _shadow_log_mlb(args, target, c["event_slug"], c["line"], c["side_notes"],
                        c["chosen"], c["m"], 1, None, c["totals"], c["ou"])
        suggestions.append({"game": c["event_slug"], "line": c["line"],
                            "mu": round(c["m"]["mu"], 3), "edge": round(c["chosen"]["edge"], 4),
                            "forecast": forecast_block(c["m"], c["line"]),
                            "prediction_id": prediction_id, "recommendation": c["rec"],
                            "_text": c["text"]})
        vlog(f"  [{c['event_slug']}] >>> SUGGEST {c['chosen']['side']} {c['line']} "
             f"@ {c['chosen']['price']:.3f} edge={c['chosen']['edge']*100:+.1f}% "
             f"size={c['size_pct']*100:.2f}% conf={c['confidence']:.2f} pred_id={prediction_id}")

    # A re-run may pick a different best line per game; void this game's now-stale
    # PENDENTE entries from an earlier run so it carries one open position, not two.
    superseded = 0
    if args.record:
        for game_slug, keep in recorded_ids.items():
            n = predictions_db.supersede_pending(args.predictions_db, game_slug, keep)
            if n:
                superseded += n
                vlog(f"  [{game_slug}] superseded {n} stale PENDENTE entry(ies) from an earlier run")
    suggestions.sort(key=lambda s: s["edge"], reverse=True)

    texts = [s.pop("_text") for s in suggestions]
    vlog(f"=== Done: {len(suggestions)} suggestion(s), {len(skipped)} skipped, "
         f"{filtered_non_league} non-MLB + {filtered_non_total} non-run-total filtered ===")

    result = {
        "date": target,
        "portfolio_value": portfolio_value,
        "first_trade_strategy": first_trade,
        "counts": {"games": len(games), "suggestions": len(suggestions),
                   "skipped": len(skipped),
                   "filtered_non_mlb": filtered_non_league,
                   "filtered_non_total": filtered_non_total,
                   "superseded": superseded},
        "suggestions": suggestions,
        "skipped": skipped,
        "disclaimer": "Paper-trading simulation — not financial advice. Real "
                      "trading involves risk of loss. Edge is reconstructed from a "
                      "model; without live inputs the engine returns zero edge.",
    }

    if args.paper and suggestions:
        result["paper_results"] = execute_paper_batch(
            [s["recommendation"] for s in suggestions], not args.paper_execute)

    result["_texts"] = texts
    return result


def advisor_kelly_half():
    from advisor import kelly_half
    return kelly_half


def execute_paper_batch(recs, dry_run):
    from execute_paper import execute_recommendation
    out = []
    for rec in recs:
        try:
            out.append(execute_recommendation(rec, dry_run=dry_run))
        except Exception as e:  # noqa: BLE001
            out.append({"status": "error", "error": str(e), "token_id": rec.get("token_id")})
    return out


def render_text(result: dict) -> str:
    lines = [f"MLB total-runs suggestions — {result['date']}",
             "=" * 64,
             f"Games: {result['counts']['games']}  "
             f"Suggestions: {result['counts']['suggestions']}  "
             f"Skipped: {result['counts']['skipped']}",
             ""]
    if not result.get("_texts"):
        lines.append("No actionable edge found.")
    for t in result.get("_texts", []):
        lines.append(t)
        lines.append("-" * 64)
    if result.get("paper_results"):
        lines.append("Paper results: " + json.dumps(result["paper_results"]))
    lines.append("")
    lines.append(result["disclaimer"])
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description="Suggest MLB total-runs entries on Polymarket.")
    p.add_argument("--date", default=None, help="Target day YYYY-MM-DD (default today UTC)")
    p.add_argument("--min-volume", type=float, default=1000.0,
                   help="Min 24h volume (default 1000; lower than the generic 10k for MLB totals)")
    p.add_argument("--min-hours", type=float, default=0.0,
                   help="Min hours until game start (default 0 = pre-game only, not started)")
    p.add_argument("--all-lines", dest="best_line_only", action="store_false", default=True,
                   help="Suggest every qualifying line (default: best-edge line per game only)")
    p.add_argument("--min-edge", type=float, default=0.05, help="Min edge after fees (default 0.05)")
    p.add_argument("--odds-min", type=float, default=rd.ODDS_MIN_DEFAULT, help="Min decimal payout (default 1.50)")
    p.add_argument("--odds-max", type=float, default=rd.ODDS_MAX_DEFAULT, help="Max decimal payout (default 3.00)")
    p.add_argument("--dispersion", type=float, default=2.0, help="variance = dispersion*mean (default 2.0)")
    p.add_argument("--league-baseline", type=float, default=8.5, help="Neutral game total (default 8.5)")
    p.add_argument("--league-prefix", default="mlb-",
                   help="Only process games whose slug starts with this (default 'mlb-'; '' = all)")
    p.add_argument("--fee-rate", type=float, default=0.0, help="Taker fee base rate (default 0; sports fee-free)")
    p.add_argument("--use-external", dest="use_external", action="store_true", default=True,
                   help="Use external data inputs (default on; falls back gracefully)")
    p.add_argument("--no-external", dest="use_external", action="store_false",
                   help="Disable external inputs -> market-implied (zero-edge) model")
    p.add_argument("--projections-csv", default=None, help="Path to a projections CSV (ToS-clean run-rate source)")
    p.add_argument("--sharp-odds-csv", default=None,
                   help="Sharp reference odds CSV (date,away,home,total_line,over_odds,under_odds) "
                        "-> fair-value anchor; turns the model into a divergence detector")
    p.add_argument("--odds-api-key", default=None,
                   help="The Odds API key (or $ODDS_API_KEY) for live Pinnacle/consensus totals")
    p.add_argument("--no-sharp-discovery", dest="sharp_discovery", action="store_false",
                   default=True,
                   help="Disable using the sharp slate as the authoritative game list "
                        "(default on when a sharp reference is loaded; recovers low-volume "
                        "games the volume-truncated tag misses)")
    p.add_argument("--no-require-sharp", dest="require_sharp", action="store_false",
                   default=True,
                   help="In divergence mode (sharp slate loaded), also evaluate games with NO "
                        "sharp match via the factor model (default OFF: skip them — the factor "
                        "model has no proven edge, so bet only on a sharp anchor)")
    p.add_argument("--refresh-prices", action="store_true", help="Refresh prices via CLOB midpoint")
    p.add_argument("--portfolio-value", type=float, default=10000.0, help="Portfolio USD for sizing")
    p.add_argument("--portfolio-db", default=None, help="Paper portfolio DB (to detect first trade)")
    p.add_argument("--record", dest="record", action="store_true", default=True,
                   help="Record predictions (+stats log) to the predictions DB (default on)")
    p.add_argument("--no-record", dest="record", action="store_false",
                   help="Do not record predictions")
    p.add_argument("--predictions-db", default=predictions_db.DEFAULT_DB,
                   help=f"Predictions DB path (default {predictions_db.DEFAULT_DB})")
    p.add_argument("--paper", action="store_true", help="Pipe suggestions into the paper trader")
    p.add_argument("--paper-execute", action="store_true", help="Actually place paper trades (default dry-run)")
    p.add_argument("--output", choices=["json", "text"], default="json")
    p.add_argument("--rate-limit", type=int, default=100, help="Min ms between API calls")
    p.add_argument("--quiet", dest="verbose", action="store_false", default=True,
                   help="Suppress the per-game analysis logs (stderr)")
    p.add_argument("--debug", action="store_true", help="Also log every API call")
    args = p.parse_args()

    try:
        result = run(args)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)

    if args.output == "text":
        print(render_text(result))
    else:
        result.pop("_texts", None)
        print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
