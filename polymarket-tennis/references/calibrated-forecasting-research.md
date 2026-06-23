# Calibrated forecasting for tennis match-winner (moneyline) — deep-research report

Research synthesis (June 2026) for the tennis match-winner market, with a specific verdict on
four candidate predictors the operator asked about: **match-time weather**, **head-to-head (H2H)**,
**handedness (lefty/righty matchup)**, and **surface (clay/grass/hard)**. Goal: not betting edge,
but an accurate win prediction WITH a *validated* confidence.

Confidence tags: **High / Medium / Low**. Most primary PDFs were 403-blocked to the fetch tool, so
single numbers are flagged; model *structures* are cross-corroborated and high-confidence. The
surface-blend mechanism and the Elo formula were verified numerically against this repo's `elo.py`.

---

## Executive summary

The match-winner outcome is **binary**, so the forecast IS a probability — there is no wide
goals/runs interval as in MLB/soccer. The field-standard, hard-to-beat backbone is **surface-blended
Elo**: `P(A beats B) = 1/(1+10^((Elo_B−Elo_A)/400))` with each player's effective rating a ~**50/50
blend of overall and surface-specific Elo**. Best models reach **~66% accuracy / Brier ~0.21**; the
bookmaker close sits ahead at **~69% / Brier ~0.196** — so the real test of edge is **closing-line
value**, not accuracy. Our `elo.py` already implements this backbone (`SURFACE_BLEND=0.5`).

**Verdict on the four candidate predictors:**

| Predictor | Enters the model? | Why (confidence) |
|---|---|---|
| **Surface** (clay/grass/hard) | ✅ **YES — already in** | Surface-specific Elo blended ~50/50 beats overall-only by +1.5–2.5pp accuracy and lowers Brier. ⚠️ surface-*only* Elo is **worse** than overall (small-sample noise) — the value is the *blend*. *High*. |
| **Handedness** (L/R) | ⚠️ **NOT as a main effect** | The lefty edge is real but small, **declining**, gender/quality-dependent, and **already absorbed into Elo** (Elo counts that a player wins, not why). Only a **lefty×righty interaction** carries non-redundant signal (~1–2%, shrinking) — optional, validate before trusting. *0.75*. |
| **Head-to-head** (H2H) | ❌ **NOT raw** | Adds ~nothing out-of-sample beyond ratings; most pairs have 0–3 prior meetings so it's dominated by sampling noise, and what it reflects is already in Elo. Only a thin residual in "intransitive" matchups (single unreplicated 2025 preprint). If used at all, **shrink toward the Elo-implied prob**. *High*. |
| **Weather / conditions** | ❌ **NOT as a feature** | No peer-reviewed evidence weather improves out-of-sample win prediction once surface ratings are present. Altitude/indoor/court-speed are real but **already captured by surface-speed/surface-Elo**; heat & wind are **variance modifiers, not directional favourite signals**; reliable pre-match weather data is a practical bottleneck. *Medium-High*. |

**One-line rule:** the only predictor that adds validated incremental value is **surface** (already
in). Handedness, H2H and weather are mostly *already priced into Elo* or *too noisy* — adding them as
raw features risks overfitting. The honest move for handedness/H2H is a small, *shrunk* interaction
term, validated by ΔBrier before use.

---

## §1 — The backbone model (verified against `elo.py`)

- **Elo win prob** `P(A)=1/(1+10^((Elo_B−Elo_A)/400))` — universal (538, Sackmann). *High*.
- **Surface-specific Elo blended ~50/50** with overall is the single highest-value enhancement.
  Sackmann: blended weighted-surface Elo ≈ hard 68.6%/0.202, clay 68.0%/0.207, grass 69.8%/0.196.
  Vaughan-Williams (JQAS 2021): a 56/44 blend hit 74.1% acc, beating overall-only (71.8%),
  **surface-only (70.5%)**, and the close (72.4%). *High* (the blend wins; surface-only loses).
  - ⚠️ **Optimal weight is contested but flat**: 538 ≈ 29% surface (hard), Sackmann ≈ 44%,
    Angelini/Candila ≈ 5.6% ATP / 65% WTA. The optimum is flat across ~30–55% — **50/50 is a robust
    default**; treat the exact weight as a low-sensitivity knob. Men benefit more than women.
- **Alternatives** (not needed for moneyline): Bradley-Terry (Elo is its online special case);
  point-based serve models (Klaassen-Magnus, Barnett-Clarke, Ingram) — best on *discrimination/AUC*
  but not on accuracy/log-loss (Kovalchik 2016). Points are *not* iid but the deviations are small
  enough that iid is "good enough" for forecasting (Klaassen-Magnus 2001). *High*.
