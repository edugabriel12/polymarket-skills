# Calibrated forecasting — the 4-layer architecture (MLB totals)

This skill's goal is **not only** to detect a betting edge. It also produces an
**accurate (or honestly approximate) prediction with a trustworthy confidence
measure**: a forecast you can believe *because its stated confidence has been
validated*. That requires four layers, each answering a different question.

> Why this matters: a single MLB game total is *irreducibly* uncertain — it can
> land anywhere from 0 to 20+ runs. No model removes that randomness (it is
> **aleatoric**, not a modeling failure). So the right deliverable is a *calibrated
> distribution* — a range plus an honest confidence — not a false-precision point.

---

## Layer 1 — Forecast a DISTRIBUTION, not a point

The game total `T` is modeled as a **Negative Binomial** (runs are overdispersed:
variance ≈ 2× mean), giving a full pmf over `T = 0..40`.

- Engine: `run_distribution.negbin_total_runs_pmf(mu, var)` → `pmf`.
- Read-outs: `forecast.py` →
  `mean_of_pmf`, `quantile`, `mode_of_pmf`, `prediction_interval`, `cdf`.
- `forecast.forecast_summary(mu, var, line)` returns mean / median / mode,
  P(over)/P(under)/push at the line, the 50% & 80% intervals, and entropy.

From a point estimate you can lie about precision; from a distribution you cannot.

## Layer 2 — CALIBRATE, so "confidence" means something

"P(Over) = 0.70" is only trustworthy if, across all the 0.70 calls, the Over hits
~70% of the time. `calibration_core.py` measures and corrects this.

**Measure**
- `reliability_diagram(pairs)` — predicted vs empirical frequency per bucket.
- `brier_decomposition(pairs)` — Murphy's identity
  `Brier = Reliability − Resolution + Uncertainty`
  (Reliability↓ = good calibration, Resolution↑ = good discrimination,
  Uncertainty = irreducible base rate `ō(1−ō)`).
- `ece` / `mce` — Expected / Maximum Calibration Error.

**Correct (post-hoc, fit on a HELD-OUT calibration set — never the training games)**
- `TemperatureCalibrator` — 1 param `T`; gentlest; preserves the argmax (only
  rescales confidence).
- `PlattCalibrator` — 2-param sigmoid `(A, B)`; works with little data.
- `IsotonicCalibrator` — monotonic PAV step map; most flexible, needs the most data.

Calibration is **suggestion-only** in this skill: the report shows the fitted
calibrator and its before/after ECE; the operator applies it manually. (Per the
constitution, the meta-layer never silently rewrites the live model.)

CLI: `python scripts/calibration.py --sport mlb --settle --fit-calibrator temperature`

## Layer 3 — Confidence PER prediction

Every prediction carries its own uncertainty statement, derived from its pmf:
- **50% / 80% prediction intervals** — the range the total is likely to land in
  (wide on purpose; that width is the honest aleatoric signal).
- **predictive entropy** (bits) — the spread/uncertainty of the distribution.

Surfaced live in `suggest_totals.forecast_block(m, line)` → stored in each
prediction's `stats_log.forecast` and shown on the dashboard's PredictionCard
("Previsão · total esperado ~N runs, intervalo 80% a–b, incerteza X bits").

Because the intervals are discrete they are *conservative* (realized coverage ≥
nominal), never overconfident — the safe direction.

## Layer 4 — VALIDATE with proper scoring rules (walk-forward)

A calibrated forecaster is judged by **proper scoring rules** — rules minimized (in
expectation) only by reporting true beliefs — computed **walk-forward** (no
look-ahead; `backtest.py` builds point-in-time team factors before each game).

- **CRPS** (`scoring.crps_pmf`) — scores the whole distribution, in **run units**,
  finite on tail blowouts; reduces exactly to `|forecast − actual|` for a point
  forecast (so it generalizes MAE). `CRPS = Σ_k (CDF(k) − 𝟙{k ≥ y})²`.
- **log-loss / Brier** (`scoring.log_loss`, `scoring.brier`) — strictly proper on
  the Over/Under binary.
- **coverage** (`scoring.coverage`) — does the 80% interval actually contain the
  truth ~80% of the time? (validates Layer 3.)

The backtest report now prints, per season and overall: CRPS, 80%/50% interval
coverage, ECE/MCE, and the Murphy Brier decomposition — alongside the existing
ROI / win-rate / CLV.

CLI: `python scripts/backtest.py --games-csv <history.csv>`

---

## Module map

| Layer | Module | Key entry points |
|---|---|---|
| 1 Distribution | `run_distribution.py`, `forecast.py` | `negbin_total_runs_pmf`, `forecast_summary` |
| 2 Calibration | `calibration_core.py` (math), `calibration.py` (CLI) | `ece`, `mce`, `brier_decomposition`, `fit_calibrator` |
| 3 Confidence | `forecast.py`, `suggest_totals.forecast_block` | `prediction_interval`, `predictive_entropy` |
| 4 Validation | `scoring.py`, `backtest.py` | `crps_pmf`, `coverage`, `run_backtest` |

All cores are **pure stdlib** (only `math`/`sqlite3`) and offline-testable:
`test_forecast.py`, `test_calibration_core.py`, `test_scoring.py` (plus the existing
`test_backtest.py`, `test_calibration.py`).

_Paper-trading research — not financial advice. Real trading involves risk of loss._
