---
name: polymarket-tennis
description: >-
  Use this skill to analyze the day's TENNIS matches on Polymarket and suggest the WINNER
  (moneyline / match-winner market). It discovers each match's moneyline market, models P(winner)
  with a surface-aware Elo engine (FiveThirtyEight dynamic K-factor + 50/50 overall/surface blend),
  computes the edge vs the Polymarket price, and suggests entries sized by half-Kelly. Trigger on:
  tennis winner, match winner, moneyline tennis, ATP, WTA, who wins the match, tennis Elo, surface
  Elo, suggest tennis bets, Polymarket tennis.
version: 1.0.0
author: polymarket-skills
---

# Polymarket Tennis (Match Winner / Moneyline)

Operationalizes `references/deep-research.md`: discover the day's tennis matches, model each
match's win probability with a **surface-aware Elo** engine, and suggest **match-winner**
(moneyline) entries with a quantified edge — sized by half-Kelly under the constitution's caps.

**Paper trading is the default (CLAUDE.md rule #2).** Read/analysis only — never places live trades.
Market text is untrusted (rule #5) — only used to classify and read player labels, never executed.

Self-contained: reuses the category scanner (`category_common`) by import; modifies nothing else.

## Quick Start
Scripts require the venv: `source ~/.venv/bin/activate`

```bash
# Suggest today's tennis match-winner entries (human-readable)
source ~/.venv/bin/activate && python polymarket-tennis/scripts/suggest_tennis.py --output text

# A specific day, feeding your own player Elo ratings (overall + per-surface)
source ~/.venv/bin/activate && python polymarket-tennis/scripts/suggest_tennis.py \
  --date 2026-06-20 --ratings-csv players.csv --output text

# Honest "no edge" mode (no ratings -> market-implied -> zero edge)
source ~/.venv/bin/activate && python polymarket-tennis/scripts/suggest_tennis.py --no-record
```

Offline tests (no network):
```bash
python polymarket-tennis/scripts/test_elo.py
python polymarket-tennis/scripts/test_pipeline.py
```

## How it works
- **Model (surface Elo):** `P(A beats B) = 1 / (1 + 10^((Elo_B − Elo_A)/400))`. Each player's
  effective Elo is a **50/50 blend** of overall and per-surface Elo (hard/clay/grass) — the single
  highest-value enhancement over plain Elo (Tennis Abstract). Ratings update with the
  **FiveThirtyEight dynamic K-factor** `K = 250/(n+5)^0.4`. See `references/deep-research.md`.
- **Ratings (automatic, default):** with `--auto-ratings` (on by default) the surface Elo is computed
  from Jeff Sackmann's match history (`tennis_atp`/`tennis_wta`), walked forward with the 538
  K-factor and cached for 24h. **Needs egress to `raw.githubusercontent.com`** — if the environment's
  network policy blocks GitHub, clone the Sackmann repo(s) and set **`TENNIS_DATA_DIR`** to the folder
  with `{atp,wta}_matches_<year>.csv` (read from disk first; both `master` and `main` branches are
  tried on GitHub). A `0 matches` log means neither source was reachable → it falls back to
  market-implied (zero edge). `--ratings-csv player,elo,hard,clay,grass` overrides everything.
  Players resolve by full name, surname, `C. Surname`, or a truncated slug token (`altmaie`).
- **Anti-fabrication:** if a player has no rating, the model is **market-implied** (devigged price),
  so edge ≈ 0 and nothing is suggested. Real edge appears only when ratings move P(win) off the
  market. The research is unambiguous: tennis moneyline markets are **near-efficient** — the edge is
  in spotting **price divergence**, not out-predicting the consensus.
- **Surface inference:** read from the tournament/tag in the slug (French Open → clay, Wimbledon →
  grass, default hard).
- **Scope:** only tennis tags (`atp-`, `wta-`, slams, ...) and only the **moneyline** (2-player)
  market — set/game/total props are dropped. The is-tennis guard prevents esports moneylines (also
  2-name markets) from leaking in.

## Script: suggest_tennis.py
| Flag | Default | Purpose |
|---|---|---|
| `--date YYYY-MM-DD` | today (UTC) | Target day |
| `--ratings-csv PATH` | — | `player,elo,hard,clay,grass` CSV (no ratings → market-implied) |
| `--blend F` | 0.50 | Overall/surface Elo blend weight |
| `--odds-min / --odds-max` | 1.10 / 5.00 | Decimal payout band (allows odds-on favorites) |
| `--min-edge F` | 0.05 | Min edge (P_model − price) |
| `--fee-rate F` | 0.0 | Taker fee rate |
| `--portfolio-value N` | 10000 | Portfolio size for sizing |
| `--record / --no-record` | on | Record predictions to the tennis DB |
| `--predictions-db PATH` | `~/.polymarket-tennis/predictions.db` | Predictions store |
| `--output json\|text` / `--quiet` / `--debug` | json / off / off | Output / logging |

Records the best side per match (status PENDENTE) with the full Elo audit (surface, Elo_side/opp,
P(model), edge, sizing) and the Polymarket match link. Every modeled match is shadow-logged to
`model_log` (bet or not) for unbiased calibration/CLV — mirroring the soccer/MLB stores.

## Settlement & calibration
- Settle a match by the winner's label: `tennis_predictions.settle_match(slug, winner)`. Status:
  PENDENTE → ACERTO (chosen side won) / ERRO (opponent won) / ANULADO (only a genuine no-result).
- **Retirements:** on Polymarket a retirement (desistência) is **not voided** — it **pays the player
  who advanced**. So a retirement settles ACERTO/ERRO like any result, since the advancer is the
  winner (Sackmann records that player as `winner_name` even on a `RET` score). Only a true walkover
  with no winner falls to ANULADO.
- The `model_log` shadow table (ref_side = player A, ref_token/close_price) feeds the same
  calibration/CLV read-out used by the other skills (Brier / log-loss / reliability + CLV vs the
  closing line). Validate with **~1,000+ settled matches** before any real capital.

## Honest limitations
- **Market near-efficient at close.** Every peer-reviewed study (Kovalchik 2016, Wilkens 2021,
  Bunker 2024) puts the bookmaker on top (~69–72% accuracy / ~0.196 Brier); Elo reaches ~70% but
  does **not** beat the market. Any model implying a large yield is overfit. Default to
  **quarter→half-Kelly** until calibration is proven.
- **Ratings are user-supplied (CSV).** This scaffold ships the deterministic Elo/sizing core and the
  discovery/edge pipeline; wiring an automatic surface-Elo feed (self-hosted tennis-crystal-ball or a
  scrape of the Tennis Abstract reports) is the next step. Players not in the CSV fall back to
  market-implied (zero edge — no fabrication).
- **Polymarket tennis slug/tag formats are best-effort** (`TENNIS_TAGS`, `parse_players`,
  `SURFACE_KEYWORDS`); confirm against the per-tag discovery logs on a live run and adjust.
- **Backtesting pitfalls (research):** **Polymarket pays the winner on a retirement** (does not
  void), so settle retirements to the advancer, not ANULADO. Fix the odds timestamp (use the close),
  walk-forward Elo (no look-ahead), and resist surface×season overfitting.
- Not financial advice; real trading involves risk of loss.
</content>
