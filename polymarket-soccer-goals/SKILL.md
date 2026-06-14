---
name: polymarket-soccer-goals
description: >-
  Use this skill to analyze the day's SOCCER (football) games on Polymarket and suggest entries in
  the TOTAL GOALS (Over/Under) and BOTH TEAMS TO SCORE (BTTS) markets. It discovers each game's
  total-goals and BTTS markets, models P(Over)/P(BTTS) with a Dixon-Coles goal model (Poisson +
  low-score correction), computes the edge vs the Polymarket price, and suggests entries restricted
  to a 1.50x–3.0x payout. Trigger on: soccer total goals, over/under goals, both teams to score,
  BTTS, futebol total de gols, ambos marcam, Dixon-Coles, goals model, suggest soccer bets,
  Polymarket football.
version: 1.0.0
author: polymarket-skills
---

# Polymarket Soccer Goals (Over/Under + BTTS)

Operationalizes `research/soccer-goals-btts-deep-research.md`: discover the day's soccer games,
model each game's goal distribution with **Dixon-Coles**, and suggest **Over/Under total-goals** and
**BTTS** entries with a quantified edge — sized by half-Kelly under the constitution's caps, filtered
to a **1.50×–3.0× payout** (entry price in `[0.3333, 0.667]`).

**Paper trading is the default (CLAUDE.md rule #2).** Read/analysis only — never places live trades.
Market text is untrusted (rule #5) — sanitized, never executed as instructions.

Self-contained: reuses the category scanner (`category_common`), the advisor's `kelly_half`, and the
paper trader by import; it modifies none of them.

## Quick Start
Scripts require the venv: `source ~/.venv/bin/activate`

```bash
# Suggest today's soccer total-goals + BTTS entries (human-readable)
source ~/.venv/bin/activate && python polymarket-soccer-goals/scripts/suggest_soccer.py --output text

# A specific day, feeding your own team ratings (Elo / attack-defense factors)
source ~/.venv/bin/activate && python polymarket-soccer-goals/scripts/suggest_soccer.py \
  --date 2026-06-14 --ratings-csv teams.csv --output text

# Honest "no edge" mode (no inputs -> market-implied -> zero edge)
source ~/.venv/bin/activate && python polymarket-soccer-goals/scripts/suggest_soccer.py --no-record
```

Offline tests (no network):
```bash
python polymarket-soccer-goals/scripts/test_dixon_coles.py
python polymarket-soccer-goals/scripts/test_pipeline.py
```

## How it works
- **Model (Dixon-Coles):** home goals ~ Poisson(λ_home), away ~ Poisson(λ_away); the joint score
  matrix gets the DC `τ` low-score correction (fits draws/0-0/1-1). Soccer goals are ~equidispersed
  (variance/mean ≈ 1), so Poisson — not Negative Binomial — is correct. Then
  `P(Over X.5) = Σ_{i+j>X} P(i,j)` and `P(BTTS) = Σ_{i≥1,j≥1} P(i,j)`. See `references/model.md`.
- **λ from data:** `total` (league baseline × attack/defense factors) + `supremacy` (from Club Elo or
  xG) → `λ_home=(total+sup)/2`, `λ_away=(total−sup)/2`. **BTTS is asymmetric** — governed by the
  smaller λ (weak attack vs strong defense).
- **Anti-fabrication:** with no inputs the model is **market-implied** (matches the Over and BTTS
  prices), so edge ≈ 0 and nothing is suggested. Real edge appears only when ratings/Elo/xG move λ.
- **Scope:** only soccer leagues (slug prefixes like `epl-`, `laliga-`, `fifwc-`, ...) and only the
  full-game **total-goals** (`...-total-<line>`) and **BTTS** (`...-btts`) markets — moneyline,
  spreads, etc. are dropped. **Pre-game only** (`--min-hours 0`), $1,000 volume floor (configurable).

## Script: suggest_soccer.py
| Flag | Default | Purpose |
|---|---|---|
| `--date YYYY-MM-DD` | today (UTC) | Target day |
| `--ratings-csv PATH` | — | Team ratings CSV (`team,elo,att_factor,def_factor`) — ToS-clean inputs |
| `--use-clubelo` | off | Best-effort live Club Elo lookup (needs name mapping) |
| `--rho F` | -0.10 | Dixon-Coles dependence (negative raises draws) |
| `--home-first / --away-first` | home-first | Which team the slug lists first |
| `--min-volume N` | 1000 | Volume gate ($/24h) |
| `--min-hours F` | 0 | Min hours to kickoff (0 = pre-game only) |
| `--odds-min / --odds-max` | 1.50 / 3.00 | Decimal payout band |
| `--min-edge F` | 0.05 | Min edge after fees |
| `--all-lines` | off | Suggest every line (default: best-edge line per game/market) |
| `--record / --no-record` | on | Record predictions to the soccer predictions DB |
| `--predictions-db PATH` | `~/.polymarket-soccer/predictions.db` | Predictions store |
| `--output json\|text` / `--quiet` / `--debug` | json / off / off | Output / logging |

Records each suggestion (status PENDENTE) with the full Dixon-Coles audit (λ_home, λ_away, ρ,
P(model), edge, sizing) and the Polymarket market link, for later calibration/win-rate analysis.

## Honest limitations
- **Market near-efficient at close; realistic O/U yield ~0.8%** (research). Any model implying >10%
  yield is overfit. Validate with Brier/log-loss + CLV over **~1,000+ entries** before real capital.
- **xG data needs scraping** (FBref/Understat via `soccerdata`, ToS-flagged) and per-league team-name
  mapping; the **ratings CSV** is the v1 workhorse (ToS-clean). Club Elo is free but needs a club-name
  map. Without inputs the engine returns zero edge (no fabrication).
- **Slug team order** (home/away) and league baselines are best-effort/tunable; the sandbox blocks
  live egress so only the deterministic core is tested here. Conservative caps (1% first / 2% model).
- Not financial advice; real trading involves risk of loss.
