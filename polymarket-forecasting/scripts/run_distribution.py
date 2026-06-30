#!/usr/bin/env python3
"""Pure-stdlib statistical core for discrete totals (goals/runs) modeling.

NO third-party imports (numpy/scipy are unavailable) — only the `math` module.
A fully deterministic, offline-testable core so `test_run_distribution.py` runs
with zero setup. Sport-agnostic and reused by the soccer model.

Model: the GAME TOTAL T is Negative Binomial. Sports totals are typically
overdispersed (variance > mean), so plain Poisson under-states the tails. We fit
by method of moments and read the over/under probability off the tail of the PMF.

Polymarket price == implied probability, so decimal odds = 1/price. The default
1.50x-3.0x payout filter maps to an entry price in [1/3.0, 1/1.5] = [0.3333, 0.667].
"""

from __future__ import annotations

import math

# Odds <-> price boundaries for the default 1.50x-3.0x payout filter.
ODDS_MIN_DEFAULT = 1.50
ODDS_MAX_DEFAULT = 3.00

# Max believable post-fee edge on a (near-efficient) MLB total. The deep research
# puts realistic edge at 2-5%; anything past this is almost certainly the model
# disagreeing with superior market information, not a real opportunity -> reject.
MAX_PLAUSIBLE_EDGE = 0.15

# Market anchor: the external factors carry bias/noise (a backtest showed the model's
# mu sitting ~0.66 runs UNDER the market on average, and losing). So shrink the model's
# mu toward the efficient market-implied mu and hard-cap the deviation, rather than
# letting the factors fade the market wholesale.
MARKET_ANCHOR_WEIGHT = 0.6   # model's weight when blending toward market_mu (0=market, 1=model)
MAX_MU_DEVIATION = 0.75      # hard cap on |mu - market_mu| in runs

# Plausible clamps so a bad input can never blow up the mean.
_FACTOR_LO, _FACTOR_HI = 0.70, 1.30
_MU_LO, _MU_HI = 3.0, 18.0


# ---------------------------------------------------------------------------
# Negative Binomial (method of moments), generalized to real r via lgamma
# ---------------------------------------------------------------------------


def negbin_params_from_moments(mu: float, var: float) -> tuple[float, float]:
    """Return (r, p) for a Negative Binomial with given mean and variance.

    Uses the (r = number of successes, p = success prob) parameterization where
    mean = r(1-p)/p and var = r(1-p)/p^2, so:
        r = mu^2 / (var - mu)
        p = mu / var          (= r / (r + mu))
    Requires var > mu (overdispersion); raises ValueError otherwise.
    """
    if mu <= 0:
        raise ValueError(f"mu must be > 0, got {mu}")
    if var <= mu:
        raise ValueError(f"variance ({var}) must exceed mean ({mu}) for NegBin")
    r = (mu * mu) / (var - mu)
    p = mu / var
    return r, p


def negbin_total_runs_pmf(mu: float, var: float, kmax: int = 40) -> list[float]:
    """PMF over total runs T = 0..kmax for a NegBin(mu, var).

    P(T=k) = exp( lgamma(k+r) - lgamma(r) - lgamma(k+1) + r*ln(p) + k*ln(1-p) ).
    Tail mass beyond kmax is folded into index kmax so the returned list sums to
    1.0 exactly (then renormalized to absorb floating error).
    """
    r, p = negbin_params_from_moments(mu, var)
    ln_p = math.log(p)
    ln_1mp = math.log1p(-p)
    lg_r = math.lgamma(r)

    pmf = [0.0] * (kmax + 1)
    cumulative = 0.0
    for k in range(kmax + 1):
        ln_pk = math.lgamma(k + r) - lg_r - math.lgamma(k + 1) + r * ln_p + k * ln_1mp
        pk = math.exp(ln_pk)
        pmf[k] = pk
        cumulative += pk
    # Fold the remaining tail (1 - cumulative) into the last bucket.
    pmf[kmax] += max(0.0, 1.0 - cumulative)
    # Renormalize to kill floating-point drift.
    total = math.fsum(pmf)
    if total > 0:
        pmf = [x / total for x in pmf]
    return pmf


def prob_over(line: float, pmf: list[float]) -> dict:
    """Over/Under/Push probabilities for `line` given a total-runs PMF.

    Half-run line (e.g. 8.5): no push.
        need = ceil(line); P(Over) = sum(pmf[k] for k >= need).
    Integer line (e.g. 9.0): push at T == line; effective probabilities
        renormalize over the non-push mass.
    Returns p_over, p_under, p_push, p_over_eff, p_under_eff, need.
    """
    kmax = len(pmf) - 1
    is_integer = abs(line - round(line)) < 1e-9

    if is_integer:
        n = int(round(line))
        p_push = pmf[n] if 0 <= n <= kmax else 0.0
        p_over = math.fsum(pmf[k] for k in range(n + 1, kmax + 1))
        p_under = math.fsum(pmf[k] for k in range(0, n))
        denom = 1.0 - p_push
        p_over_eff = p_over / denom if denom > 0 else 0.0
        p_under_eff = p_under / denom if denom > 0 else 0.0
        need = n + 1
    else:
        need = math.ceil(line)
        p_over = math.fsum(pmf[k] for k in range(need, kmax + 1))
        p_under = math.fsum(pmf[k] for k in range(0, need))
        p_push = 0.0
        p_over_eff = p_over
        p_under_eff = p_under

    return {
        "p_over": p_over,
        "p_under": p_under,
        "p_push": p_push,
        "p_over_eff": p_over_eff,
        "p_under_eff": p_under_eff,
        "need": need,
    }


