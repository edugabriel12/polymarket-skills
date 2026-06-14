#!/usr/bin/env python3
"""Pure-stdlib Dixon-Coles goal model for soccer Over/Under and BTTS markets.

NO third-party imports (only `math`). This is the deterministic, offline-testable
core (analog of the MLB skill's run_distribution.py).

Model (see research/soccer-goals-btts-deep-research.md):
- Home goals X ~ Poisson(lam_home), away Y ~ Poisson(lam_away).
- Joint score matrix P(i,j) = tau(i,j) * Poisson(i;lam_home) * Poisson(j;lam_away),
  where Dixon-Coles tau corrects the four low scorelines (0-0,1-0,0-1,1-1) to fit
  draws/low scores. Soccer goals are ~equidispersed (variance/mean ~ 1), so Poisson
  (not Negative Binomial) is the right base.
- P(Over X.5) = sum of cells with i+j > X;  P(BTTS) = sum with i>=1 and j>=1.

Polymarket price == implied probability, so decimal odds = 1/price.
"""

from __future__ import annotations

import math

KMAX = 12  # max goals per team in the score matrix (covers soccer)

# Default Dixon-Coles dependence parameter. Negative rho raises draw/low-score
# probability (the empirically observed correction). Tunable; calibrate per league.
DEFAULT_RHO = -0.10

ODDS_MIN_DEFAULT = 1.50
ODDS_MAX_DEFAULT = 3.00

_LAM_LO, _LAM_HI = 0.10, 6.0
_FACTOR_LO, _FACTOR_HI = 0.60, 1.70


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def poisson_pmf(k: int, lam: float) -> float:
    """P(N=k) for N ~ Poisson(lam), via log-gamma for stability."""
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(k * math.log(lam) - lam - math.lgamma(k + 1))


def tau(x: int, y: int, lam: float, mu: float, rho: float) -> float:
    """Dixon-Coles low-score correction factor (1 outside the four corners)."""
    if x == 0 and y == 0:
        return 1.0 - lam * mu * rho
    if x == 0 and y == 1:
        return 1.0 + lam * rho
    if x == 1 and y == 0:
        return 1.0 + mu * rho
    if x == 1 and y == 1:
        return 1.0 - rho
    return 1.0


def score_matrix(lam_home: float, lam_away: float, rho: float = DEFAULT_RHO,
                 kmax: int = KMAX) -> list[list[float]]:
    """Joint score-probability matrix P[i][j], i=home goals, j=away goals.

    Applies the Dixon-Coles tau correction and renormalizes to sum to 1.0
    (tau does not preserve total mass; clamp any negative cell to 0).
    """
    ph = [poisson_pmf(i, lam_home) for i in range(kmax + 1)]
    pa = [poisson_pmf(j, lam_away) for j in range(kmax + 1)]
    mat = [[0.0] * (kmax + 1) for _ in range(kmax + 1)]
    total = 0.0
    for i in range(kmax + 1):
        for j in range(kmax + 1):
            v = tau(i, j, lam_home, lam_away, rho) * ph[i] * pa[j]
            if v < 0:
                v = 0.0
            mat[i][j] = v
            total += v
    if total > 0:
        for i in range(kmax + 1):
            for j in range(kmax + 1):
                mat[i][j] /= total
    return mat


def prob_over(line: float, matrix: list[list[float]]) -> dict:
    """P(Over)/P(Under)/push for a total-goals line given a score matrix.

    Half line (2.5): no push. Integer line (2.0): push at total == line, and the
    effective probabilities renormalize over the non-push mass.
    """
    n = len(matrix)
    is_integer = abs(line - round(line)) < 1e-9
    p_over = p_under = p_push = 0.0
    for i in range(n):
        for j in range(n):
            t = i + j
            p = matrix[i][j]
            if is_integer and t == round(line):
                p_push += p
            elif t > line:
                p_over += p
            else:
                p_under += p
    denom = 1.0 - p_push
    return {
        "p_over": p_over, "p_under": p_under, "p_push": p_push,
        "p_over_eff": p_over / denom if denom > 0 else 0.0,
        "p_under_eff": p_under / denom if denom > 0 else 0.0,
    }


def prob_btts(matrix: list[list[float]]) -> dict:
    """P(both teams score) and P(not). yes = sum over i>=1 and j>=1."""
    n = len(matrix)
    yes = 0.0
    for i in range(1, n):
        for j in range(1, n):
            yes += matrix[i][j]
    return {"p_yes": yes, "p_no": 1.0 - yes}


# ---------------------------------------------------------------------------
# Lambda derivation
# ---------------------------------------------------------------------------