- **Numerical check (this repo's `elo.py`)**: a clay specialist (clay 2050) vs a grass specialist
  (grass 2080), both ~1900–1950 overall → P(clay-specialist wins) = **0.660 on clay, 0.309 on grass**,
  0.50 on hard; overall-only Elo gives 0.50 everywhere (surface signal lost). Confirms the blend is
  doing real, surface-dependent work, exactly as the literature predicts.

## §2 — The four candidate predictors (detailed)

### Surface — ✅ include (done)
Real, large, incremental. Mechanism: ace/hold rates shift hard ≈ grass > clay (grass/fast hard
reward servers; clay rewards defence/topspin). Already implemented as `blended_elo`. *High*.

### Handedness — ⚠️ interaction-only, optional
~13–15% of pros are lefty (vs ~11% population); the edge is negative-frequency-dependent (rarity →
unfamiliarity), so it's a **matchup term, not a skill term**. ~2% at the game level, concentrated on
ad-court break points; larger in men, "almost absent" in women, and **declining** over decades. A
lefty's wins are already in their Elo, so a main-effect dummy ≈ 0 incremental. The only non-redundant
piece is a **lefty×righty interaction**; even that is ~1–2% and falling. *Falsifiable test*: fit Elo
+ lefty dummy (expect ≈0) vs Elo + lefty×opp-hand interaction (expect small, possibly significant,
<0.5pt acc). *0.75*.

### Head-to-head — ❌ not raw
Sackmann's canonical "Limited Value of Head-to-Head Records": barely moves the needle because pairs
have too few meetings; ratings aggregate *all* matches, H2H discards almost everything. Sharp lines
track style-neutral Elo, not H2H (pundits overweight it, not the close). The one credible counterpoint
(GNN intransitivity, arXiv 2510.20454, 2025): on *average* it does **not** beat Elo (65.7%/0.215 vs
66.5%/0.212), but a +3.26% Kelly ROI appears when *selectively* targeting high-intransitivity matchups
— a single unreplicated preprint, classic selection-on-subset risk. If used: **shrink H2H toward the
Elo-implied probability**, never feed the raw rate. *High* (don't use raw); *Medium* (intransitivity).

### Weather / conditions — ❌ not a feature
No peer-reviewed evidence that raw weather improves out-of-sample winner Brier/accuracy once surface
ratings exist; the leading models (GNN, surface-Elo, GS statistical-learning) achieve their numbers
**with no weather features**. Altitude (Madrid ~660m → faster ball, higher bounce, servers favoured)
and indoor/outdoor are **already captured by surface-speed / surface-Elo** (Hawk-Eye Court Pace Index
explicitly *bundles* temperature/humidity/wind/ball — so weather and CPI are collinear, not additive).
Heat is real for *how* a match plays (fewer winners, more double faults, more medical calls >32°C
WBGT) but its effect on the *winner* is ambiguous (it hits both players); wind raises unforced errors
and *variance*. Pre-match weather data is also a practical bottleneck (forecasts are coarse; CPI is
in-play). **Best practice: treat heat/wind as confidence/variance modifiers (widen uncertainty,
reduce confidence), not as directional favourite shifts.** *Medium-High* (a defensible negative
result — exclude to avoid overfitting).

## §3 — The 4 calibrated-forecasting layers, mapped to a BINARY market

1. **Distribution** — the outcome is Bernoulli(p); the forecast *is* the win probability p. (A serve
   model could give a set/games distribution, but moneyline needs only p.) So "Layer 1" here is just
   the calibrated p plus its **Bernoulli entropy** (max 1 bit at p=0.5) as the spread measure.
2. **Calibration** — reuse the shared `calibration_core`: reliability diagram, **ECE/MCE** (ECE is
   *not* proper — secondary to Brier/log-loss), **Murphy Brier decomposition**, and post-hoc
   **Platt/temperature** (isotonic once ≥~1000 settled matches — easily met for ATP/WTA). Tennis
   models are documented as **roughly well-calibrated** (538/Elo ≈ Pinnacle on Brier).
3. **Per-prediction confidence** — for a binary outcome there is **no interval**; confidence = the
   calibrated p, and its trustworthiness is established by calibration over many matches, not within
   one. Report the Bernoulli entropy and the reliability bin the prediction falls in. Heat/wind →
   widen uncertainty (§2). **Conformal prediction has never been applied to tennis** (precedent: NCAA
   basketball, Johnstone-Nettleton) — an optional novelty, but a binary outcome gains little from it.
4. **Validation** — **Brier + log-loss** (RPS reduces to Brier for binary), reliability/ECE, **all
   walk-forward** with point-in-time Elo (never k-fold — leakage). Benchmark: good model ~66%/Brier
   0.21, market ~69%/0.196 — **judge edge by closing-line value, not accuracy**. Favourite-longshot
   bias means any residual edge is likelier on favourites. *High*.

## §4 — Recommended implementation (porting the layers to tennis)

1. **Keep the surface-blended Elo backbone** (`elo.py`, already optimal at 50/50). Do **not** add H2H,
   handedness, or weather as raw features — the evidence says they're already priced into Elo or too
   noisy, and adding them risks overfitting on this near-efficient market.
2. **forecast_block (Layer 1+3)**: emit the calibrated win prob + Bernoulli entropy + (optionally) a
   heat/wind "uncertainty flag" that *widens* confidence rather than moving the favourite.
3. **Calibration (Layer 2)**: reuse `calibration_core` + the `calibration.py --sport tennis` path
   (reliability/ECE/Murphy + temperature/Platt). Calibrate the win prob; isotonic only past ~1000.
4. **Validation (Layer 4)**: a `backtest_tennis.py` walk-forward — point-in-time surface Elo →
   Brier + log-loss + reliability/ECE + accuracy, benchmarked vs the devigged closing line. The two
   *optional* experiments worth an explicit ΔBrier ablation before adoption: (a) a shrunk lefty×righty
   interaction, (b) H2H shrunk toward the Elo-implied prob. Adopt only if they improve walk-forward
   Brier — the research expects ~no gain.

## Open items / caveats
- Optimal surface weight is sample/era/gender-specific; 50/50 is the robust default (verified flat
  optimum). WTA may prefer heavier overall weight.
- The H2H-intransitivity ROI and the handedness-interaction ΔBrier are both *unestablished* in clean
  ablations — run them on your own data before trusting (both are cheap walk-forward tests).
- Single-number figures (74.1% blend, 538 0.71/0.29, Kovalchik tables) were 403-blocked — verify
  before quoting as load-bearing.
- Models are calibrated but do **not** beat the closing line (Wilkens 2021) — the market is the bar.

_Paper-trading research — not financial advice. Real trading involves risk of loss._
