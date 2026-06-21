# Deep Research — Tennis Match-Winner Prediction

Synthesis of a 5-angle deep-research pass (serve-based models, Elo/rating models, machine
learning, data sources/APIs, market efficiency & validation). Built for an operation that
analyzes tennis matches and suggests the winner, analogous to the existing soccer/MLB ops
(probabilistic model → edge vs market price → half-Kelly → calibration/CLV).

> **Method caveat:** `WebFetch` was HTTP-403 blocked on every PDF/journal this session, so
> numeric figures come from corroborated search extractions, not line-by-line PDF reads.
> Confidence is flagged per claim; exact Brier/log-loss/ROI decimals need confirmation in the
> primary PDFs before being hard-coded.

---

## Central verdict (HIGH confidence — corroborated across 4 of 5 angles)

Tennis moneyline markets are **highly efficient**. **No published model reliably beats the
Pinnacle closing line after the vig.** The accuracy ceiling is **~70% on the ATP** for every
model family; the bookmaker consensus (~72%) sits on top. So the operation must NOT bet "my
model predicts better than the market" — it must **detect when the Polymarket price diverges
from a well-calibrated surface-Elo model** (a mispricing / mean-reversion play). This is the
same philosophy as the soccer/MLB ops.

---

## 1. The four model families

### A) Serve-based hierarchical models (point → game → set → match)
Predict the winner from each player's probability of winning a point on serve, building up
recursively under an i.i.d.-points assumption.
- **Deuce** (HIGH): `P(win | deuce) = p²/(1 − 2pq)`, `q = 1−p`.
- **Game from 0–0** (MEDIUM — single source): `p⁴(15 − 34p + 28p² − 8p³)/(1 − 2p + 2p²)`.
- Set / tiebreak / match have closed/recursive forms in **O'Malley (2008)** — the reference
  implementation. Set-win probability is **independent of who serves first**.
- **Amplification:** p=0.51/point → ~0.57/set → ~0.64/match. Small per-point edges compound.
- **Klaassen & Magnus (2001):** points are NOT strictly i.i.d. ("winning mood", important-point
  effects, stronger for weaker players) but i.i.d. is a good forecasting approximation.
- **Barnett & Clarke (2005):** combine a player's serve vs the opponent's return relative to tour
  averages. Concept verified; exact algebra unverified.
- **Verdict:** best for *in-play* win probability and simulation. In head-to-head comparison
  (Kovalchik 2016) point-based had the **highest log-loss / lowest accuracy** of the three
  families. Use as a **secondary** layer, not the main engine.

### B) Rating models — Elo (RECOMMENDED core)
- **Win probability** (HIGH, 4+ sources): `P(A beats B) = 1 / (1 + 10^((Elo_B − Elo_A)/400))`.
- **FiveThirtyEight dynamic K-factor** (HIGH): `K = 250 / (n + 5)^0.4`, `n` = player's match count.
  New players start at 1500; high K early so few-match players move fast.
- **Surface-specific Elo + ~50/50 blend** (overall + surface) is the single highest-value
  enhancement over plain Elo (Tennis Abstract; test 50/50 vs 56/44 on your pool).
- **Performance** (Kovalchik 2016): 538-Elo ~70% overall, **~75% among top players**, "competitive
  with bookmakers" but does **not** beat them. Career history helps low-ranked (59%→64%), not top.
- **Variants:** **Glicko-2** (rating deviation/volatility) → better for low-history players
  (injury returns, juniors); **Weighted/MOV Elo** (Angelini et al. 2022 EJOR) → small bump
  (~66.4% / Brier 0.212 vs 65.8% / 0.215; market still tops at ~69% / 0.196).
- **Verdict:** **dynamic-K Elo + surface blend is the central engine** — peer-validated, simple,
  incremental, beats rankings.

### C) Machine learning (logistic, XGBoost, RF, NN)
- All land at **~65–70%** (ATP). Complexity buys little; **ML does not beat clean Elo/logistic or
  the market** (Wilkens 2021: "most signal is already in the odds").
- **Leakage warning:** Gao & Kowalczyk's 83% is almost certainly serve-outcome leakage — use it
  only for "serve strength matters", not the headline number.
- **Common-opponents model** (Knottenbelt/Spanias/Madurska 2012): the clean way to get
  surface-adjusted serve stats by comparing both players against shared opponents.

### D) Market (the benchmark, not noise)
Devigged implied probability is the most accurate forecast in every peer-reviewed study.
**Pinnacle = sharp reference.**

---

## 2. Predictive features (by importance)
1. **Elo/ranking difference** (dominant)
2. **Surface-specific Elo** (hard/clay/grass) — via common-opponents
3. **Recent form** (rolling 5/10/20-match serve/return averages)
4. **Head-to-head** (overall + by surface)
5. **Fatigue/rest** (matches/minutes/sets in last X days)
6. Serve advantage, indoor/outdoor, altitude, best-of-3 vs 5, tournament importance

