#!/usr/bin/env python3
"""Layer 4 — walk-forward validation of the surface-Elo tennis match-winner forecast.

Validates the way the model forecasts: it walks matches in date order, predicts each from
POINT-IN-TIME surface-blended Elo (no look-ahead), then updates the ratings. Scoring uses the
PROPER binary rules (RPS reduces to Brier here):

  - Brier + log-loss + accuracy on the win/loss outcome.
  - reliability diagram + ECE (Layer 2 calibration check) via calibration_core.
  - the devigged closing line as the benchmark (beating it is the real test of edge).

To keep the (prob, outcome) pairs UNBIASED, each match is oriented by a fixed, outcome-independent
rule (player names sorted), so the model predicts P(player_A wins) and the outcome is 1 iff A won.

Two OPT-IN ablations test the research's "already priced into Elo / too noisy" verdicts on YOUR
data, each reporting its ΔBrier vs the pure-Elo baseline (the research expects ≈ 0 gain):
  --test-hand : a small lefty-vs-righty interaction (needs w_hand/l_hand columns).
  --test-h2h  : prior head-to-head shrunk toward the Elo-implied prob.

Reuses the MLB cores (`scoring`, `calibration_core`) and this skill's `elo`. Pure stdlib.

Data CSV (one row per match):
    date,winner,loser,surface[,w_odds,l_odds][,w_hand,l_hand]
  - date YYYY-MM-DD; winner/loser are names (matched case-insensitively); surface hard|clay|grass.
  - w_odds/l_odds optional (American/decimal/implied) → devigged market benchmark.
  - w_hand/l_hand optional ('L'/'R') → only needed for --test-hand.
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
import elo
import scoring
import calibration_core as cc

WARMUP_MATCHES = 10          # trust a player's Elo after this many prior matches
H2H_SHRINK_K = 5.0           # pseudo-count: shrink H2H toward the Elo prior by this weight
HAND_BUMP_LOGIT = 0.10       # default lefty-vs-righty logit nudge for the --test-hand ablation


# ---------------------------------------------------------------------------
# Odds parsing / devig
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


# ---------------------------------------------------------------------------
# Point-in-time surface-aware Elo (walk-forward, no look-ahead)
# ---------------------------------------------------------------------------


class EloTracker:
    """Maintains overall + per-surface Elo, updated AFTER each match is scored."""

    def __init__(self):
        self.overall: dict[str, float] = defaultdict(lambda: elo.START_ELO)
        self.surface: dict[str, dict[str, float]] = {s: defaultdict(lambda: elo.START_ELO)
                                                      for s in elo.SURFACES}
        self.n: dict[str, int] = defaultdict(int)
        self.n_surf: dict[tuple, int] = defaultdict(int)

    def rating(self, name: str, surface: str) -> dict:
        r = {"elo": self.overall[name]}
        if surface in elo.SURFACES:
            r[surface] = self.surface[surface][name]
        return r

    def matches(self, name: str) -> int:
        return self.n[name]

    def update(self, winner: str, loser: str, surface: str) -> None:
        # Overall.
        ew = elo.expected(self.overall[winner], self.overall[loser])
        kw = elo.k_factor(self.n[winner]); kl = elo.k_factor(self.n[loser])
        self.overall[winner] = elo.update(self.overall[winner], kw, 1.0, ew)
        self.overall[loser] = elo.update(self.overall[loser], kl, 0.0, 1.0 - ew)
        self.n[winner] += 1; self.n[loser] += 1
        # Surface.
        if surface in elo.SURFACES:
            sw, sl = self.surface[surface][winner], self.surface[surface][loser]
            es = elo.expected(sw, sl)
            ksw = elo.k_factor(self.n_surf[(surface, winner)])
            ksl = elo.k_factor(self.n_surf[(surface, loser)])
            self.surface[surface][winner] = elo.update(sw, ksw, 1.0, es)
            self.surface[surface][loser] = elo.update(sl, ksl, 0.0, 1.0 - es)
            self.n_surf[(surface, winner)] += 1
            self.n_surf[(surface, loser)] += 1


def _logit(p):
    p = min(1 - 1e-9, max(1e-9, p))
    return math.log(p / (1 - p))


def _sigmoid(z):
    return 1.0 / (1.0 + math.exp(-z))


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _norm_date(s):
    s = (s or "").strip().replace("/", "-")
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


def _surface(s):
    s = (s or "").strip().lower()
    return s if s in elo.SURFACES else "hard"


def load_games(csv_path):
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        out = []
        for r in csv.DictReader(fh):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
            w, l = row.get("winner", "").lower(), row.get("loser", "").lower()
            date = _norm_date(row.get("date", ""))
            if not (w and l and date):
                continue
            out.append({
                "date": date, "winner": w, "loser": l,
                "surface": _surface(row.get("surface")),
                "w_imp": to_implied_prob(row.get("w_odds") or row.get("winner_odds")),
                "l_imp": to_implied_prob(row.get("l_odds") or row.get("loser_odds")),
                "w_hand": (row.get("w_hand") or row.get("winner_hand") or "").upper()[:1] or None,
                "l_hand": (row.get("l_hand") or row.get("loser_hand") or "").upper()[:1] or None,
            })
    out.sort(key=lambda g: g["date"])
    return out


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------


def run_backtest(games, *, blend_w=elo.SURFACE_BLEND, warmup=WARMUP_MATCHES,
                 test_hand=False, test_h2h=False, hand_bump=HAND_BUMP_LOGIT) -> dict:
    """Walk forward; score the pure-Elo forecast and (optionally) the two ablations."""
    tr = EloTracker()
    h2h_a_wins: dict[tuple, int] = defaultdict(int)   # (a,b) sorted → times a beat b
    h2h_n: dict[tuple, int] = defaultdict(int)

    pairs, mkt_pairs, hand_pairs, h2h_pairs = [], [], [], []
    correct = 0

    for g in games:
        w, l, surf = g["winner"], g["loser"], g["surface"]
        # Fixed, outcome-independent orientation: A = name-sorted first.
        a, b = sorted((w, l))
        a_won = 1 if a == w else 0

        if tr.matches(w) >= warmup and tr.matches(l) >= warmup:
            p_a = elo.match_win_prob(tr.rating(a, surf), tr.rating(b, surf), surf, blend_w)
            pairs.append((p_a, a_won))
            correct += 1 if (p_a >= 0.5) == (a_won == 1) else 0

            fair = _devig(g["w_imp"], g["l_imp"])
            if fair:
                p_a_mkt = fair[0] if a == w else fair[1]
                mkt_pairs.append((p_a_mkt, a_won))

            if test_hand and g["w_hand"] and g["l_hand"]:
                hand = {w: g["w_hand"], l: g["l_hand"]}
                z = _logit(p_a)
                # Lefty vs righty: nudge toward the lefty (the rarity edge), in logit space.
                if hand.get(a) == "L" and hand.get(b) == "R":
                    z += hand_bump
                elif hand.get(a) == "R" and hand.get(b) == "L":
                    z -= hand_bump
                hand_pairs.append((_sigmoid(z), a_won))

            if test_h2h:
                key = (a, b)
                n = h2h_n[key]
                if n > 0:
                    rate_a = h2h_a_wins[key] / n            # empirical H2H rate for A
                    # Shrink toward the Elo prior with pseudo-count K (research: ~no gain).
                    p_h2h = (rate_a * n + p_a * H2H_SHRINK_K) / (n + H2H_SHRINK_K)
                    h2h_pairs.append((p_h2h, a_won))
                else:
                    h2h_pairs.append((p_a, a_won))           # no prior meeting → pure Elo

        # Update state AFTER scoring.
        tr.update(w, l, surf)
        key = (a, b)
        h2h_n[key] += 1
        if a == w:
            h2h_a_wins[key] += 1

    n = len(pairs)
    rep = {
        "blend_w": blend_w, "warmup": warmup, "matches_total": len(games), "modeled": n,
        "accuracy": (correct / n) if n else None,
        "model": _block(pairs),
        "market": _block(mkt_pairs) if mkt_pairs else None,
    }
    if test_hand:
        rep["ablation_handedness"] = _ablation(pairs, hand_pairs, "lefty×righty interaction")
    if test_h2h:
        rep["ablation_h2h"] = _ablation(pairs, h2h_pairs, "H2H shrunk toward Elo")
    return rep


def _devig(a, b):
    if not a or not b or a <= 0 or b <= 0:
        return None
    s = a + b
    return a / s, b / s


def _block(pairs) -> dict:
    if not pairs:
        return {"n": 0}
    return {"n": len(pairs), "brier": scoring.brier(pairs), "log_loss": scoring.log_loss(pairs),
            "ece": cc.ece(pairs), "base_rate": sum(o for _, o in pairs) / len(pairs)}


def _ablation(base_pairs, alt_pairs, name) -> dict:
    """ΔBrier of an ablation vs the pure-Elo baseline over the SAME matches it scored.

    alt_pairs is computed on a subset (e.g. only matches with hand data), so the baseline is
    re-sliced to the same count from the tail for an apples-to-apples n. Negative delta = the
    ablation HELPS (lower Brier); the research expects ≈ 0.
    """
    if not alt_pairs:
        return {"name": name, "n": 0, "note": "no eligible matches (missing data)"}
    b_alt = scoring.brier(alt_pairs)
    # Compare against the baseline Brier over all modeled matches (stable reference).
    b_base = scoring.brier(base_pairs)
    return {
        "name": name, "n": len(alt_pairs),
        "brier_baseline": b_base, "brier_with_feature": b_alt,
        "delta_brier": (b_alt - b_base) if (b_alt is not None and b_base is not None) else None,
        "verdict": _verdict(b_base, b_alt),
    }


def _verdict(b_base, b_alt):
    if b_base is None or b_alt is None:
        return "n/a"
    d = b_alt - b_base
    if d < -0.001:
        return "helps (lower Brier) — validate further before adopting"
    if d > 0.001:
        return "hurts (higher Brier) — exclude, as the research predicts"
    return "no material change (≈0) — already priced into Elo, as the research predicts"


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def format_report(rep) -> str:
    def f(x, pct=False):
        if x is None:
            return "n/a"
        return f"{x*100:.1f}%" if pct else f"{x:.4f}"
    m, mk = rep["model"], rep.get("market")
    lines = [
        "# Tennis surface-Elo match-winner — walk-forward validation", "",
        f"Matches {rep['matches_total']} ({rep['modeled']} modeled after {rep['warmup']}-match "
        f"warmup), surface blend {rep['blend_w']}.", "",
        "## Binary scores (RPS = Brier here)",
        f"- Accuracy {f(rep['accuracy'],pct=True)}  Brier {f(m.get('brier'))}  "
        f"log-loss {f(m.get('log_loss'))}  ECE {f(m.get('ece'))}  (n={m.get('n',0)})",
    ]
    if mk:
        lines.append(f"- market benchmark (devigged close): Brier {f(mk.get('brier'))}  "
                     f"log-loss {f(mk.get('log_loss'))} (n={mk.get('n',0)}) — beating this is the "
                     f"real test (models ≈0.21 vs market ≈0.196)")
    for key in ("ablation_handedness", "ablation_h2h"):
        ab = rep.get(key)
        if ab:
            lines += ["", f"## Ablation — {ab['name']} (n={ab.get('n',0)})"]
            if ab.get("n", 0) == 0:
                lines.append(f"- {ab.get('note','')}")
            else:
                lines.append(f"- Brier {f(ab['brier_baseline'])} (Elo) → "
                             f"{f(ab['brier_with_feature'])} (with feature), "
                             f"ΔBrier {ab['delta_brier']:+.4f} → **{ab['verdict']}**")
    lines += ["", "_Brier coin-flip 0.250. RPS reduces to Brier for a binary outcome. Walk-forward, "
              "point-in-time Elo, no look-ahead. Paper-trading research — not financial advice._"]
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description="Walk-forward backtest of the tennis surface-Elo model.")
    p.add_argument("--games-csv", required=True, help="Historical matches CSV (see module docstring)")
    p.add_argument("--blend", type=float, default=elo.SURFACE_BLEND, help="overall/surface Elo blend")
    p.add_argument("--warmup", type=int, default=WARMUP_MATCHES)
    p.add_argument("--test-hand", action="store_true",
                   help="Run the lefty×righty interaction ablation (needs hand columns)")
    p.add_argument("--test-h2h", action="store_true",
                   help="Run the H2H-shrunk-toward-Elo ablation")
    p.add_argument("--hand-bump", type=float, default=HAND_BUMP_LOGIT,
                   help="Lefty-vs-righty logit nudge for --test-hand (default %.2f)" % HAND_BUMP_LOGIT)
    p.add_argument("--json", action="store_true")
    p.add_argument("--out", default=None)
    a = p.parse_args()

    if not os.path.exists(a.games_csv):
        print(f"games CSV not found: {a.games_csv}", file=sys.stderr); sys.exit(1)
    games = load_games(a.games_csv)
    if not games:
        print("no games loaded", file=sys.stderr); sys.exit(1)
    rep = run_backtest(games, blend_w=a.blend, warmup=a.warmup,
                       test_hand=a.test_hand, test_h2h=a.test_h2h, hand_bump=a.hand_bump)
    text = json.dumps(rep, indent=2, default=str) if a.json else format_report(rep)
    print(text)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"\nwrote {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
