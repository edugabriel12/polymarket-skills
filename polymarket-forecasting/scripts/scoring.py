#!/usr/bin/env python3
"""Layer 4 — proper scoring rules for a discrete totals forecast (pure stdlib).

A calibrated forecaster is judged by PROPER scoring rules: rules a forecaster
minimizes (in expectation) only by reporting its true beliefs. This module scores
the full predictive DISTRIBUTION, not just the over/under bet:

  - CRPS  — Continuous Ranked Probability Score, the distributional generalization
            of the Brier score. In RUN UNITS, finite on tail blowouts, and it
            reduces exactly to the absolute error |forecast − actual| for a point
            (deterministic) forecast. For a discrete pmf over total runs:
                CRPS(F, y) = Σ_k (CDF(k) − 𝟙{k ≥ y})²
            where CDF(k) = P(T ≤ k) and y is the realized total.
  - log-loss / Brier — strictly proper scores on the OVER/UNDER binary.
  - coverage — does an X% prediction interval actually contain the truth ~X% of
            the time? (validates Layer 3's intervals.)

NO third-party imports (numpy/scipy are unavailable) — only `math`. Everything here
is deterministic and offline-testable so `test_scoring.py` runs with zero setup.
"""

from __future__ import annotations

import math

_EPS = 1e-12


# ---------------------------------------------------------------------------
# CRPS — scores the whole predictive distribution (run units)
# ---------------------------------------------------------------------------


def crps_pmf(pmf: list[float], actual: float) -> float:
    """CRPS of a discrete total-runs pmf against the realized integer total.

        CRPS = Σ_{k=0..kmax} (CDF(k) − 𝟙{k ≥ actual})²

    Lower is better; the units are runs. For a point-mass pmf at m this reduces to
    |m − actual| (see test), so CRPS is a strict generalization of mean-absolute
    error to a full distribution. A confident-but-wrong forecast (sharp pmf far from
    `actual`) is penalized roughly linearly in the miss, NOT quadratically — that
    bounded tail behavior is why CRPS is preferred over squared error for counts.
    """
    cdf = 0.0
    total = 0.0
    for k, p in enumerate(pmf):
        cdf += p
        indicator = 1.0 if k >= actual else 0.0
        total += (cdf - indicator) ** 2
    return total


def crps_point(forecast: float, actual: float) -> float:
    """CRPS of a deterministic (point) forecast == absolute error |forecast − actual|."""
    return abs(forecast - actual)


# ---------------------------------------------------------------------------
# Binary proper scores on the OVER/UNDER outcome
# ---------------------------------------------------------------------------


def brier(pairs: list[tuple[float, int]]) -> float | None:
    """Mean Brier score (p − outcome)² over (prob, outcome∈{0,1}) pairs.

    Coin flip ≈ 0.25; a sharp MLB total market ≈ 0.196. Strictly proper. None if empty.
    """
    if not pairs:
        return None
    return sum((p - o) ** 2 for p, o in pairs) / len(pairs)


def log_loss(pairs: list[tuple[float, int]]) -> float | None:
    """Mean negative log-likelihood over (prob, outcome) pairs. Strictly proper.

    Probabilities are clipped to [eps, 1−eps] so a confident miss stays finite.
    """
    if not pairs:
        return None
    eps = 1e-6
    return -sum(
        o * math.log(min(1 - eps, max(eps, p)))
        + (1 - o) * math.log(min(1 - eps, max(eps, 1 - p)))
        for p, o in pairs
    ) / len(pairs)


# ---------------------------------------------------------------------------
# Interval coverage — validates Layer 3's prediction intervals
# ---------------------------------------------------------------------------


def coverage(records: list[tuple[float, float, float]]) -> float | None:
    """Fraction of (lo, hi, actual) triples whose actual lies in the closed [lo, hi].

    A well-calibrated 80% interval should return ~0.80 here. None if empty.
    """
    if not records:
        return None
    inside = sum(1 for lo, hi, actual in records if lo <= actual <= hi)
    return inside / len(records)


# ---------------------------------------------------------------------------
# Aggregation helper
# ---------------------------------------------------------------------------


def mean_crps(pmf_actuals: list[tuple[list[float], float]]) -> float | None:
    """Mean CRPS over a list of (pmf, actual_total) pairs. None if empty."""
    if not pmf_actuals:
        return None
    return sum(crps_pmf(pmf, y) for pmf, y in pmf_actuals) / len(pmf_actuals)