Always use **differences** between the two players and **pre-match data only**. Serve-points-won
is the strongest signal — and therefore the most dangerous leakage vector.

---

## 3. Data stack (with license caveat)
| Source | Provides | Note |
|---|---|---|
| **Jeff Sackmann `tennis_atp` / `tennis_wta`** | Matches since 1968, rankings, stats, bios — CSV | ⚠️ **CC BY-NC-SA: non-commercial**. Legal review before live money |
| **`tennis_MatchChartingProject` / `_slam_pointbypoint`** | Point/shot-by-shot | Serve/return granularity |
| **Tennis-Data.co.uk** | ATP/WTA results **with odds** (`PSW/PSL`=Pinnacle, `B365`, Max/Avg), CSV/year | **Essential for backtest/CLV**. Filter `Comment` (Retired/Walkover) |
| **Tennis Abstract / Ultimate Tennis Statistics** | Elo overall + per surface | UTS is open-source: **`mcekovic/tennis-crystal-ball`** (Apache 2.0) — self-host the Elo |
| **Sportradar / API-Tennis / SportDevs / The Odds API** | Live + odds (Pinnacle incl. in The Odds API) | Paid for live point-by-point. Prices unconfirmed |

> 🔴 The richest free data (Sackmann) is **non-commercial**. Resolve before live/commercial use.
> For paper-trading/analysis it is fine with attribution.

---

## 4. Market efficiency & edges
- **Favorite-longshot bias exists** (longshots overbet; stronger for low-ranked, late rounds, big
  tournaments) but is mostly a **bookmaker-margin artifact**, weak on exchanges, not cleanly
  monetizable after the overround.
- **No model robustly beats the close.** Published positive ROIs (Sipko +4.35%, Cornman
  +3.3%/match, a +8.9% tipster) are **thin, in-sample, fragile** — low confidence until proven
  out-of-sample after vig.
- **CLV vs Pinnacle close = gold-standard skill metric.** +1–2% = good; +5% = excellent; <0 = no edge.

---

## 5. Validation & tennis-specific pitfalls
- **Metrics:** **log-loss** (primary — punishes confident-and-wrong), **Brier**, **calibration**
  curve. Target: beat the market's log-loss/Brier (~0.196 Brier / ~69%) or **stand down**.
  Accuracy alone (even 70%) is not edge.
- **Staking:** **quarter-Kelly** until calibration is proven, then **half-Kelly** (constitution §2).
  Full-Kelly with estimation error → >50% drawdowns.
- **Pitfalls:**
  - **Retirements/walkovers:** rules vary by book. **On Polymarket a retirement is NOT voided — it
    pays the player who advanced** (the match winner), so settle retirements to the advancer
    (ACERTO/ERRO), not ANULADO; only a true no-play walkover voids. Filter `Comment`/score
    `RET`/`W/O` accordingly.
  - **Timing leakage:** odds collapse when a retirement looms — never mix in-play price into a
    "pre-match" backtest. Fix the odds timestamp (use the close).
  - **Look-ahead in Elo:** update ratings only with pre-match info (walk-forward).
  - **Overfitting** surface×season (small samples); extra features add little.

---

## 6. Recommended architecture (mirrors the existing ops)
```
Scanner (day's matches + Polymarket odds)
  → Analyzer: dynamic-K Elo (538) + 50/50 surface blend → P(win) via logistic
             → [optional] serve-based hierarchical for in-play probability
  → Edge: P_model − P_implied(Polymarket, devigged)   ← acts only on mispricing
  → Strategy: quarter→half-Kelly, constitution caps
  → Paper trader (default)
  → Calibration/CLV: log-loss, Brier, reliability, CLV vs Pinnacle close
```
Same structure as the soccer/MLB ops — swap Dixon-Coles/NegBin for surface-Elo and total/BTTS for
moneyline. The edge is **price divergence**, not out-predicting the consensus.

---

## 7. Key sources
**Models:** Newton & Keller (2005) `cis.upenn.edu/~bhusnur4/.../NeKe2005.pdf` · O'Malley (2008,
JQAS) `ideas.repec.org/a/bpj/jqsprt/v4y2008i2n15.html` · Klaassen & Magnus (2001 JASA / 2003 EJOR)
`janmagnus.nl/papers/JRM065.pdf` · Barnett & Clarke (2005, IMA)
`academic.oup.com/imaman/article-abstract/16/2/113/704903` · **Kovalchik (2016)**
`vuir.vu.edu.au/34652/1/jqas-2015-0059.pdf` · Wilkens (2021) `journals.sagepub.com/doi/10.3233/JSA-200463`
· Angelini et al. (2022, EJOR) `cris.unibo.it/.../Weighted ELO...pdf` · Yue et al. (2022, Glicko,
PLOS One) · Knottenbelt et al. (2012, common-opponents)
`sciencedirect.com/science/article/pii/S0898122112002106` · Bunker et al. (2024)
`journals.sagepub.com/doi/10.1177/17543371231212235`

