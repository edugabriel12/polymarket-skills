# Run model — Negative Binomial total-runs distribution

Implemented in `scripts/run_distribution.py` (pure stdlib `math`; no numpy/scipy).
Grounded in `research/mlb-total-runs-deep-research.md`.

## Why Negative Binomial (not Poisson)

MLB runs are **overdispersed** — empirically `variance ≈ 2 × mean` per game (AL game mean ~4.50,
variance ~10.0; NL ~4.26 / ~9.14) — and zero-inflated (more shutouts than memoryless models
predict). Plain Poisson forces `variance = mean`, so it underestimates shutouts, overestimates
1-run innings, and has too thin a tail. The Negative Binomial's second parameter absorbs the
overdispersion and fits both per-inning and per-game runs well.

## The math

Model the **game total** `T` directly as NegBin with mean `μ` and variance `v = dispersion·μ`
(`dispersion = 2.0` by default). Method of moments, in the `(r, p)` form
(`mean = r(1−p)/p`, `var = r(1−p)/p²`):

```
r = μ² / (v − μ)          # requires v > μ (overdispersion); with v = 2μ → r = μ, p = 0.5
p = μ / v                 # = r / (r + μ)
```

PMF for real `r`, via log-gamma for stability:

```
P(T = k) = exp( lgamma(k+r) − lgamma(r) − lgamma(k+1) + r·ln(p) + k·ln(1−p) )
```

`negbin_total_runs_pmf(μ, v, kmax=40)` evaluates `k = 0..kmax`, folds the residual tail into
`kmax`, and renormalizes to sum exactly 1.0.

### Over / Under from the PMF

`prob_over(line, pmf)`:
- **Half-run line** (e.g. 8.5): no push. `need = ceil(line)`; `P(Over) = Σ_{k≥need} pmf[k]`,
  `P(Under) = Σ_{k<need} pmf[k]`.
- **Integer line** (e.g. 9.0): push at `T = line`. `P(push) = pmf[line]`; the *effective*
  probabilities renormalize over the non-push mass: `P_over_eff = P_over / (1 − P_push)`.

`P(Over)` is **monotone increasing in μ** (the tail shifts right) — the basis for the
market-implied solver.

## Deriving μ

```
baseline_mu(park_factor, league_baseline=8.5) = league_baseline × park_factor/100
```

`adjust_mu(base, *, home_off, away_off, home_sp, away_sp, home_field, temp_f, wind_out_mph)`
applies optional, **capped** adjustments. Each team's runs `= (base/2) × own_offense ×
opponent_pitching`; weather adds small deltas:
- temperature: `×(1 + clamp((T−70)/10 × 1%, ±6%))`
- wind out: `+ min(2.0, max(0, mph−5) × 0.07)` runs
All factors are clamped to `[0.70, 1.30]` and μ to `[3, 18]` so a bad input can't explode the mean.

### Features — INCLUDED vs EXCLUDED (per operator choice)

| Feature | Status | Rationale |
|---|---|---|
| **Home/away** (park factor + small `home_field` delta) | **INCLUDED** | Home park sets the run environment (Coors ~+28%); strong evidence. |
| **Season retrospect** (team season run-scoring + starter/bullpen RA9 factors) | **INCLUDED** | Season rates are the core of µ; the predictive "matchup" is this lineup vs the opposing starter (handedness/platoon ~0.017 wOBA), folded into the offense factor. |
| **Short-term recent form** (last-N games) | **EXCLUDED** | "Hot streaks" are mostly noise that regresses; season rates beat them. |
| **Head-to-head team-vs-team** | **EXCLUDED** | Small-sample noise, rosters change; not predictive. The predictive matchup is lineup-vs-starter, not "Team A vs Team B" history. |

## Anti-fabrication fallback

`market_implied_mu(line, market_p_over, dispersion)` bisects μ so the model's `P(Over)` equals the
market price. When **no external inputs exist**, the pipeline uses this μ → model edge ≈ 0 → the
decision tree rejects every trade. The skill therefore **never invents an edge** from nothing;
edge only appears when real inputs move μ away from the market-implied value.

## Recalibration knobs

`--dispersion` (default 2.0) and `--league-baseline` (default 8.5) are exposed because the run
environment shifts season to season. The `variance = 2×mean` approximation slightly undercounts
shutouts; the Enby / zero-modified NegBin (research §1.2) is the documented upgrade path.
