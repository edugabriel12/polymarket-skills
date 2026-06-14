# Dixon-Coles goal model

Implemented in `scripts/dixon_coles.py` (pure stdlib `math`). Grounded in
`research/soccer-goals-btts-deep-research.md`.

## Why Dixon-Coles (not Negative Binomial)
Soccer goals are **~equidispersed** (variance/mean ≈ 1), unlike baseball (~2×). So Poisson is the
right base — the NegBin used by the MLB skill is unnecessary here. The independent double-Poisson
(Maher 1982) underestimates draws/low scores, which **Dixon-Coles (1997)** fixes with a low-score
correction `τ`.

## The math
Home goals `X ~ Poisson(λ_home)`, away `Y ~ Poisson(λ_away)`. Joint score matrix:

```
P(i,j) = τ(i,j; λ_home, λ_away, ρ) · Poisson(i; λ_home) · Poisson(j; λ_away)
```

with the DC correction (1 outside the four corners), then renormalize:

| (x,y) | τ |
|---|---|
| (0,0) | 1 − λμρ |
| (0,1) | 1 + λρ |
| (1,0) | 1 + μρ |
| (1,1) | 1 − ρ |

`ρ = 0` recovers independent Poisson. **Negative ρ raises draw/low-score probability** (the
empirically observed correction); default `ρ = -0.10` (tunable per league via `--rho`).

### Markets from the matrix
- **`prob_over(line, matrix)`** — `P(Over X.5) = Σ_{i+j>X} P(i,j)`. Integer lines (2.0) handle a
  push at `i+j == line` and renormalize the effective probabilities.
- **`prob_btts(matrix)`** — `P(BTTS yes) = Σ_{i≥1, j≥1} P(i,j)`. Equivalently
  `1 − e^{−λ} − e^{−μ} + τ(0,0)·e^{−(λ+μ)}` under the correction.

## Deriving λ
```
lambdas_from_total_supremacy(total, supremacy) -> ((total+sup)/2, (total-sup)/2)   # clamped
```
- **total** = league baseline (`leagues.py`) × attack/defense factors (`adjust_total`, geometric mean
  of supplied factors). Baselines: Bundesliga ~3.14, Ligue 1 ~2.96, EPL ~2.93, La Liga ~2.62,
  Serie A ~2.56, World Cup ~2.55; default 2.70.
- **supremacy** (home goal differential): `supremacy_from_elo(elo_home, elo_away, home_adv_elo=65,
  goals_per_elo=1/350)` — home advantage 0 for neutral competitions (`fifwc`, `euro`, ...). The
  constants are tunable approximations and **need calibration per league/data**.
- xG path: `total = total_xg`, `supremacy = supremacy_xg` when xG inputs are available.

**BTTS asymmetry:** `P(BTTS)` is governed by the **smaller** λ (the weaker attack vs the stronger
defense), so it falls as |supremacy| grows even at a fixed total — a lopsided "Over 2.5" game can be
"No BTTS". This is why defensive ratings/key-defender news matter more for BTTS than for the total.

## Anti-fabrication fallback
When no external inputs exist:
- `market_implied_lambdas(line, p_over[, p_btts])` — bisects the total to match the Over price, then
  (if given) the supremacy to match the BTTS price → reproduces the market on both, edge ≈ 0.
- `market_implied_from_btts(p_btts)` — for a standalone BTTS market, bisects the total (supremacy 0)
  to match the BTTS price.
So the skill never invents an edge from nothing; edge only appears when real inputs move λ.

## Recalibration knobs
`--rho` (DC dependence), the league baselines in `leagues.py`, and the Elo constants
(`home_adv_elo`, `goals_per_elo`) all need per-season/per-league calibration. Track Brier/log-loss
and CLV to validate.
