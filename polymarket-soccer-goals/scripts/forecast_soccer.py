#!/usr/bin/env python3
"""Layers 1 & 3 for soccer — goal distribution + per-prediction confidence (pure stdlib).

Layer 1 (DISTRIBUTION): the Dixon-Coles score matrix already IS a full joint distribution
over (home, away) goals. This module reads the marginal TOTAL-GOALS pmf off it (anti-diagonal
sums) and the BTTS probability, so over/under and BTTS come from one consistent forecast.

Layer 3 (PER-PREDICTION CONFIDENCE): from the total-goals pmf we derive 50%/80% prediction
intervals + predictive entropy — the honest "how uncertain is this match" statement. A single
match total is irreducibly uncertain (~99% aleatoric per the research), so the 80% interval is
wide (~1-5 goals); that width is the signal, not a defect.

Reuses the MLB skill's generic pmf helpers (`forecast.py`: quantile/interval/entropy — they
work on ANY pmf) and the Dixon-Coles core (`dixon_coles.py`). NO third-party imports.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  (puts the reused MLB scripts dir on sys.path)

import dixon_coles as dc
import forecast as fc  # generic pmf helpers from the MLB skill (cdf/quantile/interval/entropy)


def total_goals_pmf(matrix: list[list[float]]) -> list[float]:
    """Marginal pmf over the match TOTAL goals from a score matrix (anti-diagonal sums).

    P(Total=k) = Σ_{i+j=k} matrix[i][j]. Length = 2*kmax+1 (0..24 for kmax=12).
    """
    n = len(matrix)
    pmf = [0.0] * (2 * (n - 1) + 1)
    for i in range(n):
        row = matrix[i]
        for j in range(n):
            pmf[i + j] += row[j]
    return pmf


def forecast_block(lam_home: float, lam_away: float, rho: float = dc.DEFAULT_RHO,
                   line: float | None = None, market_type: str = "TOTAL") -> dict:
    """Layer 1 + 3 confidence summary for a match from its Dixon-Coles lambdas.

    Returns the expected/most-likely total, the 50%/80% prediction intervals, predictive
    entropy, the BTTS probability, and (for a TOTAL market with a line) over/under. The heavy
    score matrix and pmf are dropped — only the human-readable summary is stored. So a bare
    "P(Over)=0.58" becomes a forecast with a stated, honest confidence range.
    """
    matrix = dc.score_matrix(lam_home, lam_away, rho)
    pmf = total_goals_pmf(matrix)
    pi50 = fc.prediction_interval(pmf, 0.50)
    pi80 = fc.prediction_interval(pmf, 0.80)
    out = {
        "mean_goals": round(fc.mean_of_pmf(pmf), 2),
        "median_goals": fc.quantile(pmf, 0.50),
        "most_likely_goals": fc.mode_of_pmf(pmf),
        "pi50": list(pi50),
        "pi80": list(pi80),
        "pi80_mass": round(fc.interval_mass(pmf, *pi80), 4),
        "entropy_bits": round(fc.predictive_entropy(pmf), 3),
        "p_btts": round(dc.prob_btts(matrix)["p_yes"], 4),
    }
    if line is not None and market_type == "TOTAL":
        pr = dc.prob_over(line, matrix)
        out["line"] = line
        out["p_over"] = round(pr["p_over_eff"], 4)
        out["p_under"] = round(pr["p_under_eff"], 4)
    return out
