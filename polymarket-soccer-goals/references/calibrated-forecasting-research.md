# Calibrated forecasting for football goals & BTTS — deep-research report

Research synthesis (June 2026) for porting the MLB 4-layer calibrated-forecasting
architecture to soccer **total goals (over/under)** and **both-teams-to-score (BTTS)**.
Goal: not betting edge, but an accurate goal/BTTS forecast WITH a *validated* confidence.

Confidence tags: **High / Medium / Low**. Several primary PDFs (Wiley/JRSS, ScienceDirect,
arXiv) were 403-blocked to the fetch tool, so single-number claims are flagged; model
*structures* are cross-corroborated and high-confidence. Two claims were verified
numerically against this repo's own `dixon_coles.py` (see §6).

---

## Executive summary

Football goals are close to **Poisson** at the aggregate level (variance ≈ mean — *unlike*
baseball runs, which are ~2× overdispersed). The field-standard model is **Dixon-Coles**:
independent Poisson + a 4-cell low-score correction (ρ) + exponential time-decay. Both
over/under and BTTS are read off the same **score matrix**. The four-layer plan ports
cleanly, with soccer-specific twists:

1. **Distribution** — Dixon-Coles score matrix → total-goals pmf (anti-diagonal sums) and
   BTTS via inclusion-exclusion. Overdispersion in *goals* is mild (NB not needed; NB *is*
   needed for corners/cards). The DC ρ-correction barely moves the O/U 2.5 line and moves
   BTTS only ~1pp (verified, §6).
