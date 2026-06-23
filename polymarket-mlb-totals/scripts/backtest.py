#!/usr/bin/env python3
"""Walk-forward backtest of the MLB total-runs model over historical seasons.

Validates the (market-anchored) Over/Under model the way it trades: for each game it
builds POINT-IN-TIME team run factors from only the games already played (no
look-ahead), runs the same `model_probabilities` + `pick_side` the live skill uses,
settles against the final score, and aggregates ROI / win rate / Brier / log-loss /
calibration / CLV by season.

Data: a normalized CSV of historical games with odds (one row per game):

    date,away,home,away_score,home_score,total_line,over_odds,under_odds,close_over_odds,close_under_odds

  - date         YYYY-MM-DD (or YYYYMMDD)
  - away,home    team abbreviations (any consistent scheme; matched case-insensitively)
  - *_score      final runs (integers)
  - total_line   the Over/Under line (e.g. 8.5)
  - over_odds/under_odds        opening (or the price you'd bet) — American (-110) OR
                                decimal (1.91) OR implied prob (0.524). Auto-detected.
  - close_*_odds (optional)     closing line, for CLV.

Sportsbook odds carry vig; we DEVIG each game to a Polymarket-equivalent fair price
(prices summing to 1) and treat that as the traded price, matching how the model runs
on Polymarket (binary $1 payout, ~no vig). Free source: sportsbookreviewsonline.com
MLB odds (one workbook/year → export to this CSV), or a Kaggle MLB odds dataset.

Pure-stdlib core (factors, settlement, metrics) is offline-testable; the model call
reuses the live `suggest_totals`/`run_distribution` code so the backtest can never
drift from what actually trades.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from collections import defaultdict

import _bootstrap  # noqa: F401
import run_distribution as rd
import suggest_totals as st
import forecast as fc
import scoring
import calibration_core as cc

WARMUP_GAMES = 20            # a team needs this many prior games before we trust its factor
DEFAULT_LEAGUE_BASELINE = 8.5
DEFAULT_DISPERSION = 2.0


# ---------------------------------------------------------------------------
# Odds parsing / devig
# ---------------------------------------------------------------------------


def to_implied_prob(value) -> float | None:
    """American (-110/+120), decimal (1.91), or implied prob (0.524) -> implied prob."""
    if value in (None, ""):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if 0.0 < v < 1.0:
        return v                                  # already a probability
    if v >= 100 or v <= -100:                      # American
        return 100.0 / (v + 100.0) if v > 0 else (-v) / (-v + 100.0)
    if v > 1.0:                                    # decimal odds
        return 1.0 / v
    return None


def devig_two_way(p_over: float | None, p_under: float | None) -> tuple[float, float] | None:
    """Two raw implied probs -> fair (no-vig) probs summing to 1."""
    if not p_over or not p_under or p_over <= 0 or p_under <= 0:
        return None
    s = p_over + p_under
    return p_over / s, p_under / s


# ---------------------------------------------------------------------------
# Point-in-time team factors (walk-forward, no look-ahead)
# ---------------------------------------------------------------------------


class TeamFactors:
    """Accumulates runs scored/allowed per team to yield point-in-time factors.

    `factors_for` uses ONLY games already fed via `update`, so a game's factors never
    see its own or any future result. Reset per season.
    """

    def __init__(self, warmup: int = WARMUP_GAMES):
        self.warmup = warmup
        self.rs: dict[str, float] = defaultdict(float)
        self.ra: dict[str, float] = defaultdict(float)
        self.g: dict[str, int] = defaultdict(int)

    def _league_rpg(self) -> float | None:
        teams = [t for t in self.g if self.g[t] > 0]
        if not teams:
            return None
        return sum(self.rs[t] / self.g[t] for t in teams) / len(teams)

    def factors_for(self, away: str, home: str) -> dict:
        """{home_off,away_off,home_sp,away_sp} or {} if either team is below warmup."""
        if self.g[away] < self.warmup or self.g[home] < self.warmup:
            return {}
        lg = self._league_rpg()
        if not lg or lg <= 0:
            return {}
        def off(t): return (self.rs[t] / self.g[t]) / lg
        def pit(t): return (self.ra[t] / self.g[t]) / lg
        return {"home_off": off(home), "away_off": off(away),
                "home_sp": pit(home), "away_sp": pit(away), "home_field": 0.10}

    def update(self, away: str, home: str, away_score: float, home_score: float) -> None:
        self.rs[away] += away_score; self.ra[away] += home_score; self.g[away] += 1
        self.rs[home] += home_score; self.ra[home] += away_score; self.g[home] += 1


# ---------------------------------------------------------------------------
# Game loading
# ---------------------------------------------------------------------------


def _norm_date(s: str) -> str:
    s = (s or "").strip().replace("/", "-")
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


def load_games(csv_path: str) -> list[dict]:
    """Load + normalize the games CSV, sorted chronologically.

    Auto-detects two layouts:
      - NORMALIZED: date,away,home,away_score,home_score,total_line,over_odds,under_odds[,close_*]
      - LONG (one row per team): date,team,opponent,runs,oppRuns,total,overOdds,underOdds
        (e.g. the public oddsDataMLB export) — the mirror rows are deduped by keeping the
        team<opponent orientation; home/away is arbitrary (irrelevant for a TOTAL).
    """
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = {(c or "").strip().lower() for c in (reader.fieldnames or [])}
        long_fmt = {"opponent", "runs", "oppruns", "total"} <= cols
        out = []
        for r in reader:
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
            g = _parse_long(row) if long_fmt else _parse_normalized(row)
            if g and g["date"] and g["away"] and g["home"]:
                out.append(g)
    out.sort(key=lambda g: g["date"])
    return out


def _parse_normalized(row: dict) -> dict | None:
    try:
        return {
            "date": _norm_date(row.get("date", "")),
            "away": row.get("away", "").lower(), "home": row.get("home", "").lower(),
            "away_score": float(row["away_score"]), "home_score": float(row["home_score"]),
            "line": float(row["total_line"]),
            "over_imp": to_implied_prob(row.get("over_odds") or row.get("over_price")),
            "under_imp": to_implied_prob(row.get("under_odds") or row.get("under_price")),
            "close_over_imp": to_implied_prob(row.get("close_over_odds") or row.get("close_over_price")),
            "close_under_imp": to_implied_prob(row.get("close_under_odds") or row.get("close_under_price")),
        }
    except (KeyError, ValueError):
        return None


def _parse_long(row: dict) -> dict | None:
    team, opp = row.get("team", "").lower(), row.get("opponent", "").lower()
    if not team or not opp or team >= opp:        # keep one orientation -> dedup the mirror row
        return None
    try:
        return {
            "date": _norm_date(row.get("date", "")), "away": team, "home": opp,
            "away_score": float(row["runs"]), "home_score": float(row["oppruns"]),
            "line": float(row["total"]),
            "over_imp": to_implied_prob(row.get("overodds")),
            "under_imp": to_implied_prob(row.get("underodds")),
            "close_over_imp": None, "close_under_imp": None,
        }
    except (KeyError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------


def _season(date: str) -> str:
    return date[:4]


def run_backtest(games: list[dict], *, league_baseline=DEFAULT_LEAGUE_BASELINE,
                 dispersion=DEFAULT_DISPERSION, min_edge=0.05, odds_min=1.50,
                 odds_max=3.00, fee_rate=0.0, warmup=WARMUP_GAMES,
                 park_factor=100.0) -> dict:
    """Walk forward season by season; return per-season + overall metrics."""
    seasons: dict[str, dict] = {}
    factors: dict[str, TeamFactors] = {}
    rows_by_season: dict[str, list] = defaultdict(list)

    for g in games:
        s = _season(g["date"])
        tf = factors.setdefault(s, TeamFactors(warmup))
        fair = devig_two_way(g["over_imp"], g["under_imp"])
        if fair is None:
            tf.update(g["away"], g["home"], g["away_score"], g["home_score"])
            continue
        over_price, under_price = fair
        inputs = tf.factors_for(g["away"], g["home"])

        m = st.model_probabilities(g["line"], over_price, park_factor, inputs,
                                   league_baseline=league_baseline, dispersion=dispersion)
        ou = {"over_token": "o", "under_token": "u",
              "over_price": over_price, "under_price": under_price,
              "book_sum": 1.0, "price_sane": True}
        chosen, notes = st.pick_side(g["line"], ou, m["p_over"], m["p_under"],
                                     fee_rate, odds_min, odds_max)

        actual = g["away_score"] + g["home_score"]
        over_won = None if abs(actual - g["line"]) < 1e-9 else (actual > g["line"])
        rows_by_season[s].append(_score_game(g, m, chosen, over_won, over_price,
                                             under_price, min_edge, actual))
        tf.update(g["away"], g["home"], g["away_score"], g["home_score"])  # AFTER modeling

    for s, rows in rows_by_season.items():
        seasons[s] = _aggregate(rows)
    overall = _aggregate([r for rows in rows_by_season.values() for r in rows])
    return {"warmup": warmup, "min_edge": min_edge, "odds_band": [odds_min, odds_max],
            "league_baseline": league_baseline, "dispersion": dispersion,
            "seasons": dict(sorted(seasons.items())), "overall": overall}


def _score_game(g, m, chosen, over_won, over_price, under_price, min_edge,
                actual=None) -> dict:
    """One modeled game -> a flat record for aggregation.

    Layer 4 distributional scores (CRPS) and Layer 3 interval coverage are computed for
    EVERY modeled game from its full pmf — they judge the forecast distribution against
    the realized total, independent of whether a bet was placed or the line pushed.
    """
    rec = {"season": _season(g["date"]), "modeled": True,
           "ref_prob": m["p_over"], "ref_outcome": (None if over_won is None else int(over_won)),
           "mu": m["mu"], "market_mu": m["market_mu"], "used_external": m["used_external"],
           "bet": False, "side": None, "pnl": 0.0, "stake": 0.0, "edge": None,
           "clv": None, "won": None,
           "crps": None, "cover50": None, "cover80": None}
    if actual is not None:
        pmf = rd.negbin_total_runs_pmf(m["mu"], m["var"])
        rec["crps"] = scoring.crps_pmf(pmf, actual)
        lo50, hi50 = fc.prediction_interval(pmf, 0.50)
        lo80, hi80 = fc.prediction_interval(pmf, 0.80)
        rec["cover50"] = 1 if lo50 <= actual <= hi50 else 0
        rec["cover80"] = 1 if lo80 <= actual <= hi80 else 0
    if not chosen or over_won is None:
        return rec                              # no bet, or a push (no settle)
    edge = chosen["edge"]
    if edge < min_edge or chosen.get("implausible"):
        return rec
    side = chosen["side"]
    price = over_price if side == "OVER" else under_price
    won = over_won if side == "OVER" else (not over_won)
    rec.update({"bet": True, "side": side, "edge": edge, "stake": 1.0, "won": won,
                "pnl": (1.0 / price - 1.0) if won else -1.0})
    # CLV: did the closing price move toward our side? (prob terms; needs close odds)
    fair_close = devig_two_way(g["close_over_imp"], g["close_under_imp"])
    if fair_close:
        close_side = fair_close[0] if side == "OVER" else fair_close[1]
        rec["clv"] = close_side - price
    return rec


def _aggregate(rows: list[dict]) -> dict:
    modeled = rows
    bets = [r for r in rows if r["bet"]]
    settled_bets = [r for r in bets if r["won"] is not None]
    pnl = sum(r["pnl"] for r in bets)
    stake = sum(r["stake"] for r in bets)
    wins = sum(1 for r in settled_bets if r["won"])
    clvs = [r["clv"] for r in bets if r["clv"] is not None]
    pairs = [(r["ref_prob"], r["ref_outcome"]) for r in modeled if r["ref_outcome"] is not None]
    gaps = [r["mu"] - r["market_mu"] for r in modeled if r["used_external"]]
    over_bets = [r for r in settled_bets if r["side"] == "OVER"]
    under_bets = [r for r in settled_bets if r["side"] == "UNDER"]
    crpss = [r["crps"] for r in modeled if r["crps"] is not None]
    cover50 = [r["cover50"] for r in modeled if r["cover50"] is not None]
    cover80 = [r["cover80"] for r in modeled if r["cover80"] is not None]

    def wr(rs): return (sum(1 for r in rs if r["won"]) / len(rs)) if rs else None
    return {
        "modeled": len(modeled), "settled": len(pairs), "bets": len(bets),
        "win_rate": (wins / len(settled_bets)) if settled_bets else None,
        "roi": (pnl / stake) if stake else None, "pnl_units": round(pnl, 3),
        "brier": _brier(pairs), "log_loss": _log_loss(pairs),
        "reliability": _reliability(pairs),
        # Layer 2 — calibration (all modeled over/under forecasts).
        "ece": cc.ece(pairs), "mce": cc.mce(pairs),
        "brier_decomposition": cc.brier_decomposition(pairs),
        # Layer 4 — distributional score (CRPS, run units) over all modeled games.
        "crps": (sum(crpss) / len(crpss)) if crpss else None,
        # Layer 3 — empirical interval coverage (should ≈ 0.50 / 0.80).
        "coverage50": (sum(cover50) / len(cover50)) if cover50 else None,
        "coverage80": (sum(cover80) / len(cover80)) if cover80 else None,
        "avg_edge": (sum(r["edge"] for r in bets) / len(bets)) if bets else None,
        "avg_clv": (sum(clvs) / len(clvs)) if clvs else None,
        "beat_close_pct": (sum(1 for c in clvs if c > 0) / len(clvs)) if clvs else None,
        "mean_mu_gap": (sum(gaps) / len(gaps)) if gaps else None,
        "over_bets": len(over_bets), "over_win_rate": wr(over_bets),
        "under_bets": len(under_bets), "under_win_rate": wr(under_bets),
    }


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _brier(pairs):
    return (sum((p - o) ** 2 for p, o in pairs) / len(pairs)) if pairs else None


def _log_loss(pairs):
    if not pairs:
        return None
    eps = 1e-6
    return -sum(o * math.log(min(1 - eps, max(eps, p))) +
               (1 - o) * math.log(min(1 - eps, max(eps, 1 - p))) for p, o in pairs) / len(pairs)


def _reliability(pairs, nbins=10):
    bins = [[] for _ in range(nbins)]
    for p, o in pairs:
        bins[min(nbins - 1, int(p * nbins))].append((p, o))
    out = []
    for i, b in enumerate(bins):
        if b:
            out.append({"bucket": f"{i/nbins:.1f}-{(i+1)/nbins:.1f}", "n": len(b),
                        "avg_pred": round(sum(p for p, _ in b) / len(b), 3),
                        "empirical": round(sum(o for _, o in b) / len(b), 3)})
    return out


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _decomp_line(d, f) -> str:
    """One-line Murphy Brier decomposition (reliability / resolution / uncertainty)."""
    if not d:
        return "- **Brier decomposition:** n/a"
    return (f"- **Brier decomposition:** reliability {f(d['reliability'])} (↓ better) − "
            f"resolution {f(d['resolution'])} (↑ better) + uncertainty {f(d['uncertainty'])} "
            f"= {f(d['recombined'])}")


def format_report(rep: dict) -> str:
    def f(x, pct=False, plus=False):
        if x is None:
            return "n/a"
        if pct:
            return f"{x*100:+.1f}%" if plus else f"{x*100:.1f}%"
        return f"{x:+.3f}" if plus else f"{x:.3f}"
    lines = ["# MLB Over/Under model backtest", "",
             f"Walk-forward (warmup {rep['warmup']} games), min_edge {rep['min_edge']*100:.0f}%, "
             f"odds band {rep['odds_band'][0]}x–{rep['odds_band'][1]}x, "
             f"baseline {rep['league_baseline']}, dispersion {rep['dispersion']}.", "",
             "| Season | Modeled | Bets | Win% | ROI | P&L(u) | Brier | LogLoss | CRPS | "
             "Cov80 | μ gap | CLV | Beat% |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for s, m in list(rep["seasons"].items()) + [("ALL", rep["overall"])]:
        lines.append(f"| {s} | {m['modeled']} | {m['bets']} | {f(m['win_rate'],pct=True)} | "
                     f"{f(m['roi'],pct=True,plus=True)} | {m['pnl_units']:+.1f} | "
                     f"{f(m['brier'])} | {f(m['log_loss'])} | {f(m.get('crps'))} | "
                     f"{f(m.get('coverage80'),pct=True)} | {f(m['mean_mu_gap'],plus=True)} | "
                     f"{f(m['avg_clv'],plus=True)} | {f(m['beat_close_pct'],pct=True)} |")
    o = rep["overall"]
    lines += ["", "## Overall",
              f"- **Bets:** {o['bets']} (OVER {o['over_bets']} @ {f(o['over_win_rate'],pct=True)}, "
              f"UNDER {o['under_bets']} @ {f(o['under_win_rate'],pct=True)})",
              f"- **ROI (flat 1u):** {f(o['roi'],pct=True,plus=True)} over {o['bets']} bets; "
              f"P&L {o['pnl_units']:+.1f}u",
              f"- **Calibration:** Brier {f(o['brier'])} (coin-flip 0.250, market ~0.196), "
              f"log-loss {f(o['log_loss'])}, ECE {f(o.get('ece'))}, MCE {f(o.get('mce'))}",
              f"- **Distribution (CRPS):** {f(o.get('crps'))} runs (mean abs miss of the "
              f"forecast distribution; lower is better)",
              f"- **Interval coverage:** 50% band {f(o.get('coverage50'),pct=True)} "
              f"(target 50%), 80% band {f(o.get('coverage80'),pct=True)} (target 80%)",
              _decomp_line(o.get("brier_decomposition"), f),
              f"- **Bias:** mean(μ − market_μ) = {f(o['mean_mu_gap'],plus=True)} runs "
              f"(0 = unbiased vs market)",
              f"- **CLV:** avg {f(o['avg_clv'],plus=True)}, beat close {f(o['beat_close_pct'],pct=True)}",
              "", "Reliability (predicted P(Over) vs empirical), all modeled games:",
              "", "| bucket | n | avg_pred | empirical |", "|---|---:|---:|---:|"]
    for b in o["reliability"]:
        lines.append(f"| {b['bucket']} | {b['n']} | {b['avg_pred']:.3f} | {b['empirical']:.3f} |")
    lines += ["", "_Edge is bet only when ≥ min_edge after the market anchor; ROI/CLV vs the "
              "devigged closing line are the real validation. Paper-trading research — not "
              "financial advice._"]
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description="Walk-forward backtest of the MLB total-runs model.")
    p.add_argument("--games-csv", required=True, help="Historical games+odds CSV (see module docstring)")
    p.add_argument("--seasons", default=None, help="Filter to seasons, e.g. 2021-2025 or 2021,2023")
    p.add_argument("--min-edge", type=float, default=0.05)
    p.add_argument("--odds-min", type=float, default=1.50)
    p.add_argument("--odds-max", type=float, default=3.00)
    p.add_argument("--league-baseline", type=float, default=DEFAULT_LEAGUE_BASELINE)
    p.add_argument("--dispersion", type=float, default=DEFAULT_DISPERSION)
    p.add_argument("--warmup", type=int, default=WARMUP_GAMES)
    p.add_argument("--park-factor", type=float, default=100.0)
    p.add_argument("--json", action="store_true", help="Emit JSON instead of the markdown report")
    p.add_argument("--out", default=None, help="Write the report/JSON to this file too")
    a = p.parse_args()

    if not os.path.exists(a.games_csv):
        print(f"games CSV not found: {a.games_csv}", file=sys.stderr); sys.exit(1)
    games = load_games(a.games_csv)
    if a.seasons:
        wanted = _parse_seasons(a.seasons)
        games = [g for g in games if _season(g["date"]) in wanted]
    if not games:
        print("no games after filtering", file=sys.stderr); sys.exit(1)

    rep = run_backtest(games, league_baseline=a.league_baseline, dispersion=a.dispersion,
                       min_edge=a.min_edge, odds_min=a.odds_min, odds_max=a.odds_max,
                       warmup=a.warmup, park_factor=a.park_factor)
    text = json.dumps(rep, indent=2, default=str) if a.json else format_report(rep)
    print(text)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"\nwrote {a.out}", file=sys.stderr)


def _parse_seasons(spec: str) -> set[str]:
    out: set[str] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            out.update(str(y) for y in range(int(lo), int(hi) + 1))
        elif part:
            out.add(part)
    return out


if __name__ == "__main__":
    main()
