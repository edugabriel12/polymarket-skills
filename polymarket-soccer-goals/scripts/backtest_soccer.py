#!/usr/bin/env python3
"""Layer 4 — walk-forward validation of the soccer Dixon-Coles goals/BTTS forecast.

Validates the way the model forecasts: for each match it builds POINT-IN-TIME team
attack/defence factors from only the matches already played (no look-ahead), forms the
Dixon-Coles score matrix, and scores the full forecast against the realized result with
PROPER scoring rules:

  - CRPS            on the total-goals distribution (run/goal units; → MAE for a point).
  - Brier + log-loss on Over/Under 2.5 (binary; RPS reduces to Brier here) and on BTTS.
  - interval coverage (50%/80%) of the total-goals prediction intervals (Layer 3 check).
  - ECE             on both the O/U and BTTS forecasts (Layer 2 calibration).

Reuses the MLB skill's pure-stdlib cores (`scoring`, `calibration_core`, `forecast`) and
this skill's `dixon_coles` / `forecast_soccer`. Pure stdlib, offline-testable.

Data: a CSV of historical matches (one row per match):
    date,home,away,home_goals,away_goals[,total_line,over_odds,under_odds,btts_yes_odds,btts_no_odds]
  - date YYYY-MM-DD; team names matched case-insensitively; goals are integers.
  - total_line defaults to 2.5 if absent. Odds (American/decimal/implied) are optional and,
    when present, devigged to a market benchmark Brier for O/U.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import defaultdict

import _bootstrap  # noqa: F401
import dixon_coles as dc
import forecast_soccer as fcs
import forecast as fc
import scoring
import calibration_core as cc

WARMUP_MATCHES = 6           # soccer seasons are short; trust a team's factor after this many
DEFAULT_RHO = dc.DEFAULT_RHO
DEFAULT_LINE = 2.5


# ---------------------------------------------------------------------------
# Odds parsing / devig (shared convention with the MLB backtest)
# ---------------------------------------------------------------------------


def to_implied_prob(value):
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


def devig_two_way(a, b):
    if not a or not b or a <= 0 or b <= 0:
        return None
    s = a + b
    return a / s, b / s


# ---------------------------------------------------------------------------
# Point-in-time team attack/defence factors (walk-forward, no look-ahead)
# ---------------------------------------------------------------------------


class TeamFactors:
    """Running goals-for/against per team → point-in-time attack & defence factors.

    `factors_for` uses ONLY matches already fed via `update`, so a match never sees its own
    or any future result. Attack/defence are ratios to the league average goals-per-team.
    """

    def __init__(self, warmup: int = WARMUP_MATCHES):
        self.warmup = warmup
        self.gf: dict[str, float] = defaultdict(float)
        self.ga: dict[str, float] = defaultdict(float)
        self.g: dict[str, int] = defaultdict(int)
        self.home_goals = 0.0
        self.away_goals = 0.0
        self.matches = 0

    def _league(self):
        if self.matches == 0:
            return None
        lg_home = self.home_goals / self.matches
        lg_away = self.away_goals / self.matches
        lg_overall = (lg_home + lg_away) / 2.0
        return lg_home, lg_away, lg_overall

    def lambdas_for(self, home: str, away: str, rho: float):
        """(lam_home, lam_away) or None if either team is below warmup / no league yet."""
        if self.g[home] < self.warmup or self.g[away] < self.warmup:
            return None
        lg = self._league()
        if not lg or lg[2] <= 0:
            return None
        lg_home, lg_away, lg_overall = lg
        def att(t): return (self.gf[t] / self.g[t]) / lg_overall
        def dfn(t): return (self.ga[t] / self.g[t]) / lg_overall
        lam_home = dc._clamp(lg_home * att(home) * dfn(away), dc._LAM_LO, dc._LAM_HI)
        lam_away = dc._clamp(lg_away * att(away) * dfn(home), dc._LAM_LO, dc._LAM_HI)
        return lam_home, lam_away

    def update(self, home, away, hg, ag):
        self.gf[home] += hg; self.ga[home] += ag; self.g[home] += 1
        self.gf[away] += ag; self.ga[away] += hg; self.g[away] += 1
        self.home_goals += hg; self.away_goals += ag; self.matches += 1


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _norm_date(s: str) -> str:
    s = (s or "").strip().replace("/", "-")
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


def _num(row, *names):
    for n in names:
        if n in row and row[n] not in ("", None):
            try:
                return float(row[n])
            except (TypeError, ValueError):
                pass
    return None


def load_games(csv_path: str) -> list[dict]:
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        out = []
        for r in reader:
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
            home, away = row.get("home", "").lower(), row.get("away", "").lower()
            hg = _num(row, "home_goals", "hg", "fthg", "home_score")
            ag = _num(row, "away_goals", "ag", "ftag", "away_score")
            date = _norm_date(row.get("date", ""))
            if not (home and away and date) or hg is None or ag is None:
                continue
            out.append({
                "date": date, "home": home, "away": away,
                "hg": int(hg), "ag": int(ag),
                "line": _num(row, "total_line", "total", "line") or DEFAULT_LINE,
                "over_imp": to_implied_prob(row.get("over_odds") or row.get("over_price")),
                "under_imp": to_implied_prob(row.get("under_odds") or row.get("under_price")),
            })
    out.sort(key=lambda g: g["date"])
    return out


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------


def run_backtest(games, *, rho=DEFAULT_RHO, warmup=WARMUP_MATCHES) -> dict:
    """Walk forward; score CRPS / O-U / BTTS / coverage over all modeled matches."""
    tf = TeamFactors(warmup)
    crpss, cover50, cover80 = [], [], []
    ou_pairs, btts_pairs, mkt_ou_pairs = [], [], []
    modeled = 0

    for g in games:
        lam = tf.lambdas_for(g["home"], g["away"], rho)
        if lam is not None:
            lam_h, lam_a = lam
            matrix = dc.score_matrix(lam_h, lam_a, rho)
            pmf = fcs.total_goals_pmf(matrix)
            actual = g["hg"] + g["ag"]
            modeled += 1

            crpss.append(scoring.crps_pmf(pmf, actual))
            lo50, hi50 = fc.prediction_interval(pmf, 0.50)
            lo80, hi80 = fc.prediction_interval(pmf, 0.80)
            cover50.append(1 if lo50 <= actual <= hi50 else 0)
            cover80.append(1 if lo80 <= actual <= hi80 else 0)

            line = g["line"]
            if abs(actual - line) > 1e-9:                 # skip integer-line pushes for O/U
                over_won = 1 if actual > line else 0
                p_over = dc.prob_over(line, matrix)["p_over_eff"]
                ou_pairs.append((p_over, over_won))
                fair = devig_two_way(g["over_imp"], g["under_imp"])
                if fair:
                    mkt_ou_pairs.append((fair[0], over_won))

            actual_btts = 1 if (g["hg"] >= 1 and g["ag"] >= 1) else 0
            btts_pairs.append((dc.prob_btts(matrix)["p_yes"], actual_btts))

        tf.update(g["home"], g["away"], g["hg"], g["ag"])  # AFTER modeling

    return {
        "rho": rho, "warmup": warmup, "matches_total": len(games), "modeled": modeled,
        "crps": (sum(crpss) / len(crpss)) if crpss else None,
        "coverage50": (sum(cover50) / len(cover50)) if cover50 else None,
        "coverage80": (sum(cover80) / len(cover80)) if cover80 else None,
        "over_under": _binary_block(ou_pairs),
        "btts": _binary_block(btts_pairs),
        "market_over_under": _binary_block(mkt_ou_pairs) if mkt_ou_pairs else None,
    }


def _binary_block(pairs) -> dict:
    """Brier (= binary RPS) + log-loss + ECE + base rate for a set of (prob, outcome) pairs."""
    if not pairs:
        return {"n": 0}
    base = sum(o for _, o in pairs) / len(pairs)
    return {
        "n": len(pairs),
        "brier": scoring.brier(pairs),
        "log_loss": scoring.log_loss(pairs),
        "ece": cc.ece(pairs),
        "base_rate": base,
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def format_report(rep: dict) -> str:
    def f(x, pct=False):
        if x is None:
            return "n/a"
        return f"{x*100:.1f}%" if pct else f"{x:.4f}"

    ou, bt, mk = rep["over_under"], rep["btts"], rep.get("market_over_under")
    lines = [
        "# Soccer Dixon-Coles goals/BTTS forecast — walk-forward validation", "",
        f"Matches {rep['matches_total']} ({rep['modeled']} modeled after {rep['warmup']}-match "
        f"warmup), ρ={rep['rho']}.", "",
        "## Distribution (Layer 1 + 4)",
        f"- **CRPS (total goals):** {f(rep['crps'])} goals (mean abs miss of the forecast "
        f"distribution; lower is better)",
        f"- **Interval coverage:** 50% band {f(rep['coverage50'],pct=True)} (target 50%), "
        f"80% band {f(rep['coverage80'],pct=True)} (target 80%)",
        "", "## Over/Under (binary — RPS = Brier here)",
        f"- n={ou.get('n',0)}  Brier {f(ou.get('brier'))}  log-loss {f(ou.get('log_loss'))}  "
        f"ECE {f(ou.get('ece'))}  (Over base rate {f(ou.get('base_rate'),pct=True)})",
    ]
    if mk:
        lines.append(f"- market benchmark (devigged close): Brier {f(mk.get('brier'))}  "
                     f"log-loss {f(mk.get('log_loss'))} over n={mk.get('n',0)} — beating this "
                     f"is the real test")
    lines += [
        "", "## BTTS (binary)",
        f"- n={bt.get('n',0)}  Brier {f(bt.get('brier'))}  log-loss {f(bt.get('log_loss'))}  "
        f"ECE {f(bt.get('ece'))}  (BTTS base rate {f(bt.get('base_rate'),pct=True)})",
        "",
        "_Brier coin-flip 0.250; a sharp football market ≈ 0.21. RPS reduces to Brier for these "
        "binary markets. Walk-forward, no look-ahead. Paper-trading research — not financial "
        "advice._",
    ]
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description="Walk-forward backtest of the soccer Dixon-Coles model.")
    p.add_argument("--games-csv", required=True, help="Historical matches CSV (see module docstring)")
    p.add_argument("--rho", type=float, default=DEFAULT_RHO, help="Dixon-Coles ρ (default %.2f)" % DEFAULT_RHO)
    p.add_argument("--warmup", type=int, default=WARMUP_MATCHES)
    p.add_argument("--json", action="store_true")
    p.add_argument("--out", default=None)
    a = p.parse_args()

    if not os.path.exists(a.games_csv):
        print(f"games CSV not found: {a.games_csv}", file=sys.stderr); sys.exit(1)
    games = load_games(a.games_csv)
    if not games:
        print("no games loaded", file=sys.stderr); sys.exit(1)
    rep = run_backtest(games, rho=a.rho, warmup=a.warmup)
    text = json.dumps(rep, indent=2, default=str) if a.json else format_report(rep)
    print(text)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"\nwrote {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
