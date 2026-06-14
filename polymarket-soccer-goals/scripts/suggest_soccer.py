#!/usr/bin/env python3
"""Suggest Over/Under TOTAL-GOALS and BTTS entries for the day's soccer games.

Pipeline: today's soccer games on Polymarket (via the category scanner) -> for
each full-game total-goals or BTTS market -> Dixon-Coles model of P(Over)/P(BTTS)
-> edge vs the Polymarket price -> 1.60x-3.0x payout filter -> pre-game decision
tree -> half-Kelly capped per CLAUDE.md -> recommendation(s). Records each
prediction (status PENDENTE) for later analysis.

Paper-first; read/analysis only. Not financial advice.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

import _bootstrap  # noqa: F401

import dixon_coles as dc
import leagues
import soccer_market as sm
import data_inputs
import baselines_source as bsrc
import soccer_predictions as spdb

from category_common import (
    APIClient, discover_markets, fetch_midpoint, game_date, log,
    resolve_category, sanitize_text,
)

STRATEGY = "soccer-goals-dc"
CAP_MODEL = 0.02
CAP_FIRST_TRADE = 0.01


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def fee_for(price: float, fee_rate: float) -> float:
    return 0.0 if (not fee_rate or price is None) else fee_rate * min(price, 1.0 - price)


def group_by_event(markets: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for m in markets:
        key = m.get("event_slug") or m.get("slug")
        if key:
            out.setdefault(key, []).append(m)
    return out


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


def pick_side(sides, fee_rate, odds_min, odds_max):
    """sides = [(name, token, price, p_model)]. Return (chosen|None, notes)."""
    notes, candidates = [], []
    for name, token, price, p in sides:
        if price is None:
            continue
        edge = p - price - fee_for(price, fee_rate)
        in_band = dc.passes_odds_filter(price, odds_min, odds_max)
        notes.append({"side": name, "price": round(price, 4), "p_model": round(p, 4),
                      "edge": round(edge, 4), "in_odds_band": in_band})
        if edge > 0 and in_band:
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

    category_key, candidates = resolve_category("soccer")
    try:
        _tag, markets = discover_markets(api, category_key, candidates,
                                         min_volume=0.0, include_closed=False)
    except Exception as e:  # noqa: BLE001
        return {"date": target, "error": f"discovery failed: {e}", "suggestions": [], "skipped": []}

    on_day = [m for m in markets if game_date(m) == target]
    games = group_by_event(on_day)
    vlog(f"Discovery: tag '{_tag}' -> {len(markets)} markets; {len(on_day)} dated {target}, {len(games)} events")

    # Keep only soccer total-goals and BTTS markets.
    total_evts = {k: v for k, v in games.items()
                  if leagues.is_soccer_slug(k) and sm.GAME_TOTAL_RE.search(k.lower())}
    btts_evts = {k: v for k, v in games.items()
                 if leagues.is_soccer_slug(k) and sm.GAME_BTTS_RE.search(k.lower())}
    filtered_non_soccer = len(games) - len(total_evts) - len(btts_evts)
    vlog(f"  {len(total_evts)} total-goals + {len(btts_evts)} BTTS soccer markets "
         f"({filtered_non_soccer} other events dropped)")

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
        calibrated = bsrc.calibrate_baselines(prefixes, token, debug=args.debug)
        if calibrated:
            vlog("  baselines calibrated: " + ", ".join(
                f"{p}={v:.2f}(was {leagues.LEAGUE_BASELINES.get(p, leagues.DEFAULT_BASELINE):.2f})"
                for p, v in sorted(calibrated.items())))

    suggestions, skipped = [], []

    def _skip(slug, reason, **extra):
        rec = {"game": slug, "reason": reason}; rec.update(extra)
        skipped.append(rec); vlog(f"  [{slug}] SKIP — {reason}")

    def _inputs_for(slug):
        home, away = leagues.parse_teams(slug, home_first=args.home_first)
        inp = data_inputs.get_match_inputs(api, home, away, leagues.league_prefix(slug),
                                           ratings=ratings, auto=args.auto_ratings,
                                           international=leagues.is_international(slug),
                                           debug=args.debug)
        total, sup, used = derive_total_supremacy(inp, bsrc.baseline_for(slug, calibrated),
                                                  leagues.is_neutral(slug))
        return inp, total, sup, used

    # --- TOTAL-GOALS markets ---
    for slug, gmarkets in total_evts.items():
        m = next((x for x in gmarkets if sm.is_total_market(x)), None)
        if not m:
            _skip(slug, "no total-goals market"); continue
        line = sm.parse_total_line(m)
        ou = sm.over_under_tokens(m)
        if line is None or not ou or ou["over_price"] is None or ou["under_price"] is None:
            _skip(slug, "could not parse total line/tokens"); continue
        _inp, total, sup, used = _inputs_for(slug)
        if used:
            lam_h, lam_a = dc.lambdas_from_total_supremacy(total, sup)
        else:
            lam_h, lam_a = dc.market_implied_lambdas(line, ou["over_price"])
        probs = dc.prob_over(line, dc.score_matrix(lam_h, lam_a, args.rho))
        chosen, notes = pick_side([("OVER", ou["over_token"], ou["over_price"], probs["p_over_eff"]),
                                   ("UNDER", ou["under_token"], ou["under_price"], probs["p_under_eff"])],
                                  args.fee_rate, args.odds_min, args.odds_max)
        _emit_or_skip("TOTAL", slug, m, line, chosen, notes, lam_h, lam_a, used,
                      ou["book_sum"], ou["price_sane"], args, portfolio_value, first_trade, kh,
                      suggestions, _skip, target)

    # --- BTTS markets ---
    for slug, gmarkets in btts_evts.items():
        m = next((x for x in gmarkets if sm.is_btts_market(x)), None)
        if not m:
            _skip(slug, "no BTTS market"); continue
        bt = sm.btts_tokens(m)
        if not bt or bt["yes_price"] is None or bt["no_price"] is None:
            _skip(slug, "could not map BTTS tokens"); continue
        _inp, total, sup, used = _inputs_for(slug)
        if used:
            lam_h, lam_a = dc.lambdas_from_total_supremacy(total, sup)
        else:
            lam_h, lam_a = dc.market_implied_from_btts(bt["yes_price"])
        probs = dc.prob_btts(dc.score_matrix(lam_h, lam_a, args.rho))
        chosen, notes = pick_side([("YES", bt["yes_token"], bt["yes_price"], probs["p_yes"]),
                                   ("NO", bt["no_token"], bt["no_price"], probs["p_no"])],
                                  args.fee_rate, args.odds_min, args.odds_max)
        _emit_or_skip("BTTS", slug, m, None, chosen, notes, lam_h, lam_a, used,
                      bt["book_sum"], bt["price_sane"], args, portfolio_value, first_trade, kh,
                      suggestions, _skip, target)

    # Best-line-per-game for TOTAL markets (BTTS is already one per matchup).
    if args.best_line_only:
        best = {}
        for s in suggestions:
            key = (sm.GAME_TOTAL_RE.sub("", s["game"]), s["market"])
            if key not in best or s["edge"] > best[key]["edge"]:
                best[key] = s
        suggestions = sorted(best.values(), key=lambda s: s["edge"], reverse=True)

    texts = [s.pop("_text") for s in suggestions]
    vlog(f"=== Done: {len(suggestions)} suggestion(s), {len(skipped)} skipped ===")

    result = {
        "date": target,
        "counts": {"total_markets": len(total_evts), "btts_markets": len(btts_evts),
                   "suggestions": len(suggestions), "skipped": len(skipped)},
        "suggestions": suggestions, "skipped": skipped,
        "disclaimer": "Paper-trading simulation — not financial advice. Without live "
                      "inputs the Dixon-Coles engine returns zero edge (market-implied).",
        "_texts": texts,
    }
    return result


def _emit_or_skip(market_type, slug, m, line, chosen, notes, lam_h, lam_a, used,
                  book_sum, price_sane, args, portfolio_value, first_trade, kh,
                  suggestions, _skip, target):
    if not chosen:
        _skip(slug, "no positive-edge side within 1.60x-3.0x band", market=market_type, sides=notes)
        return
    passed, reason = decision_tree(chosen, m, book_sum, price_sane,
                                   min_volume=args.min_volume, min_edge=args.min_edge,
                                   min_hours=args.min_hours)
    if not passed:
        _skip(slug, reason, market=market_type, side=chosen["side"]); return
    size_pct, size_usd, kelly = size_position(chosen["p_model"], chosen["price"],
                                              portfolio_value, first_trade, kh)
    if kelly <= 0:
        _skip(slug, "Kelly <= 0", market=market_type); return
    if size_usd < 10:
        _skip(slug, f"size ${size_usd:.2f} below $10 minimum", market=market_type); return

    confidence = max(0.5, min(0.5 + chosen["edge"], 0.65)) if used else 0.5
    price = chosen["price"]
    odds = dc.decimal_odds(price)
    market_url = leagues.game_url(slug)
    desc = (f"{market_type} {chosen['side']}" + (f" {line}" if line is not None else "")
            + f" @ {price:.3f} (payout {odds:.2f}x) edge {chosen['edge']*100:+.1f}% "
            + ("Dixon-Coles" if used else "market-implied (fallback)"))
    stats = {
        "model": "dixon_coles", "market": market_type, "chosen_side": chosen["side"],
        "lam_home": round(lam_h, 4),
        "lam_away": round(lam_a, 4), "rho": args.rho, "line": line,
        "model_prob": round(chosen["p_model"], 4), "entry_price": price,
        "decimal_odds": round(odds, 4), "edge_after_fee": round(chosen["edge"], 4),
        "used_external": used, "book_sum": book_sum, "price_sane": price_sane,
        "kelly_fraction": round(kelly, 5), "size_pct": round(size_pct, 5),
        "size_usd": round(size_usd, 2), "confidence": round(confidence, 3), "sides": notes,
    }
    pred_id = None
    if args.record:
        try:
            pred_id = spdb.record_prediction({
                "game_slug": slug, "game_date": target, "league": leagues.league_prefix(slug),
                "market": market_type, "market_question": sanitize_text(m.get("question", "")),
                "condition_id": m.get("condition_id"), "token_id": chosen["token"], "line": line,
                "side": chosen["side"], "entry_price": price, "decimal_odds": odds,
                "model_prob": chosen["p_model"], "edge": chosen["edge"], "lam_home": lam_h,
                "lam_away": lam_a, "rho": args.rho, "confidence": confidence, "size_pct": size_pct,
                "size_usd": size_usd, "kelly_fraction": kelly, "used_external": used,
                "fee_rate": args.fee_rate, "strategy": STRATEGY, "market_url": market_url,
                "stats": stats,
            }, args.predictions_db)
        except Exception as e:  # noqa: BLE001
            if args.debug:
                print(f"[record] failed: {e}", file=sys.stderr)

    rec = {"token_id": chosen["token"], "side": "YES", "action": "BUY",
           "size_pct": round(size_pct, 4), "price": round(price, 4),
           "confidence": round(confidence, 3), "reasoning": sanitize_text(desc),
           "strategy": STRATEGY, "fee_rate": args.fee_rate}
    text = (f"Market: {sanitize_text(m.get('question',''))}  [{slug}]\n"
            f"Edge type: news-driven (Dixon-Coles model)\n{desc}\n"
            f"Size: ${size_usd:,.2f} ({size_pct*100:.2f}%)  Confidence: {confidence:.2f}")
    suggestions.append({"game": slug, "market": market_type, "side": chosen["side"],
                        "line": line, "edge": round(chosen["edge"], 4),
                        "lam_home": round(lam_h, 3), "lam_away": round(lam_a, 3),
                        "prediction_id": pred_id, "recommendation": rec, "_text": text})


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
    p.add_argument("--min-volume", type=float, default=1000.0)
    p.add_argument("--min-edge", type=float, default=0.05)
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