def lambdas_from_total_supremacy(total: float, supremacy: float) -> tuple[float, float]:
    """Split an expected total and a home supremacy into (lam_home, lam_away).

    lam_home = (total + supremacy)/2, lam_away = (total - supremacy)/2, clamped.
    """
    lam_home = _clamp((total + supremacy) / 2.0, _LAM_LO, _LAM_HI)
    lam_away = _clamp((total - supremacy) / 2.0, _LAM_LO, _LAM_HI)
    return lam_home, lam_away


def supremacy_from_elo(elo_home: float, elo_away: float,
                       home_adv_elo: float = 65.0,
                       goals_per_elo: float = 1.0 / 350.0) -> float:
    """Expected home goal supremacy from Club Elo ratings.

    (elo_home + home_adv_elo - elo_away) * goals_per_elo. The constants are
    tunable approximations (~65 Elo home edge; ~350 Elo per 1 goal) — calibrate
    per league/data. Returns a goal differential (can be negative).
    """
    return (elo_home + home_adv_elo - elo_away) * goals_per_elo


def adjust_total(base_total: float, *, att_home=None, att_away=None,
                 def_home=None, def_away=None) -> float:
    """Scale a league baseline total by both teams' attack/defense factors.

    Each factor is 1.0 = league average; None = neutral. The combined multiplier
    is the geometric-ish mean of the four (clamped) so a strong-attack vs
    weak-defense game raises the expected total.
    """
    factors = [f for f in (att_home, att_away, def_home, def_away) if f is not None]
    if not factors:
        return base_total
    mult = 1.0
    for f in factors:
        mult *= _clamp(float(f), _FACTOR_LO, _FACTOR_HI)
    mult = mult ** (1.0 / len(factors))  # average effect across supplied factors
    return _clamp(base_total * mult, 0.4, 7.0)


# ---------------------------------------------------------------------------
# Market-implied fallback (anti-fabrication): match the market on both markets
# ---------------------------------------------------------------------------


def _bisect(f, target, lo, hi, iters=60, tol=1e-6):
    a, b = lo, hi
    fa = f(a) - target
    for _ in range(iters):
        mid = (a + b) / 2.0
        fm = f(mid) - target
        if abs(fm) < tol:
            return mid
        if (fa < 0) == (fm < 0):
            a, fa = mid, fm
        else:
            b = mid
    return (a + b) / 2.0


def market_implied_lambdas(over_line: float, p_over: float,
                           p_btts_yes: float | None = None,
                           rho: float = DEFAULT_RHO) -> tuple[float, float]:
    """Solve (lam_home, lam_away) so the model matches the market.

    Step 1: with supremacy fixed at 0, bisect the total so model P(Over) == p_over.
    Step 2: if a BTTS price is given, bisect the supremacy so model P(BTTS) == that.
    The result reproduces the market on BOTH markets, so the model edge is ~0 by
    construction — the skill never fabricates an edge when it has no real inputs.
    """
    def over_of_total(total):
        lh, la = lambdas_from_total_supremacy(total, 0.0)
        return prob_over(over_line, score_matrix(lh, la, rho))["p_over_eff"]

    total = _bisect(over_of_total, _clamp(p_over, 1e-6, 1 - 1e-6), 0.6, 9.0)

    supremacy = 0.0
    if p_btts_yes is not None:
        def btts_of_sup(sup):
            lh, la = lambdas_from_total_supremacy(total, sup)
            return prob_btts(score_matrix(lh, la, rho))["p_yes"]
        # BTTS decreases as |supremacy| grows; search non-negative supremacy.
        target = _clamp(p_btts_yes, 1e-6, 1 - 1e-6)
        if btts_of_sup(0.0) > target:
            supremacy = _bisect(btts_of_sup, target, 0.0, total - 0.2)
    return lambdas_from_total_supremacy(total, supremacy)


# ---------------------------------------------------------------------------
# Odds helpers
# ---------------------------------------------------------------------------


def market_implied_from_btts(p_btts_yes: float, rho: float = DEFAULT_RHO) -> tuple[float, float]:
    """Solve symmetric (lam_home, lam_away) so model P(BTTS) == the market price.

    P(BTTS) increases monotonically with the total (supremacy fixed at 0), so
    bisection converges. Used for a standalone BTTS market in fallback mode:
    edge ~ 0 by construction (no fabricated edge).
    """
    def btts_of_total(total):
        lh, la = lambdas_from_total_supremacy(total, 0.0)
        return prob_btts(score_matrix(lh, la, rho))["p_yes"]

    total = _bisect(btts_of_total, _clamp(p_btts_yes, 1e-6, 1 - 1e-6), 0.6, 9.0)
    return lambdas_from_total_supremacy(total, 0.0)


def decimal_odds(price: float) -> float:
    return float("inf") if price <= 0 else 1.0 / price


def passes_odds_filter(price: float, odds_min: float = ODDS_MIN_DEFAULT,
                       odds_max: float = ODDS_MAX_DEFAULT) -> bool:
    return (1.0 / odds_max) <= price <= (1.0 / odds_min)
