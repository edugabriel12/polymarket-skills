#!/usr/bin/env python3
"""Pure-stdlib surface-aware Elo engine for tennis match-winner prediction.

NO third-party imports (only `math`). This is the deterministic, offline-testable
core (analog of the soccer skill's dixon_coles.py and the MLB skill's
run_distribution.py).

Model (see references/deep-research.md) — the peer-validated reference engine:
- Win probability is the standard logistic Elo:
      P(A beats B) = 1 / (1 + 10^((Elo_B - Elo_A) / 400))
- FiveThirtyEight dynamic K-factor (shrinks as a player accumulates matches):
      K(n) = K_BASE / (n + K_OFFSET)^K_SHAPE      [250 / (n+5)^0.4]
- Surface-specific Elo blended ~50/50 with overall Elo is the single highest-value
  enhancement over plain Elo (Tennis Abstract). blend_w is tunable.

The market price on Polymarket IS the implied probability, so the fair (no-vig) price
for a side equals our model probability; edge = P_model - price.
"""

from __future__ import annotations

import math

SURFACES = ("hard", "clay", "grass")
START_ELO = 1500.0            # standard Elo seed for a new player

# FiveThirtyEight tennis Elo K-factor parameters: K = 250 / (n + 5)^0.4.
K_BASE, K_OFFSET, K_SHAPE = 250.0, 5.0, 0.4

# Overall/surface blend weight: 0.5 => 50/50 (Tennis Abstract's best general setting).
SURFACE_BLEND = 0.5

# Decimal-payout band for moneyline entries. Wider than the soccer goals band because
# match-winner favorites trade at short prices; tune per appetite.
ODDS_MIN_DEFAULT = 1.10       # price <= 0.909 (allows odds-on favorites)
ODDS_MAX_DEFAULT = 5.00       # price >= 0.20  (caps how big an underdog we back)


def k_factor(n_matches: int) -> float:
    """Dynamic K-factor: large early (few matches), shrinking with experience."""
    return K_BASE / (max(0, n_matches) + K_OFFSET) ** K_SHAPE


def expected(elo_a: float, elo_b: float) -> float:
    """Logistic expected score (= win probability) of A vs B."""
    return 1.0 / (1.0 + 10.0 ** ((elo_b - elo_a) / 400.0))


# win_prob is an alias kept for readability at call sites.
win_prob = expected


def update(elo: float, k: float, actual: float, exp: float) -> float:
    """One Elo update. actual = 1.0 win / 0.0 loss; exp = pre-match expected score."""
    return elo + k * (actual - exp)


def blend(overall: float, surface: float | None, w: float = SURFACE_BLEND) -> float:
    """Blend overall and surface Elo. Falls back to overall when no surface rating."""
    if surface is None:
        return overall
    w = min(1.0, max(0.0, w))
    return w * surface + (1.0 - w) * overall


def blended_elo(rating: dict, surface: str | None, w: float = SURFACE_BLEND) -> float:
    """Resolve a player's effective Elo for a surface from a rating dict.

    rating: {"elo": float, "hard": float|None, "clay": ..., "grass": ...}. Missing
    overall defaults to START_ELO; missing surface defers to overall via blend().
    """
    overall = float(rating.get("elo", START_ELO))
    surf = rating.get(surface) if surface in SURFACES else None
    return blend(overall, None if surf is None else float(surf), w)


def match_win_prob(rating_a: dict, rating_b: dict, surface: str | None,
                   blend_w: float = SURFACE_BLEND) -> float:
    """P(A beats B) from two surface-aware rating dicts."""
    return expected(blended_elo(rating_a, surface, blend_w),
                    blended_elo(rating_b, surface, blend_w))


# ---------------------------------------------------------------------------
# Pricing / edge / sizing (moneyline)
# ---------------------------------------------------------------------------


def decimal_odds(price: float) -> float:
    """Polymarket price == implied probability, so decimal payout = 1/price."""
    return float("inf") if price <= 0 else 1.0 / price


def passes_odds_band(price: float, odds_min: float = ODDS_MIN_DEFAULT,
                     odds_max: float = ODDS_MAX_DEFAULT) -> bool:
    if price is None or price <= 0 or price >= 1:
        return False
    d = decimal_odds(price)
    return odds_min <= d <= odds_max


def edge(p_model: float, price: float) -> float:
    """Probability edge: model win prob minus the market-implied price."""
    return p_model - price


def kelly_fraction(p_model: float, price: float) -> float:
    """Full-Kelly stake fraction for backing a side at `price` with win prob `p_model`.

    Decimal odds d = 1/price, net odds b = d - 1. f* = (p*b - (1-p)) / b. Negative
    (no edge) clamps to 0. Callers apply HALF-Kelly and the constitution's size caps.
    """
    if price <= 0 or price >= 1:
        return 0.0
    b = decimal_odds(price) - 1.0
    if b <= 0:
        return 0.0
    f = (p_model * b - (1.0 - p_model)) / b
    return max(0.0, f)


def half_kelly(p_model: float, price: float) -> float:
    """Half-Kelly fraction (constitution §2)."""
    return kelly_fraction(p_model, price) / 2.0


def devig_two_way(price_a: float, price_b: float) -> tuple[float, float] | None:
    """Remove the overround from a two-way market -> fair implied probabilities.

    Returns (fair_a, fair_b) normalized to sum 1, or None if prices are unusable.
    Use the fair price (not the raw price) as the honest market benchmark.
    """
    if not price_a or not price_b or price_a <= 0 or price_b <= 0:
        return None
    s = price_a + price_b
    if s <= 0:
        return None
    return price_a / s, price_b / s
