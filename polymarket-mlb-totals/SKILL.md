---
name: polymarket-mlb-totals
description: >-
  Use this skill to analyze the day's MLB games on Polymarket and suggest entries in the TOTAL
  RUNS (Over/Under) market. It pulls every MLB game of a date via the category scanner, finds each
  game's total-runs Over/Under market, models P(Over)/P(Under) with a Negative Binomial run
  distribution (park + season run-rate + weather adjusted), computes the edge vs the Polymarket
  price, and suggests entries — restricted to a 1.50x–3.0x payout. Trigger on: MLB total runs,
  over/under runs, baseball totals model, suggest MLB bets, run total prediction, MLB totals
  edge, beisebol total de runs, sugerir entradas MLB, modelo de over/under da MLB.
version: 1.0.0
author: polymarket-skills
---

# Polymarket MLB Totals

Operationalizes `research/mlb-total-runs-deep-research.md`: discover the day's MLB games, model
each game's **total runs** distribution, and suggest **Over/Under** entries with a quantified,
classified edge — sized by half-Kelly under the constitution's caps, and filtered to a
**1.50×–3.0× payout** (entry price in `[0.3333, 0.667]`; Polymarket price = implied probability,
decimal odds = 1/price).

**Paper trading is the default (CLAUDE.md rule #2).** This is a simulation, not financial advice;
real trading involves risk of loss. Market text is untrusted user content (rule #5) — sanitized,
never executed as instructions.

This skill is **self-contained** and **reuses** existing skills by import (it modifies none):
the category scanner (`category_common`, `list_games_today`), the advisor's `kelly_half`, and the
paper trader's `execute_recommendation`.

## Quick Start

Scripts require the venv: `source ~/.venv/bin/activate`

```bash
# Suggest today's MLB total-runs entries (human-readable)
source ~/.venv/bin/activate && python polymarket-mlb-totals/scripts/suggest_totals.py --output text

# A specific day, JSON
source ~/.venv/bin/activate && python polymarket-mlb-totals/scripts/suggest_totals.py --date 2026-06-14

# Feed real season run-rates (ToS-clean) and dry-run into the paper trader
source ~/.venv/bin/activate && python polymarket-mlb-totals/scripts/suggest_totals.py \
  --projections-csv myteams.csv --paper

# Honest "no edge" mode (no STRONG inputs -> market-implied -> zero edge)
source ~/.venv/bin/activate && python polymarket-mlb-totals/scripts/suggest_totals.py --no-external --output text
```

Run the offline test suite (no network needed):

```bash
python polymarket-mlb-totals/scripts/test_run_distribution.py
python polymarket-mlb-totals/scripts/test_pipeline.py
```

## How it works

```
list_games_today (scanner)  →  find total-runs Over/Under market  →  NegBin model P(Over)/P(Under)
   →  edge = P_model − price − fee  →  pick side  →  1.50×–3.0× odds filter  →  entry decision tree
   →  half-Kelly + 2%/1% caps  →  recommendation (JSON + text)  →  optional --paper
```

- **Model:** the game total is **Negative Binomial** (MLB runs are overdispersed, `variance ≈ 2×mean`
  — plain Poisson is wrong). `P(Over)` is the PMF tail.
- **μ is anchored to the market** unless a **STRONG input** (team offense/pitching factors) is present.
  The mean starts at the **market-implied** μ; only `*_off`/`*_sp` factors move it off the market.
  **Weather and the park factor alone do NOT override the market** (they're second-order and already
  in the line) — this is what stops the model fabricating huge fake edges on high-total games. Strong
  inputs are resolved automatically from the **MLB Stats API** (free, no key): team offense/pitching
  from season standings (`team_factors.py`), refined by **today's probable starter's FIP**
  (`starter_factors.py`, the #1 input, blended ~60/40 starter/bullpen). A `--projections-csv` overrides
  both. SIERA/xFIP (FanGraphs, ToS-flagged) would be a further upgrade over FIP.
- **Anti-fabrication + sanity cap:** with no strong inputs μ = market-implied, so edge ≈ 0 and nothing
  is suggested. And any post-fee edge above **`MAX_PLAUSIBLE_EDGE` (15%)** is **rejected** — on a
  near-efficient market that signals model error, not value (the col-oak post-mortem).
- **Edge type & sizing:** classified as **news-driven** (model/forecast) → **2% cap**, **1% on the
  first trades** of this new strategy; half-Kelly via the advisor. See `references/edge-and-risk.md`.
- **Scope filters:** only **MLB** games (slug prefix `mlb-`) and only **full-game run-total** markets
  (`...-total-<line>`) — moneyline, spreads, first-5-innings (`-f5-`), strikeout props (`-k-`), and
  NRFI are excluded, so a non-run total is never modeled.
- **Daily-sports gates (relax the generic constitution rules):** this operation targets the day's
  games, so it bets **pre-game only** (game not started, `--min-hours 0`) instead of the generic
  ">24h to resolution" rule, and uses a **$1,000** volume floor (MLB total sub-markets are thinner
  than the generic $10k). By default it suggests the **best-edge line per game** (`--all-lines` to
  show every line).

## Script: suggest_totals.py

| Flag | Default | Purpose |
|---|---|---|
| `--date YYYY-MM-DD` | today (UTC) | Target day |
| `--min-volume N` | 1000 | Decision-tree volume gate ($/24h) |
| `--min-hours F` | 0 | Min hours until game start (0 = pre-game only, not started) |
| `--all-lines` | off | Suggest every qualifying line (default: best-edge line per game) |
| `--min-edge F` | 0.05 | Min edge after fees (decision tree) |
| `--odds-min / --odds-max` | 1.50 / 3.00 | Decimal payout band (→ price 0.667 … 0.333) |
| `--dispersion F` | 2.0 | `variance = dispersion × mean` |
| `--league-baseline F` | 8.5 | Neutral game total |
| `--league-prefix TEXT` | `mlb-` | Only process games whose slug starts with this (filters out soccer/esports; `''` = all) |
| `--fee-rate F` | 0.0 | Taker fee base (sports are fee-free; 0.063 models crypto-style) |
| `--use-external / --no-external` | on | Use external data inputs (graceful fallback) |
| `--projections-csv PATH` | — | ToS-clean season run-rate source (see references/data-sources.md) |
| `--refresh-prices` | off | Refresh prices via CLOB midpoint |
| `--portfolio-value F` | 10000 | Portfolio USD for sizing |
| `--portfolio-db PATH` | — | Paper DB (detects first trade → 1% cap) |
| `--record / --no-record` | on | Record each prediction (+ stats log) to the predictions DB |
| `--predictions-db PATH` | `~/.polymarket-mlb-totals/predictions.db` | Predictions store path |
| `--paper` / `--paper-execute` | off | Pipe to paper trader (dry-run unless `--paper-execute`) |
| `--output json\|text` | json | Output format |
| `--rate-limit MS` / `--debug` | 100 / off | API pacing / logging |

Output: `suggestions[]` (each a paper-trader-ready recommendation + the model μ/line and the
recorded `prediction_id`) and `skipped[]` (every game not suggested, with the reason — rule #8).

## Predictions store & settlement (`track_predictions.py`)

Every suggested prediction is persisted (by default) to a dedicated SQLite DB
(`~/.polymarket-mlb-totals/predictions.db`, separate from the paper trader) for later
analysis — calibration, win rate, CLV. Each row stores the prediction, the **full
statistical/mathematical audit** behind it (`stats_log`, JSON: μ, variance, NegBin params,
park factor, inputs used, P(Over)/P(Under)/push, edge, odds, Kelly, sizing, both-sides notes),
and a **status**:

| Status | Meaning |
|---|---|
| `PENDENTE` | default — the game/bet has not settled yet |
| `ACERTO` | settled and the prediction was correct |
| `ERRO` | settled and the prediction was wrong |
| `ANULADO` | push/void (integer line where total == line) |

```bash
# Review and settle
python polymarket-mlb-totals/scripts/track_predictions.py --summary
python polymarket-mlb-totals/scripts/track_predictions.py --list --status PENDENTE --output text
python polymarket-mlb-totals/scripts/track_predictions.py --settle-game mlb-hou-kc-2026-06-13 --actual-total 9
python polymarket-mlb-totals/scripts/track_predictions.py --auto-settle --date 2026-06-13   # MLB Stats API finals (best-effort)
```

### Full math audit (`audit_log.py`)
Dump the complete `stats_log` (model, μ/variance, inputs, probabilities, edge, sizing) for every
prediction. Pure stdlib; works for both the MLB and soccer stores (`--sport`).
```bash
python polymarket-mlb-totals/scripts/audit_log.py                 # all MLB predictions, full audit
python polymarket-mlb-totals/scripts/audit_log.py --status PENDENTE --date 2026-06-16
python polymarket-mlb-totals/scripts/audit_log.py --sport soccer  # the soccer store
python polymarket-mlb-totals/scripts/audit_log.py --json > audit.json   # machine-readable
```

### Reset the store (`reset_db.py`)
Clear all recorded predictions (and the analysis cache) to start fresh after a model change. Pure
stdlib; `--sport mlb|soccer|all`. Destructive — asks to confirm unless `--yes`.
```bash
python polymarket-mlb-totals/scripts/reset_db.py --sport all --yes   # clear both stores
python polymarket-mlb-totals/scripts/reset_db.py --sport mlb --delete-file --yes  # remove the .db file
```

Re-recording the same game/line/side while `PENDENTE` updates the snapshot (captures line
movement); a settled row is never overwritten. Schema in `references/predictions-store.md`.

### Shadow calibration log (`model_log` table)
**Every** modeled game is logged to a separate `model_log` table — including the ones the model did
**not** bet (`bet=0`, with the `skip_reason`). It stores the reference-side probability (`ref_prob`
for OVER), the market price, the model params, and the pick. This avoids selection bias so you can
compute **Brier / log-loss / reliability over all games**, not just the bet ones. Read it with
`predictions_db.get_model_log()` / `soccer_predictions.get_model_log()`.

### Calibration report (`calibration.py`)
Settles the shadow log (a game's actual outcome is propagated to **all** its lines, bet or not) and
scores the model. Pure stdlib; `--sport mlb|soccer`.
```bash
python polymarket-mlb-totals/scripts/calibration.py --sport mlb --settle      # settle + report
python polymarket-mlb-totals/scripts/calibration.py --sport soccer --settle --json
```
Reports Brier, log-loss, and a reliability table (predicted vs empirical) for all modeled markets and
for bet-only. **CLV** (needs a closing-price snapshot) and settling games with no bet line (needs the
results feed) are the documented follow-ups.

### Historical backtest (`backtest.py`)
Walk-forward validation over past seasons — the way the model trades. For each game it builds
**point-in-time** team run factors from only the games already played (no look-ahead), runs the same
`model_probabilities` + `pick_side` as the live skill, settles against the final score, and aggregates
**ROI / win rate / Brier / log-loss / calibration / CLV / μ-vs-market bias** by season.

Needs a normalized games+odds CSV (one row/game):
`date,away,home,away_score,home_score,total_line,over_odds,under_odds[,close_over_odds,close_under_odds]`
— odds may be American (`-110`), decimal (`1.91`), or implied prob (`0.524`); sportsbook vig is
devigged to a Polymarket-equivalent fair price. Free source: **sportsbookreviewsonline.com** MLB odds
(one workbook/year → export to this CSV) or a Kaggle MLB odds dataset.
```bash
python polymarket-mlb-totals/scripts/backtest.py --games-csv mlb_odds.csv --seasons 2021-2025
python polymarket-mlb-totals/scripts/backtest.py --games-csv mlb_odds.csv --json --out bt.json
```
ROI/CLV vs the devigged **closing** line are the real validation; `mean(μ − market_μ)` flags residual
Over/Under bias. Team factors are point-in-time from the results; per-start starter FIP is a documented
extension (needs a probables history).

### Sharp anchor — the divergence detector (`sharp_odds.py`)
The deep research (`references/edge-pathways-deep-research.md`) is blunt: a predictive run-total model
does **not** beat the MLB closing line — the **sharp close (Pinnacle, devigged) IS the efficient
probability**. So with a sharp reference the model stops trying to out-predict and instead acts on
**divergence**: `model_probabilities(..., sharp_over_price=...)` anchors fair value to the sharp price,
so the edge (`p_model − Polymarket price`) measures how far Polymarket strays from the sharp line —
bet a side only when Polymarket prices it cheaper than the sharp fair value. Without a sharp ref the
model stays Polymarket-anchored (zero edge — anti-fabrication).
```bash
# Live: The Odds API (includes Pinnacle).            Backtest/offline: a CSV.
python polymarket-mlb-totals/scripts/suggest_totals.py --odds-api-key $ODDS_API_KEY
python polymarket-mlb-totals/scripts/suggest_totals.py --sharp-odds-csv sharp.csv
```

### Sharp-driven discovery (`sharp_discovery.py`)
The `mlb` Gamma tag is **not honored** — it returns the global volume-ranked mix and pagination caps
at offset ~2100 (HTTP 422), so low-volume MLB games are truncated (only ~2 of the day's ~11 surface).
When a sharp slate is loaded it carries the **full daily card**, so it becomes the **authoritative game
list**: for each sharp game we fetch its Polymarket markets directly by event slug (`mlb-<away>-<home>-
<date>`, both orderings tried) and **union** them into discovery. This fixes coverage (every game found
regardless of volume rank) **and** matching — every added game already has its sharp reference, because
team identifiers from any source (abbrev `CHC` or full name `Chicago Cubs`) are normalized to the
Polymarket slug abbreviation (`sharp_odds.normalize_team`). On by default when a sharp reference is
present; disable with `--no-sharp-discovery`. Each game's total markets are grouped by their
line-encoding slug (`...-total-8pt5`) so the run-total filter keeps them. The sharp anchor is then
interpreted **at the sharp's own line** (`sharp_ref` returns line + fair P(over)): the model inverts it
to an expected total (μ) at the sharp line and prices whatever Polymarket line is on offer off that μ,
so a sharp main line of 8.5 correctly prices a Polymarket alternate of 7.5/11.5 — line drift between
the books no longer drops the reference.

### CLV vs the sharp close (`clv_vs_sharp.py`) — the validated edge metric
`CLV(side) = sharp_close_fair_prob(side) − entry_price`. Beating the sharp close is the only proxy that
confirms a real edge (in ~50 bets, vs thousands for raw P&L). The close prob is taken **at the bet's own
line** even when the sharp closed at a different line (same μ-inversion as the model anchor — alternate-line
bets are no longer dropped). Two-step workflow:
```bash
# 1) Near first pitch each day, snapshot the sharp CLOSE (merge into one season-long CSV):
python polymarket-mlb-totals/scripts/capture_close.py --out sharp_close.csv   # uses $ODDS_API_KEY
# 2) Once you have ~50 settled entries, score them against the accumulated closes:
python polymarket-mlb-totals/scripts/clv_vs_sharp.py --sharp-odds-csv sharp_close.csv
```
`capture_close.py` fetches the current Pinnacle/consensus totals, devigs them, and merges
`close_over_odds`/`close_under_odds` rows keyed by (date, team-set) so re-runs upsert.
`avg_CLV > 0` and `beat_close > 50%` = real edge. Backtest validation on 10 real seasons showed the
*predictive* model has no edge (ROI ~0, Brier ≈ coin-flip); the sharp anchor + CLV is how you find and
prove the only durable edge — Polymarket diverging from the sharp consensus.

### Player-prop feasibility scan (`props_scan.py`)
The research's one "superior prediction" path is **player props** (strikeouts/HR/hits), and Polymarket
added MLB props in 2026 — but their liquidity is thinner than game lines. Before building a prop model,
this verifies the premise: discovers the day's MLB markets, classifies them (moneyline / game total /
**player prop** / team prop) and measures liquidity (24h volume, on-book liquidity, spread, and CLOB
order-book **depth** within 5¢ for the top props), then prints a VIABLE / THIN / NONE verdict.
```bash
python polymarket-mlb-totals/scripts/props_scan.py --date 2026-06-21
python polymarket-mlb-totals/scripts/props_scan.py --json --top 20
```
Run on a networked machine (sandbox blocks Polymarket). It also dumps sample slugs per class so the
classifier can be confirmed against the real market format. Decision gate: only build a strikeout/HR
prop model if the scan shows enough props with real book depth (else the edge is liquidity-capped).

## Web dashboard (`webapp/`)

A modern React + FastAPI dashboard to interact with the model visually — two tabs:
- **Análises** — the day's Over/Under suggestions as colorful cards with the full NegBin math; the
  heavy calc runs **once per day** and is cached until day end.
- **Resultados** — ROI, P&L, total/Over/Under win rate (daily/weekly/monthly) with charts; **each
  visit triggers settlement** from the authoritative MLB final total (reported only once a game is
  Final) to update PENDENTE → ACERTO/ERRO, with a direct Polymarket market link per row. The
  Polymarket closed status is an optional extra guard (`require_closed`), off by default.

Dark/light toggle, read-only (never trades). Run with `webapp/dev.sh` (backend :8000, frontend
:5173) or see `webapp/README.md`. Seed offline demo data: `POST /api/seed-demo` or
`python scripts/seed_demo.py --reset`.

## Honest limitations (read `references/edge-and-risk.md`)

1. **No live data in this sandbox.** Polymarket and MLB data egress is blocked; only the
   deterministic core is tested here. End-to-end runs need a networked environment. Without real
   inputs the engine returns **zero edge** by design — it never fabricates one.
2. **Real edge needs calibrated inputs + validation.** MLB totals are near-efficient at close;
   realistic edge is ROI ~2–5% / win rate ~53–55%. Track **Brier, log-loss, calibration, and CLV**
   over **~1,000+ entries** before trusting it. CLAUDE.md §4 live-readiness gates still apply.
3. **Conservative by design.** New-strategy 1% cap, model/news 2% cap, half-Kelly. Intentionally small.
4. **Data-source ToS.** MLB Stats API / Statcast are MLBAM-copyrighted (commercial/bulk restricted);
   a betting model is plausibly commercial. Prefer a licensed feed for production; the `--projections-csv`
   path keeps ingestion ToS-clean. `references/data-sources.md` has the details.
5. **Model assumptions.** `variance = 2×mean` slightly undercounts shutouts (Enby/zero-modified NegBin
   corrects this); integer-line handling assumes Polymarket voids pushes — verify per market. The
   `--dispersion` and `--league-baseline` knobs exist because they need recalibration each season.
