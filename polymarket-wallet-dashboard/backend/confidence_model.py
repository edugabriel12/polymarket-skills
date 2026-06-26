#!/usr/bin/env python3
"""Derive a wallet's confidence→value bands from its uploaded CSV, and classify a
live position size into a confidence tier + suggested unit.

The CSV carries, per bet, a confidence label (Alta/Média/Baixa) AND the amount
invested. Wallets size by conviction, so each tier occupies a value band. We learn
a robust FLOOR per tier (a low percentile of that tier's invested, ordered and
clamped monotonic) and, live, a market's TOTAL position maps to the highest tier
whose floor it meets (`posição_total ≥ piso`).

Confidence → Unidade Sugerida (used on the Sports side): Alta=1U, Média=0.5U, Baixa=0.25U.
"""

from __future__ import annotations

import statistics as _st

TIERS = ("Alta", "Média", "Baixa")          # canonical order: biggest → smallest
UNIT = {"Alta": 1.0, "Média": 0.5, "Baixa": 0.25}
FLOOR_PCTL = 0.05                            # floor ≈ tier minimum (5th pctl: trims only a far outlier)


def unit_for(confidence: str) -> float | None:
    return UNIT.get(confidence)


def _pctl(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    i = min(len(sorted_vals) - 1, int(q * len(sorted_vals)))
    return sorted_vals[i]


def derive_thresholds(records: list[dict]) -> dict:
    """{tier: {floor, unit, n, min, median, max}} learned from the CSV records.

    Floors are clamped so they never decrease from Baixa→Média→Alta even if a wallet's
    data is noisy, guaranteeing a well-ordered live classifier.
    """
    bands: dict[str, dict] = {}
    for tier in TIERS:
        vals = sorted(float(r.get("invested", 0.0)) for r in records
                      if (r.get("confidence") == tier and float(r.get("invested", 0.0)) > 0))
        if not vals:
            continue
        bands[tier] = {
            "floor": round(_pctl(vals, FLOOR_PCTL), 2), "unit": UNIT[tier], "n": len(vals),
            "min": round(vals[0], 2), "median": round(_st.median(vals), 2),
            "max": round(vals[-1], 2),
        }
    # Clamp floors monotonic ascending Baixa ≤ Média ≤ Alta (defensive vs noisy wallets).
    prev = 0.0
    for tier in reversed(TIERS):              # Baixa, Média, Alta
        if tier in bands:
            bands[tier]["floor"] = round(max(bands[tier]["floor"], prev), 2)
            prev = bands[tier]["floor"]
    return bands


def classify_position(total_position: float, thresholds: dict) -> dict | None:
    """Highest tier whose floor ≤ total_position. None if below the lowest floor
    (not yet a "bet"). Returns {confidence, unit, floor}."""
    if total_position is None:
        return None
    for tier in TIERS:                        # Alta → Média → Baixa
        band = thresholds.get(tier)
        if band and total_position >= band["floor"]:
            return {"confidence": tier, "unit": band["unit"], "floor": band["floor"]}
    return None
