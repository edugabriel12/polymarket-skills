#!/usr/bin/env python3
"""Layers 1 & 3 for tennis — the binary forecast + per-prediction confidence (pure stdlib).

A match-winner market is BINARY, so the forecast IS the win probability — there is no wide
goals/runs interval as in MLB/soccer. "Layer 1 (distribution)" here is the Bernoulli(p), and
"Layer 3 (confidence)" is the calibrated p plus its **predictive entropy** (max 1 bit at the
p=0.5 toss-up; → 0 as the forecast sharpens). Per the research, a single binary prediction's
trustworthiness comes from CALIBRATION over many matches, not from anything inside one
prediction — so there is no interval to report, only the probability, its entropy, and a
human confidence label.

The research also says heat/wind are *variance* modifiers, not directional favourite signals,
so an optional `uncertainty_flag` widens the stated confidence (nudges entropy up) WITHOUT
moving the probability.

Reuses the MLB skill's generic `forecast.predictive_entropy` (works on any pmf). NO third-party
imports.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  (puts the reused MLB scripts dir on sys.path)

import forecast as fc  # generic pmf helper from the MLB skill


def confidence_label(entropy_bits: float) -> str:
    """Map Bernoulli entropy (bits) to a human confidence band.

    p=0.5 → 1.00 bit (toss-up); p≈0.70 → 0.88; p≈0.85 → 0.61; p≈0.95 → 0.29.
    """
    if entropy_bits >= 0.95:
        return "toss-up"
    if entropy_bits >= 0.72:
        return "lean"
    return "strong"


def forecast_block(p_win: float, *, uncertainty_flag: bool = False) -> dict:
    """Layer 1 + 3 summary for a binary match-winner forecast.

    Returns the win/lose probabilities, the predictive entropy (bits), and a confidence
    label. `uncertainty_flag` (e.g. extreme heat / high wind) bumps the reported entropy up
    one notch toward a toss-up — widening stated confidence without shifting the favourite.
    """
    p = max(1e-9, min(1.0 - 1e-9, p_win))
    pmf = [1.0 - p, p]                      # [lose, win] Bernoulli
    ent = fc.predictive_entropy(pmf)        # bits, max 1.0 at p=0.5
    label = confidence_label(ent)
    if uncertainty_flag and label == "strong":
        label = "lean"
    elif uncertainty_flag and label == "lean":
        label = "toss-up"
    return {
        "p_win": round(p, 4),
        "p_lose": round(1.0 - p, 4),
        "entropy_bits": round(ent, 3),
        "confidence": label,
        "uncertainty_flag": bool(uncertainty_flag),
    }
