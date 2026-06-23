#!/usr/bin/env python3
"""Suggest tennis MATCH-WINNER (moneyline) entries on Polymarket.

Pipeline (analog of suggest_soccer.py): discover the day's tennis matches per real
tour/tournament tag -> classify the moneyline market -> model P(winner) with the
surface-aware Elo engine -> edge vs the Polymarket price -> half-Kelly sizing under
the constitution caps -> record the best side per match (PENDENTE) + shadow-log all.

Anti-fabrication (CLAUDE.md): if a player has no rating, the model is MARKET-IMPLIED
(devigged price), so edge ~ 0 and nothing is suggested. Real edge appears only when a
rating source moves P(win) off the market. Market text is untrusted (rule #5).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import _bootstrap  # noqa: F401  (adds category-watcher scripts to sys.path)

import elo
import forecast_tennis as fct
import ratings as ratings_mod
import ratings_source
import tennis_market as tm
import tennis_predictions as tdb
import sharp_odds_tennis as sot

from category_common import APIClient, discover_markets, game_date, log

STRATEGY = "tennis-elo-moneyline"
CAP_MODEL = 0.05            # per-trade cap (model edge)
CAP_FIRST_TRADE = 0.01     # first trade with a new strategy
# On a near-efficient match-winner market, an edge this large signals MODEL error (the Elo
# over/under-rating a player), not value -> flagged implausible and skipped. Same as MLB/soccer.
MAX_PLAUSIBLE_EDGE = 0.15
# Discovery: probe a page to classify a tag honored vs global-mix (Gamma ignores unknown
# tag slugs and returns the global volume mix). Mirrors the soccer skill.
PROBE_MAX = 100            # one page, to classify the tag
PER_TAG_MAX = 600          # full pull for an honored tag
DEEP_MAX = 2500            # fallback broad pass when no tag is honored
HONORED_FRAC = 0.60        # >= this tennis fraction in the probe => honored tag


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def is_tennis_slug(slug: str) -> bool:
    """A match slug is tennis if its tag prefix is a known tour/tournament tag."""
    s = (slug or "").lower()
    return any(s.startswith(t + "-") for t in tm.TENNIS_TAGS)


def group_by_match(markets: list[dict]) -> dict:
    out: dict[str, list[dict]] = {}
    for m in markets:
        key = m.get("event_slug") or m.get("slug")
        if key:
            out.setdefault(key, []).append(m)
    return out


def _is_tennis(m) -> bool:
    return is_tennis_slug(m.get("event_slug") or m.get("slug") or "")


def discover_tennis(api, vlog) -> list[dict]:
    """Union the day's tennis markets across the tour tags (deduped, tennis-only).

    Probe one page per tag; honored tags (~all tennis) are paginated in full; tags that
    return the global mix are not deep-paginated. If Gamma honors no tag, fall back to one
    DEEP broad pass so matches aren't lost behind a 1-page probe. (Same approach as the
    soccer skill — Gamma ignores unknown tag slugs and returns the global volume mix.)
    """
    markets, seen = [], set()
    honored = []

    def _add(ms) -> int:
        n = 0
        for m in ms:
            if not _is_tennis(m):
                continue
            key = m.get("condition_id") or m.get("slug")
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            markets.append(m)
            n += 1
        return n

    for tag in tm.TENNIS_TAGS:
        try:
            _t, probe = discover_markets(api, "tennis", [tag], min_volume=0.0,
                                         max_markets=PROBE_MAX, include_closed=False)
        except Exception as e:  # noqa: BLE001
            vlog(f"  tag {tag!r} failed: {e}")
            continue
        if not probe:
            continue
        frac = sum(1 for m in probe if _is_tennis(m)) / len(probe)
        if frac >= HONORED_FRAC:
            try:
                _t, full = discover_markets(api, "tennis", [tag], min_volume=0.0,
                                            max_markets=PER_TAG_MAX, include_closed=False)
            except Exception:  # noqa: BLE001
                full = probe
            vlog(f"  tag {tag!r}: HONORED +{_add(full)} tennis (probe {frac:.0%})")
            honored.append(tag)
        else:
            _add(probe)            # global mix: keep its tennis subset, don't paginate deeper
    if not honored:
        try:
            _t, deep = discover_markets(api, "tennis", ["tennis"], min_volume=0.0,
                                        max_markets=DEEP_MAX, include_closed=False)
            vlog(f"  no honored tags -> deep broad pass: +{_add(deep)} tennis "
                 f"(of {len(deep)} scanned)")
        except Exception as e:  # noqa: BLE001
            vlog(f"  deep broad pass failed: {e}")
    return markets


def model_probability(player_a, player_b, surface, rt, blend_w):
    """Return (p_a, used_external, elo_a, elo_b). Market-implied fallback if uncovered."""
    ra = ratings_mod.resolve(player_a, rt) if player_a else None
    rb = ratings_mod.resolve(player_b, rt) if player_b else None
    if ra and rb:
        ea = elo.blended_elo(ra, surface, blend_w)
        eb = elo.blended_elo(rb, surface, blend_w)
        return elo.expected(ea, eb), True, ea, eb
    return None, False, None, None


def pick_side(sides, p_a, fee_rate, odds_min, odds_max, max_edge=MAX_PLAUSIBLE_EDGE):
    """Choose the higher-edge side that passes the odds band. sides: [A, B] dicts with
    label/token/price. p_a = model P(side A wins). Returns (chosen|None, notes).

    A side whose edge exceeds `max_edge` is flagged `implausible` and excluded — on a
    near-efficient market a huge edge signals model error, not value."""
    probs = (p_a, 1.0 - p_a)
    notes, cands = [], []
    for i, s in enumerate(sides):
        price = s["price"]
        if price is None:
            continue
        p = probs[i]
        e = elo.edge(p, price)
        band = elo.passes_odds_band(price, odds_min, odds_max)
        implausible = e > max_edge
        notes.append({"label": s["label"], "price": price, "p_model": round(p, 4),
                      "edge": round(e, 4), "in_odds_band": band, "implausible": implausible})
        if band and not implausible:
            cands.append({"side": s["label"], "token": s["token"], "price": price,
                          "opponent": sides[1 - i]["label"], "p_model": p, "edge": e})
    if not cands:
        return None, notes
    cands.sort(key=lambda c: c["edge"], reverse=True)
    return cands[0], notes


def _load_sharp_lookup(args, target, vlog) -> dict:
    """Load the sharp h2h reference (Pinnacle via The Odds API) -> divergence detector.

    Lists active tennis tours, then queries each. Best-effort / {} offline. With it the
    model anchors P(win) to the sharp; without it it stays Elo-predictive (edge-capped).
    """
    if getattr(args, "no_sharp", False):
        return {}
    key = getattr(args, "odds_api_key", None) or os.environ.get("ODDS_API_KEY")
    if not key:
        vlog("  sharp source: none (no ODDS_API_KEY) -> Elo model (edge-capped)")
        return {}
    keys = sot.fetch_active_tennis_keys(key, vlog=vlog)
    only = getattr(args, "sharp_tours", None)
    if only:
        wanted = {x.strip().lower() for x in only.split(",") if x.strip()}
        keys = [k for k in keys if any(w in k.lower() for w in wanted)]
    lookup = sot.fetch_sharp_tennis(key, keys, date=target,
                                    min_quota_reserve=int(getattr(args, "sharp_min_reserve", 0) or 0),
                                    vlog=vlog)
    if lookup:
        # k = (date, frozenset(surnames)); count how many are dated for today's slate so a
        # date-boundary mismatch (a night match crossing UTC midnight) is visible in the log.
        dated = sum(1 for k in lookup if k[0] == target)
        vlog(f"  sharp reference loaded: {len(lookup)} match(es) "
             f"({dated} dated {target}) (divergence-detector mode)")
    else:
        vlog("  sharp reference EMPTY — divergence detector OFF "
             "(model stays Elo-predictive, edge-capped). Check the [odds-api] lines above: "
             "0 tours, quota reserve hit, or no h2h parsed.")
    return lookup


def run(args) -> dict:
    api = APIClient(rate_limit_ms=args.rate_limit, debug=args.debug)
    vlog = log if getattr(args, "verbose", True) else (lambda *a, **k: None)
    target = args.date or now_utc().date().isoformat()
    vlog(f"=== Tennis match-winner analysis for {target} ===")

    try:
        markets = discover_tennis(api, vlog)
    except Exception as e:  # noqa: BLE001
        return {"date": target, "error": f"discovery failed: {e}", "suggestions": [], "skipped": []}

    on_day = [m for m in markets if game_date(m) == target]
    matches = group_by_match(on_day)
    vlog(f"Discovery: {len(tm.TENNIS_TAGS)} tags -> {len(markets)} markets; "
         f"{len(on_day)} dated {target}, {len(matches)} matches")

    match_evts = {k: v for k, v in matches.items()
                  if is_tennis_slug(k) and any(tm.is_match_market(x) for x in v)}
    vlog(f"  {len(match_evts)} tennis moneyline matches "
         f"({len(matches) - len(match_evts)} other events dropped)")

    # Ratings: explicit CSV wins; else auto-compute surface Elo from Sackmann data
    # (network, cached; empty offline -> market-implied). --no-auto-ratings forces CSV-only.
    if args.ratings_csv:
        rt = ratings_mod.load_ratings(args.ratings_csv)
    elif args.auto_ratings:
        rt = ratings_source.auto_ratings(tour=args.tour, debug=args.debug)
        vlog(f"  auto ratings ({args.tour}): {len(rt)} players")
    else:
        rt = {}
    portfolio_value = float(args.portfolio_value)
    sharp_lookup = _load_sharp_lookup(args, target, vlog)
    require_sharp = bool(sharp_lookup) and getattr(args, "require_sharp", True)
    suggestions, skipped, cand_rows = [], [], []

    def _skip(slug, reason, **extra):
        rec = {"match": slug, "reason": reason}; rec.update(extra)
        skipped.append(rec); vlog(f"  [{slug}] SKIP — {reason}")

    for slug, mks in match_evts.items():
        m = next((x for x in mks if tm.is_match_market(x)), None)
        if not m:
            _skip(slug, "no moneyline market"); continue
        ms = tm.match_sides(m)
        if not ms or any(s["price"] is None for s in ms["sides"]):
            _skip(slug, "could not map moneyline tokens/prices"); continue
        surface = args.surface or tm.surface_for(slug)
        pa_slug, pb_slug = tm.parse_players(slug)
        # Prefer the market's own outcome labels for rating resolution; fall back to slug.
        label_a, label_b = ms["sides"][0]["label"], ms["sides"][1]["label"]
        p_a, used, ea, eb = model_probability(label_a or pa_slug, label_b or pb_slug,
                                              surface, rt, args.blend)
        # Sharp anchor (divergence detector): when the sharp prices this match, use its fair
        # P(win) directly — edge then measures Polymarket vs sharp, not Elo vs market.
        sharp_pa = (sot.sharp_win_ref(sharp_lookup, target, label_a or pa_slug, label_b or pb_slug)
                    if sharp_lookup else None)
        if sharp_pa is not None:
            p_a, used = sharp_pa, used
        elif require_sharp:
            _skip(slug, "no sharp reference (divergence mode bets only on a sharp anchor)",
                  price_sane=ms["price_sane"])
            continue
        elif p_a is None:                     # anti-fabrication: devig -> ~0 edge
            fair = elo.devig_two_way(ms["sides"][0]["price"], ms["sides"][1]["price"])
            p_a = fair[0] if fair else ms["sides"][0]["price"]
        chosen, notes = pick_side(ms["sides"], p_a, args.fee_rate, args.odds_min, args.odds_max,
                                  getattr(args, "max_edge", MAX_PLAUSIBLE_EDGE))
        vlog(f"  [{slug}] {label_a} vs {label_b} ({surface}): P({label_a})={p_a:.3f} "
             f"external={used}"
             + (f" sharp={sharp_pa:.3f}" if sharp_pa is not None else (" (no sharp ref)" if sharp_lookup else ""))
             + (f" elo={ea:.0f}/{eb:.0f}" if used and sharp_pa is None else ""))
        ref = ms["sides"][0]
        cand = {"slug": slug, "surface": surface, "p_a": p_a, "used": used,
                "elo_a": ea, "elo_b": eb, "sides": ms["sides"], "ou": ms, "m": m,
                "chosen": chosen, "notes": notes, "ref_token": ref["token"],
                "ref_label": label_a, "ref_price": ref["price"]}
        if not chosen:
            impl = next((n for n in notes
                         if n.get("implausible") and n["edge"] > 0 and n["in_odds_band"]), None)
            reason = (f"edge {impl['edge']:.1%} implausibly large (> {MAX_PLAUSIBLE_EDGE:.0%} cap) "
                      f"— likely model error" if impl else "no side in odds band")
            _skip(slug, reason, price_sane=ms["price_sane"])
            cand_rows.append({**cand, "bet": 0, "skip_reason": reason})
            continue
        if chosen["edge"] < args.min_edge:
            _skip(slug, f"edge {chosen['edge']*100:.1f}% < {args.min_edge*100:.0f}%")
            cand_rows.append({**cand, "bet": 0, "skip_reason": "edge below threshold"})
            continue
        cand_rows.append({**cand, "bet": 1, "skip_reason": None})

    # Record bettable candidates (one row per match already), shadow-log everything.
    recorded_ids: dict = {}
    for c in cand_rows:
        _shadow_log(c, args, target)
        if c["bet"] != 1 or not c["chosen"]:
            continue
        ch = c["chosen"]
        size_pct, size_usd, kelly, conf = _size(ch, args, portfolio_value)
        rec_id = _record(c, ch, size_pct, size_usd, kelly, conf, args, target)
        if rec_id is not None:
            recorded_ids.setdefault(c["slug"], set()).add(rec_id)
        suggestions.append({"match": c["slug"], "surface": c["surface"],
                            "side": ch["side"], "opponent": ch["opponent"],
                            "price": ch["price"], "edge": round(ch["edge"], 4),
                            "p_model": round(ch["p_model"], 4),
                            "forecast": fct.forecast_block(ch["p_model"]),
                            "size_pct": round(size_pct, 5), "prediction_id": rec_id})
        vlog(f"  [{c['slug']}] >>> SUGGEST {ch['side']} @ {ch['price']:.3f} "
             f"edge={ch['edge']*100:+.1f}% size={size_pct*100:.2f}% pred_id={rec_id}")

    superseded = 0
    if args.record:
        for slug, keep in recorded_ids.items():
            superseded += tdb.supersede_pending(args.predictions_db, slug, keep)

    suggestions.sort(key=lambda s: s["edge"], reverse=True)
    vlog(f"=== Done: {len(suggestions)} suggestion(s), {len(skipped)} skipped ===")
    return {"date": target,
            "counts": {"matches": len(match_evts), "suggestions": len(suggestions),
                       "skipped": len(skipped), "superseded": superseded},
            "suggestions": suggestions, "skipped": skipped,
            "disclaimer": "Paper-trading simulation — not financial advice. Without "
                          "player ratings the engine is market-implied (zero edge)."}


def _size(chosen, args, portfolio_value):
    kelly = elo.half_kelly(chosen["p_model"], chosen["price"])
    conf = min(1.0, max(0.0, 0.5 + chosen["edge"]))
    cap = CAP_FIRST_TRADE if not args.record else CAP_MODEL
    size_pct = min(kelly, cap)
    if conf < 0.7:
        size_pct = min(size_pct, 0.05)
    return size_pct, round(size_pct * portfolio_value, 2), kelly, conf


def _record(c, chosen, size_pct, size_usd, kelly, conf, args, target):
    if not args.record:
        return None
    stats = {"model": "surface_elo", "surface": c["surface"], "elo_side": c["elo_a"]
             if chosen["side"] == c["ref_label"] else c["elo_b"],
             "elo_opp": c["elo_b"] if chosen["side"] == c["ref_label"] else c["elo_a"],
             "p_model": round(chosen["p_model"], 4), "edge": round(chosen["edge"], 4),
             "forecast": fct.forecast_block(chosen["p_model"]),
             "used_external": c["used"], "blend": args.blend, "notes": c["notes"]}
    try:
        return tdb.record_prediction({
            "match_slug": c["slug"], "match_date": target,
            "tour": (c["slug"].split("-")[0] if c["slug"] else None),
            "surface": c["surface"], "market_question": c["m"].get("question", ""),
            "condition_id": c["m"].get("condition_id"), "token_id": chosen["token"],
            "side": chosen["side"], "opponent": chosen["opponent"],
            "entry_price": chosen["price"], "decimal_odds": elo.decimal_odds(chosen["price"]),
            "model_prob": chosen["p_model"], "edge": chosen["edge"],
            "elo_side": stats["elo_side"], "elo_opp": stats["elo_opp"], "confidence": conf,
            "size_pct": size_pct, "size_usd": size_usd, "kelly_fraction": kelly,
            "used_external": c["used"], "fee_rate": args.fee_rate, "strategy": STRATEGY,
            "market_url": _match_url(c["slug"]), "stats": stats,
        }, args.predictions_db)
    except Exception as e:  # noqa: BLE001
        if args.debug:
            print(f"[record] failed: {e}", file=sys.stderr)
        return None


def _shadow_log(c, args, target):
    if not args.record:
        return
    ch = c["chosen"]
    try:
        tdb.record_model_log({
            "match_slug": c["slug"], "match_date": target,
            "tour": (c["slug"].split("-")[0] if c["slug"] else None), "surface": c["surface"],
            "ref_side": c["ref_label"], "ref_prob": round(c["p_a"], 4),
            "ref_price": c["ref_price"], "ref_token": c["ref_token"],
            "pick_side": ch["side"] if ch else None,
            "pick_edge": round(ch["edge"], 4) if ch else None,
            "used_external": c["used"],
            "model_params": {"elo_a": c["elo_a"], "elo_b": c["elo_b"], "surface": c["surface"]},
            "bet": c["bet"], "skip_reason": c["skip_reason"], "market_url": _match_url(c["slug"]),
        }, args.predictions_db)
    except Exception as e:  # noqa: BLE001
        if args.debug:
            print(f"[model_log] failed: {e}", file=sys.stderr)


def _match_url(slug: str) -> str:
    return f"https://polymarket.com/event/{tm.base_match_slug(slug)}"


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Suggest tennis match-winner entries on Polymarket.")
    p.add_argument("--date", default=None, help="Target day YYYY-MM-DD (UTC)")
    p.add_argument("--ratings-csv", default=None, help="player,elo,hard,clay,grass CSV (overrides auto)")
    p.add_argument("--auto-ratings", dest="auto_ratings", action="store_true", default=True,
                   help="Auto-compute surface Elo from Sackmann data (default on)")
    p.add_argument("--no-auto-ratings", dest="auto_ratings", action="store_false")
    p.add_argument("--tour", choices=("atp", "wta"), default="atp", help="Tour for auto ratings")
    p.add_argument("--surface", choices=("hard", "clay", "grass"), default=None,
                   help="Override surface (else inferred from the slug/tournament)")
    p.add_argument("--blend", type=float, default=elo.SURFACE_BLEND, help="overall/surface Elo blend")
    p.add_argument("--odds-min", type=float, default=elo.ODDS_MIN_DEFAULT)
    p.add_argument("--odds-max", type=float, default=elo.ODDS_MAX_DEFAULT)
    p.add_argument("--min-edge", type=float, default=0.05)
    p.add_argument("--max-edge", type=float, default=MAX_PLAUSIBLE_EDGE,
                   help=f"Reject a side whose edge exceeds this as likely model error (default {MAX_PLAUSIBLE_EDGE})")
    p.add_argument("--odds-api-key", default=None,
                   help="The Odds API key (or $ODDS_API_KEY) -> sharp anchor (divergence detector)")
    p.add_argument("--no-sharp", action="store_true", help="Disable the sharp anchor (Elo model, capped)")
    p.add_argument("--sharp-tours", default=None,
                   help="Comma substrings to limit sharp tours (e.g. 'atp,wta'); default all active")
    p.add_argument("--no-require-sharp", dest="require_sharp", action="store_false", default=True,
                   help="With a sharp slate loaded, still model matches with NO sharp match "
                        "(default OFF: skip them — bet only on a sharp anchor)")
    p.add_argument("--sharp-min-reserve", type=int, default=0,
                   help="Stop the sharp fetch once Odds-API remaining quota hits this floor (0 = no reserve)")
    p.add_argument("--fee-rate", type=float, default=0.0)
    p.add_argument("--portfolio-value", type=float, default=10000.0)
    p.add_argument("--predictions-db", default=tdb.DEFAULT_DB)
    p.add_argument("--record", dest="record", action="store_true", default=True)
    p.add_argument("--no-record", dest="record", action="store_false")
    p.add_argument("--output", choices=("json", "text"), default="json")
    p.add_argument("--quiet", dest="verbose", action="store_false", default=True)
    p.add_argument("--debug", action="store_true")
    p.add_argument("--rate-limit", type=int, default=0)
    return p


def main() -> None:
    args = build_arg_parser().parse_args()
    result = run(args)
    if args.output == "json":
        print(json.dumps(result, indent=2, default=str))
    else:
        for s in result.get("suggestions", []):
            print(f"{s['match']}: {s['side']} @ {s['price']:.3f} "
                  f"edge={s['edge']*100:+.1f}% ({s['surface']})")
        if not result.get("suggestions"):
            print("No actionable edge found.")


if __name__ == "__main__":
    main()
