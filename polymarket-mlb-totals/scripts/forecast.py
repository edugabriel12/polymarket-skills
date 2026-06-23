#!/usr/bin/env python3
"""Layers 1 & 3 — distributional forecast + per-prediction confidence (pure stdlib).

Layer 1 (DISTRIBUTION): the model already forecasts a full Negative-Binomial pmf over
total runs (`run_distribution.negbin_total_runs_pmf`) rather than a single point. This
module reads usable quantities off that pmf: mean, median, mode, and tail probabilities.

Layer 3 (PER-PREDICTION CONFIDENCE): a single number ("70%") is not enough — each
forecast also needs an honest statement of HOW uncertain it is. From the pmf we derive:
  - prediction intervals (50% / 80% central) — the range the total is likely to land in;
  - predictive entropy — the spread/uncertainty of the distribution in bits.

A single MLB game total is irreducibly uncertain (it can land anywhere from 0 to 20+),
so these intervals are wide by nature — that width is the honest signal, not a defect.
Coverage of the intervals is validated walk-forward in Layer 4 (`scoring.coverage`).

NO third-party imports — only `math`. Deterministic and offline-testable.
"""

from __future__ import annotations

import math

import run_distribution as rd


# ---------------------------------------------------------------------------
# Reading quantities off a pmf
# ---------------------------------------------------------------------------


def cdf(pmf: list[float]) -> list[float]:
    """Cumulative distribution CDF[k] = P(T ≤ k)."""
    out = []
    c = 0.0
    for p in pmf:
        c += p
        out.append(c)
    return out


def quantile(pmf: list[float], q: float) -> int:
    """Smallest k with CDF(k) ≥ q (the q-quantile of the total-runs distribution)."""
    q = max(0.0, min(1.0, q))
    c = 0.0
    for k, p in enumerate(pmf):
        c += p
        if c >= q - 1e-12:
            return k
    return len(pmf) - 1


def mean_of_pmf(pmf: list[float]) -> float:
    """Expected total runs E[T] = Σ k·pmf[k]."""
    return math.fsum(k * p for k, p in enumerate(pmf))


def mode_of_pmf(pmf: list[float]) -> int:
    """Most likely single total (argmax of the pmf)."""
    best_k, best_p = 0, -1.0
    for k, p in enumerate(pmf):
        if p > best_p:
            best_k, best_p = k, p
    return best_k


def prediction_interval(pmf: list[float], mass: float = 0.80) -> tuple[int, int]:
    """Central interval [lo, hi] (integer run totals) covering ≥ `mass` probability.

    Built from the (mass/2) and (1−mass/2) quantiles. Because runs are discrete the
    realized coverage is conservative (≥ mass), never below — the honest direction.
    """
    alpha = (1.0 - mass) / 2.0
    lo = quantile(pmf, alpha)
    hi = quantile(pmf, 1.0 - alpha)
    return lo, hi


def interval_mass(pmf: list[float], lo: int, hi: int) -> float:
    """Total probability the pmf assigns to the closed integer range [lo, hi]."""
    lo = max(0, lo)
    hi = min(len(pmf) - 1, hi)
    return math.fsum(pmf[lo:hi + 1])


def predictive_entropy(pmf: list[float], base: float = 2.0) -> float:
    """Shannon entropy H(T) = −Σ pmf·log(pmf) of the total-runs distribution.

    Higher = more spread-out / less certain. In bits by default. This is ALEATORIC
    uncertainty — the irreducible game-to-game randomness of baseball scoring, which
    dominates in sports; no better model removes it.
    """
    h = 0.0
    for p in pmf:
        if p > 0:
            h -= p * math.log(p, base)
    return h


# ---------------------------------------------------------------------------
# One-shot forecast summary (Layer 1 distribution + Layer 3 confidence)
# ---------------------------------------------------------------------------


def forecast_summary(mu: float, var: float, line: float | None = None,
                     kmax: int = 40) -> dict:
    """Full distributional forecast for a game from its (mu, var).

    Returns the point summaries (mean/median/mode), the over/under/push probabilities
    at `line` (if given), the 50%/80% prediction intervals, and the predictive entropy.
    The raw pmf is included so callers can score it (Layer 4 CRPS) or render it.
    """
    pmf = rd.negbin_total_runs_pmf(mu, var, kmax=kmax)
    pi50 = prediction_interval(pmf, 0.50)
    pi80 = prediction_interval(pmf, 0.80)
    out = {
        "mu": mu,
        "var": var,
        "mean": mean_of_pmf(pmf),
        "median": quantile(pmf, 0.50),
        "mode": mode_of_pmf(pmf),
        "pi50": pi50,
        "pi80": pi80,
        "pi50_mass": interval_mass(pmf, *pi50),
        "pi80_mass": interval_mass(pmf, *pi80),
        "entropy_bits": predictive_entropy(pmf, base=2.0),
        "pmf": pmf,
    }
    if line is not None:
        probs = rd.prob_over(line, pmf)
        out.update({
            "line": line,
            "p_over": probs["p_over_eff"],
            "p_under": probs["p_under_eff"],
            "p_push": probs["p_push"],
        })
    return out