2. **Calibration** — same toolkit as MLB (reliability diagram, ECE/MCE, Murphy Brier
   decomposition), but **calibrate each market separately** (1X2 / O-U / BTTS carry
   different biases) and **prefer Platt/temperature/beta over isotonic** (a season ≈ 380
   games is far below isotonic's ~1000-sample need). Both bookmaker odds *and* stat models
   are documented to be **overconfident on heavy favourites** and to **underrate draws**.
3. **Per-prediction confidence** — total-goals prediction intervals are wide and
   irreducible (~99% aleatoric per match); the 80% interval ≈ **1–5 goals**. Predictive
   entropy works as a per-match confidence scalar. **Conformal prediction has never been
   applied to football scorelines** (genuine gap; exists for NCAA basketball + time series).
4. **Validation** — **RPS** is the football standard for 1X2, but for the **binary** O-U
   and BTTS markets **RPS reduces exactly to the Brier score**, so report **Brier + log-loss**
   there and **CRPS** for the full goals-total distribution. Validate **walk-forward**. Best
   models sit at RPS ≈ 0.19–0.21 vs bookmaker ≈ 0.20 — beating the *closing* line is hard.

---

## §1 — Layer 1: the goals distribution (and BTTS derivation)

- **Maher (1982)** — independent double-Poisson with attack/defence + home parameters; he
  already noted a small positive home/away correlation (~0.2). *High* (structure); *Medium*
  (the 0.2).
  *Statistica Neerlandica* 36:109–118.
- **Dixon-Coles (1997)** — independent Poisson × τ correction on the 4 lowest cells:
  τ(0,0)=1−λμρ, τ(0,1)=1+λρ, τ(1,0)=1+μρ, τ(1,1)=1−ρ; τ=1 elsewhere. Plus exponential
  time-decay φ(t)=exp(−ξt), ξ≈0.0065/day tuned out-of-sample. Fixes Poisson's
  under-prediction of 0-0/1-1 draws. *High*.
  *JRSS-C* 46(2):265–280, doi:10.1111/1467-9876.00065.
  ⚠ **Sign convention of ρ** differs across sources (small negative in the primary; some
  blogs flip the sign) — substantively identical (low-score draws inflated), but it will
  silently break a re-implementation. *High* this is a convention, not a real conflict.
- **Bivariate Poisson (Karlis-Ntzoufras 2003)** — shared-shock λ₃ = Cov(home,away), but
  structurally **only non-negative** correlation; their own diagonal-inflated extensions
  *did not materially improve* the goals fit. Optional, under-delivers for goals. *High*.
  *JRSS-D* 52(3):381–393.
- **Copulas (McHale-Scarf 2007/2011; Boshnakov et al. 2017, Frank-copula bivariate Weibull)**
  — can represent the **weak negative** home/away dependence the bivariate Poisson cannot;
  the Weibull-count model beat Poisson/DC on fit and showed positive backtest returns.
  Use only if you need true dependence modeling. *Medium-High*.
- **Overdispersion** — goals are ≈ Poisson at league-season aggregate (χ² fails to reject);
  mild overdispersion appears at single-team granularity. The baseball contrast: runs are
  clearly ~2× overdispersed → NB; **goals are not**, so Poisson/DC stay standard for goals,
  while **corners/cards need NB**. *Medium-High*.
- **Over/Under from the matrix** — build M[i][j]=P(home=i)P(away=j) (×τ for DC), then
  P(Total=k)=Σ_{i+j=k} M[i][j]; Over 2.5 = Σ_{i+j≥3}. *High*.
- **BTTS** — inclusion-exclusion identity (holds for ANY joint):
  **P(BTTS)=1−P(home=0)−P(away=0)+P(0-0)**. Under independence it factors to
  **(1−e^{−λ_home})(1−e^{−λ_away})**. *0.99 (identity)*.
- **Typical values** — league total ≈ 2.5–2.9 g/match (Bundesliga highest ~3.1, Serie A
  ~2.56; ~2.71 pooled European), split ~1.5 home / ~1.2 away. BTTS base rate ~52–60%
  (≈55–57% modern PL/Bundesliga). *High* (band); *Low* (any single point — drifts by
  league/season, verify on your data).

## §2 — Layer 2: calibration

- **Method**: reliability diagram (prefer the **CORP/PAV** consistent version), **ECE/MCE**
  (use adaptive/equal-mass bins — naive fixed-width ECE is gameable), **Murphy decomposition**
  Brier = Reliability − Resolution + Uncertainty. *High*. (Guo 2017; Murphy 1973; Siegert
  2017; Dimitriadis-Gneiting-Jordan PNAS 2021.)
- **Football odds ARE broadly well-calibrated but biased**: (a) **favourite-longshot bias**
  — longshots overbet, favourites underbet, replicated even on Betfair Exchange (behavioural,
  not bookmaker-set); (b) **draws underrated / worst-discriminated** (Štrumbelj-Šikonja 2010;
  the original DC motivation). *High* on directions; *Medium* on exact magnitudes (some from
  a non-peer-reviewed blog study: bet365 ≈1.21% / Betfair ≈1.72% calibration error).
- **FiveThirtyEight SPI** publicly claims "32% means 32%" calibration, with a documented
  failure: **overconfident on heavy favourites** (2020: 80%-favourites won ~72%) — same
  direction as the market's bias. *High* (direction); *Medium* (the 80→72 figure).
- **Post-hoc method choice (critical, small data)**: isotonic needs **≥~1000** calibration
  points (Niculescu-Mizil & Caruana 2005) — a single league-season (~380) is far below, so
  use **Platt (sigmoid)**, **temperature** (1-param, most data-efficient, preserves argmax),
  or **beta calibration** (3-param, better at the 0/1 tails where football is worst). *High*.
- **Calibrate each market separately** — no published basis for assuming 1X2, O/U and BTTS
  share a curve; they carry different biases (longshot vs slight over-bias on Overs). *High*.

## §3 — Layer 3: per-prediction confidence

- **Aleatoric dominates**: a Bayesian EPL study put irreducible (aleatoric) uncertainty at
  **~99.4%** of total per-match uncertainty — better models barely shrink a single match's
  interval. *Medium* (single study) but directionally **High** (matches the sport's nature).
- **Total-goals intervals are wide**: for λ_total≈2.6 the **80% central interval ≈ 1–5 goals**,
  95% ≈ 0–6; SD ≈ √λ ≈ 1.6. Discreteness makes central intervals **conservative (over-cover)**
  — the safe direction (same as MLB NegBin). *High*.
- **Predictive entropy** of the scoreline/1X2 distribution is a usable confidence scalar
  (1X2 max = log₂3 ≈ 1.585 bits). *High* (math); *Medium* (the vendor empirics).
- **Conformal prediction**: distribution-free coverage ≥1−α under exchangeability
  (Angelopoulos-Bates 2023); **CQR** (Romano 2019) for heteroscedastic intervals; **ACI/DtACI**
  (Gibbs-Candès 2021) for *non-exchangeable temporal* data — the correct tool for a season.
  **But: no published application of conformal prediction to football scorelines/goals**
  (it exists for NCAA-basketball win prob and generic time series). *Medium-High* this is a
  real gap. → A split-conformal/ACI wrapper on a DC goals model would be novel and buildable,
  but per §3 its expected gain is small **if** the DC intervals already cover near-nominal
  (test empirically first — same thermometer logic as the MLB build).

## §4 — Layer 4: validation

- **RPS** (Epstein 1969; Constantinou-Fenton 2012) is the 1X2 standard because outcomes are
  ordinal: RPS = (1/(r−1))Σ_i (Σ_{j≤i}(p_j−e_j))². *High*.
- **Binary markets** (O/U, BTTS): **RPS reduces exactly to the Brier score** (r=2), so report
  **Brier + log-loss**, not "RPS". Log-loss punishes overconfidence harder (unbounded); both
  strictly proper. *High*.
- **CRPS** for the full goals-total count distribution (Gneiting-Raftery 2007) — the discrete
  analogue of RPS, reduces to MAE for a point forecast (exactly as in the MLB build). *High*
  (propriety); *Medium* that football literature routinely uses it (it mostly uses bucketed
  RPS / log-loss).
- **Validate walk-forward** (expanding/rolling origin); never random k-fold (leakage). Apply
  DC time-decay tuned out-of-sample. *High*.
- **Benchmarks**: best 1X2 models RPS ≈ 0.19–0.21, bookmaker ≈ 0.198–0.202 — at/just inside
  the line; **odds are a superior, hard-to-beat benchmark** (Hvattum-Arntzen 2010). O/U and
  BTTS are *marginally* exploitable in single peer-reviewed studies (Wheatcroft 2020 ~0.8%/bet
  with shots+corners; da Costa 2022 BTTS), but vs *average* not *closing* odds. *High* (hard
  to beat); *Medium* (the profit figures — period/overfitting caveats).
- **Scoring-rule dispute to flag**: Wheatcroft (2021) "case against RPS" favours the log
  (ignorance) score for 1X2; RPS is *proper*, the critique is about usefulness, not propriety.
  → Report **both** RPS and log-loss for 1X2. *High* (dispute exists).

## §5 — direct BTTS modeling vs goals-derived

da Costa, Marinho & Pires (2022, *IJF* 38(3):895–909) compared a **direct BTTS classifier**
to **goals-model-derived BTTS** → **similar performance**. So derive BTTS from the goals
model (one fit gives O/U + BTTS + correct-score consistently); the marginal goal rates
λ_home/λ_away dominate, not the dependence correction. *Medium-High*.

## §6 — Numerical verification against this repo's `dixon_coles.py`

Two flagged/derived claims, tested by sweeping realistic (λ_home, λ_away) ∈ [0.6, 2.1]² and
ρ ∈ {−0.13, −0.10, −0.05} on `dc.score_matrix` / `dc.prob_btts` / `dc.prob_over`:

| Quantity | DC(ρ) vs independent Poisson (ρ=0) | Verdict |
|---|---|---|
| **BTTS** | mean \|Δ\| = **0.98 pp**, max **1.74 pp** | **C4 CONFIRMED** — second-order |
| **O/U 0.5** | mean **1.37 pp**, max 1.74 pp | DC matters on low lines |
| **O/U 1.5** | mean **1.37 pp**, max 1.74 pp | DC matters on low lines |
| **O/U 2.5** | mean **0.00 pp** | **refutes** "DC raises Under 2.5" |
| **O/U 3.5** | mean **0.00 pp** | DC invisible on high lines |

**Why**: the τ correction only touches (0,0),(0,1),(1,0),(1,1) — all ≤2 total goals, all on
the Under side of 2.5 — so redistributing mass among them **preserves the Under-2.5 total
exactly** (Δ=0 on O/U 2.5+), but **does** move BTTS because only (1,1) is BTTS=Yes. The
research agent's claim that "DC directly shifts Under 2.5 upward" is therefore **wrong for the
2.5 line** (true only for 0.5/1.5). Practical upshot: **for O/U 2.5 the ρ-correction is
irrelevant — getting λ right is everything**; for BTTS it's a real but ~1pp effect.

---

## Recommended implementation (porting the 4 layers to soccer)

1. **Distribution** — keep Dixon-Coles; expose the full **goals-total pmf** + **BTTS** from the
   same matrix. Add a `forecast_block` (mean/median/mode total, 50%/80% intervals, entropy)
   like the MLB skill. NB only if you later add corners/cards.
2. **Calibration** — reuse the MLB `calibration_core` (reliability/ECE/MCE/Murphy + Platt/
   temperature/isotonic), but **fit per market** (O/U and BTTS separately) and **default to
   temperature/Platt** (not isotonic) until ≥~1000 settled outcomes. Audit the heavy-favourite
   / Over bins explicitly.
3. **Confidence** — total-goals 50%/80% intervals + entropy per match; BTTS is binary so its
   "interval" is just the calibrated probability + its reliability bin. Hold off on conformal
   until the empirical interval-coverage thermometer (port the MLB one) shows the DC intervals
   are miscalibrated on real games.
4. **Validation** — extend the soccer backtest with **CRPS** (goals total), **Brier + log-loss**
   (O/U and BTTS), **interval coverage**, ECE — all **walk-forward**. Benchmark O/U/BTTS Brier
   vs the sharp/closing line (the real test), not just vs a coin flip.

## Open items / caveats

- ρ **sign convention** — confirm `dixon_coles.py` matches the cited τ (it reproduces the
  expected direction: BTTS up ~1pp with ρ=−0.13, §6).
- **Conformal-for-football is unproven** — treat as optional R&D gated by the coverage
  thermometer, exactly as we concluded for MLB.
- Single-number figures (538 80→72, statsbet 1.21/1.72%, Wheatcroft 0.8%/bet, ~99.4%
  aleatoric) are single-source / non-peer-reviewed or 403-blocked — verify before quoting.
- RPS-vs-log-loss is an open dispute — report both for 1X2.

_Paper-trading research — not financial advice. Real trading involves risk of loss._
