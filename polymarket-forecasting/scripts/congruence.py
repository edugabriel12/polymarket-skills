#!/usr/bin/env python3
"""Model↔sharp CONGRUENCE — use both estimates without corrupting the edge (pure stdlib).

The sharp close is the efficient probability, so it sets the trade EDGE (what we bet on).
The statistical model (Elo / NegBin / Dixon-Coles) is a SECOND, independent opinion. Instead
of blending the model into the edge — which the research shows degrades the estimate AND lets a
corrupt sharp line slip past the implausible-edge cap — we use the AGREEMENT between the two to
modulate conviction:

  - model agrees with the sharp (small gap)  -> full size, full confidence;
  - model disagrees (large gap)              -> shrink size toward 0, pull confidence to 0.5;
  - gap beyond the implausible band          -> flagged incongruent (size ~0 → naturally skipped).

This never INCREASES size (factor ∈ [0,1]) — strictly conservative — and strengthens, not
weakens, the safety net: a bet the model itself does not corroborate is sized down. It only
applies when BOTH a sharp anchor and an independent model probability exist; otherwise it is a
no-op (factor 1.0).

NO third-party imports. Deterministic and offline-testable. Shared by all three sports
(the gap is a probability, comparable across markets).
"""

from __future__ import annotations

# Defaults (probability units). full agreement ≤3pts → full size; ≥15pts (the implausible-edge
# cap) → zero size; ≥10pts → flagged incongruent.
FULL_AGREE = 0.03
ZERO_AT = 0.15
INCONGRUENT_AT = 0.10

NEUTRAL = {"gap": None, "factor": 1.0, "incongruent": False, "agreement": "n/a", "applied": False}


def gap(p_model: float, p_sharp: float) -> float:
    """Absolute probability gap between the model's independent estimate and the sharp."""
    return abs(p_model - p_sharp)


def congruence_factor(g: float, *, full: float = FULL_AGREE, zero: float = ZERO_AT) -> float:
    """Size multiplier in [0,1] from the gap: 1 at/below `full`, 0 at/above `zero`, linear between."""
    if g <= full:
        return 1.0
    if g >= zero:
        return 0.0
    return (zero - g) / (zero - full)


def assess(p_model: float | None, p_sharp: float | None, *,
           full: float = FULL_AGREE, zero: float = ZERO_AT,
           incongruent_at: float = INCONGRUENT_AT) -> dict:
    """Bundle the congruence read-out for a (model, sharp) probability pair.

    Returns NEUTRAL (factor 1.0, applied=False) when either input is missing — so a game
    without an independent model, or without a sharp anchor, is never penalized.
    """
    if p_model is None or p_sharp is None:
        return dict(NEUTRAL)
    g = gap(p_model, p_sharp)
    f = congruence_factor(g, full=full, zero=zero)
    if g <= full:
        agreement = "high"
    elif g < incongruent_at:
        agreement = "moderate"
    else:
        agreement = "low"
    return {
        "gap": round(g, 4),
        "factor": round(f, 4),
        "incongruent": g >= incongruent_at,
        "agreement": agreement,
        "applied": True,
    }


def apply_confidence(confidence: float, factor: float) -> float:
    """Pull a confidence toward the 0.5 toss-up by the congruence factor (disagreement = less sure)."""
    return 0.5 + (confidence - 0.5) * factor