**Elo/implementation:** FiveThirtyEight US Open 2016 · Tennis Abstract Elo
`tennisabstract.com/blog/2019/12/03/an-introduction-to-tennis-elo/` · UTS
`ultimatetennisstatistics.com/eloRatings` · `github.com/mcekovic/tennis-crystal-ball` (Apache 2.0)

**Data:** `github.com/JeffSackmann/tennis_atp` (+`_wta`, `_MatchChartingProject`,
`_slam_pointbypoint`) · `tennis-data.co.uk` · The Odds API
`the-odds-api.com/sports/tennis-odds.html` · Sportradar `developer.sportradar.com/tennis`

**Market/validation:** Lahvička FLB `mpra.ub.uni-muenchen.de/47905/` · Abinzano et al.
`ssrn.com/abstract=2664708` · Pinnacle ATP efficiency · Tennis Abstract "Measuring the Performance
of Tennis Prediction Models" (log-loss/Brier/calibration).

---

## 8. Contested claims (flagged)
- ❌ "Blended Elo beats the closing line (74.1% > 72.4%)" — vendor source, contradicts all
  peer-review; likely in-sample bias.
- ❌ Gao & Kowalczyk 83% — serve-outcome leakage.
- ⚠️ ROIs +3–9% (Sipko/Cornman/tipster) — thin, fragile, do not replicate out-of-sample.
- ⚠️ API pricing — mostly "not confirmed" (only The Odds API free 500/mo and SportDevs 300/day
  were confirmed).

---

---

## 9. Data access when `raw.githubusercontent.com` is blocked

A follow-up research pass (some networks return a synthetic 404 for
`raw.githubusercontent.com` while `github.com` works). Options for the SAME Sackmann
match CSVs, and for Elo directly:

**Same files, different host (drop-in):**
- **jsDelivr** — `https://cdn.jsdelivr.net/gh/JeffSackmann/tennis_atp@master/atp_matches_2025.csv`.
  Independent multi-CDN (not a raw redirect). 20 MB/file cap — fine for the per-year match CSVs
  (~1-3 MB). Pin `@<sha>` for reproducibility; `@master` caches ~7 days.
- **statically.io** — `https://cdn.statically.io/gh/JeffSackmann/tennis_atp/master/atp_matches_2025.csv`
  (~25 MB cap). **raw.githack.com** is another caching proxy.
- **GitHub Contents API (raw media type)** — `api.github.com/repos/.../contents/<file>?ref=master`
  with `Accept: application/vnd.github.raw` (≤100 MB; 60 req/hr unauthenticated). ⚠️ Do NOT follow the
  JSON `download_url` field — it points back to the blocked `raw.githubusercontent.com`.
- **git clone / codeload** — `git clone --depth 1 https://github.com/JeffSackmann/tennis_atp.git`
  (and `tennis_wta`), or `codeload.github.com/.../tar.gz/refs/heads/master`. Host `github.com`/
  `codeload.github.com`, no size caps — the most reliable bulk path.

The skill tries raw → jsDelivr → statically automatically; `TENNIS_DATA_DIR` reads a local clone.

**Elo directly (skip computing from matches):**
- **wheeloratings.com** — `tennis_atp_ratings.html` / `tennis_wta_ratings.html`, overall + Hard/Clay/
  Grass Elo, CSV export on the stats pages.
- **Ultimate Tennis Statistics** — `ultimatetennisstatistics.com/rankingsTable?rankType=ELO_RANK`
  (and `HARD_ELO_RANK`/`CLAY_ELO_RANK`/`GRASS_ELO_RANK`, `&date=DD-MM-YYYY`), JSON-backed Bootgrid.
  Fully self-hostable via `mcekovic/tennis-crystal-ball` (Apache-2.0) + the `mcekovic/uts-database`
  Postgres image — zero external calls.
- **Tennis Abstract** — `tennisabstract.com/reports/atp_elo_ratings.html` (overall + blended
  hElo/cElo/gElo); JS-rendered report.
- **Mirrors of the match data:** Kaggle `guillemservera/tennis` (faithful Sackmann mirror, ATP+WTA,
  CSV/Parquet/SQLite), `Tennismylife/TML-Database` (GitHub, ATP, daily). Commercial tennis APIs
  (Sportradar/Goalserve/API-Tennis/SportDevs) expose rankings/results but **not Elo**.

*Sources: jsDelivr/GitHub/Statically docs; tennisabstract.com; ultimatetennisstatistics.com;
wheeloratings.com; kaggle.com; github.com/Tennismylife/TML-Database.*

---

*Research synthesis — not financial advice. Real trading involves risk of loss.*
</content>