# ---------------------------------------------------------------------------
# Mean (mu) derivation: park-adjusted baseline + optional capped adjustments
# ---------------------------------------------------------------------------


def baseline_mu(park_factor: float = 100.0, league_baseline: float = 8.5) -> float:
    """Park-adjusted league-average game total.

    mu = league_baseline * (park_factor / 100). league_baseline 8.5 is a neutral
    round default (AL ~4.50 + NL ~4.26 ~= 8.76). Coors (~128) -> ~10.9.
    """
    return league_baseline * (park_factor / 100.0)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _factor(x):
    """Normalize an optional multiplicative factor (None -> neutral 1.0), clamped."""
    return 1.0 if x is None else _clamp(float(x), _FACTOR_LO, _FACTOR_HI)


def adjust_mu(base_mu: float, *,
              home_off=None, away_off=None,
              home_sp=None, away_sp=None,
              home_field=None, temp_f=None, wind_out_mph=None) -> float:
    """Adjust the total-runs mean with optional, capped real inputs.

    Per the approved feature set (references/run-model.md):
      INCLUDED  home/away (park is already in base_mu; `home_field` adds a small
                run delta), and season retrospect (`*_off` = team season run-rate
                factors, `*_sp` = starter+bullpen season run-allowed factors).
      EXCLUDED  short-term recent form and head-to-head team records (noise).

    Each team's runs = (base/2) * own_offense * opponent_pitching. Weather adds
    small additive deltas. Any None input is treated as neutral, so with all None
    this returns base_mu unchanged.
    """
    per_team = base_mu / 2.0
    home_runs = per_team * _factor(home_off) * _factor(away_sp)
    away_runs = per_team * _factor(away_off) * _factor(home_sp)
    mu = home_runs + away_runs

    # Home-field run-environment delta (small; clamped to +/-0.5 run).
    if home_field is not None:
        mu += _clamp(float(home_field), -0.5, 0.5)

    # Temperature: ~+1% total runs per +10F above a 70F reference (research §2).
    if temp_f is not None:
        mu *= 1.0 + _clamp((float(temp_f) - 70.0) / 10.0 * 0.01, -0.06, 0.06)

    # Wind blowing out: ~+0.07 run/mph beyond 5 mph, capped at +2 runs.
    if wind_out_mph is not None:
        excess = max(0.0, float(wind_out_mph) - 5.0)
        mu += min(2.0, excess * 0.07)

    return _clamp(mu, _MU_LO, _MU_HI)


def variance_from_mu(mu: float, dispersion: float = 2.0) -> float:
    """Total-runs variance = dispersion * mean (research: variance ~ 2x mean)."""
    return dispersion * mu


def market_implied_mu(line: float, market_p_over: float,
                      dispersion: float = 2.0,
                      lo: float = _MU_LO, hi: float = _MU_HI,
                      iters: int = 60) -> float:
    """Solve mu so the model's P(Over) matches the market (anti-fabrication).

    P(Over) is monotincreasing in mu (tail shifts right), so bisection converges.
    Used when no external inputs exist: the resulting model edge is ~0 by design,
    so the skill never invents an edge from nothing.
    """
    target = _clamp(float(market_p_over), 1e-6, 1.0 - 1e-6)

    def p_over_eff(mu: float) -> float:
        pmf = negbin_total_runs_pmf(mu, variance_from_mu(mu, dispersion))
        return prob_over(line, pmf)["p_over_eff"]

    a, b = lo, hi
    fa = p_over_eff(a) - target
    for _ in range(iters):
        mid = (a + b) / 2.0
        fm = p_over_eff(mid) - target
        if abs(fm) < 1e-6:
            return mid
        if (fa < 0) == (fm < 0):
            a, fa = mid, fm
        else:
            b = mid
    return (a + b) / 2.0


def anchor_to_market(mu_model: float, market_mu: float,
                     weight: float = MARKET_ANCHOR_WEIGHT,
                     cap: float = MAX_MU_DEVIATION) -> float:
    """Shrink the factor-based mu toward the market-implied mu, then cap the deviation.

    blended = market_mu + weight*(mu_model - market_mu); the result is then clamped so
    |result - market_mu| <= cap. Removes the systematic bias/noise the raw factors add
    while still letting a strong, plausible signal nudge mu off the market.
    """
    blended = market_mu + weight * (mu_model - market_mu)
    dev = max(-cap, min(cap, blended - market_mu))
    return market_mu + dev


# ---------------------------------------------------------------------------
# Odds <-> price helpers (the default 1.50x-3.0x payout filter)
# ---------------------------------------------------------------------------


def decimal_odds(price: float) -> float:
    """Decimal payout multiple for a Polymarket price (= implied prob)."""
    if price <= 0:
        return float("inf")
    return 1.0 / price


def passes_odds_filter(price: float,
                       odds_min: float = ODDS_MIN_DEFAULT,
                       odds_max: float = ODDS_MAX_DEFAULT) -> bool:
    """True iff the entry price yields a decimal payout in [odds_min, odds_max].

    e.g. default band: payout 1.50x <-> price 0.667 (ceiling); 3.0x <-> 0.3333 (floor).
    """
    price_floor = 1.0 / odds_max
    price_ceil = 1.0 / odds_min
    return price_floor <= price <= price_ceil
