# Predictions store

A dedicated SQLite database (default `~/.polymarket-mlb-totals/predictions.db`) that records
every prediction the model makes, the statistical/mathematical audit behind it, and the
settlement status. It is **separate** from the paper trader's portfolio DB — this skill never
modifies that schema. Implemented in `scripts/predictions_db.py`; reviewed/settled via
`scripts/track_predictions.py`.

## Why
For later analysis: win rate by ACERTO/ERRO, calibration (Brier/log-loss using `model_prob` vs
outcome), and CLV (using `entry_price` vs the closing price). The constitution's validation gates
(research §5) need this history over ~1,000+ predictions before any real capital.

## Table `predictions`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `created_at`, `updated_at` | TEXT | UTC ISO |
| `game_slug` | TEXT | e.g. `mlb-hou-kc-2026-06-13` |
| `game_date` | TEXT | YYYY-MM-DD |
| `market_question` | TEXT | sanitized (untrusted market text) |
| `condition_id`, `token_id` | TEXT | Polymarket identifiers (token = the bet side) |
| `line` | REAL | total-runs line (e.g. 8.5) |
| `side` | TEXT | `OVER` / `UNDER` |
| `entry_price` | REAL | Polymarket price at prediction (= implied prob) |
| `decimal_odds` | REAL | 1 / entry_price |
| `model_prob` | REAL | model P(chosen side) |
| `edge` | REAL | after fees |
| `mu`, `variance`, `dispersion` | REAL | distribution moments used |
| `park_factor` | REAL | home-park run index |
| `confidence`, `size_pct`, `size_usd`, `kelly_fraction` | REAL | sizing |
| `used_external` | INTEGER | 1 if real inputs moved μ off the market |
| `fee_rate`, `strategy` | | |
| `stats_log` | TEXT | **JSON audit**: model, μ, variance, NegBin (r,p), league_baseline, park_factor, the `inputs` used (offense/pitching/weather/home_field), line/need, P(Over)/P(Under)/push, decimal odds, fee, edge, book_sum/price_sane, Kelly/cap/size, confidence, and per-side notes |
| `status` | TEXT | `PENDENTE` (default) / `ACERTO` / `ERRO` / `ANULADO` |
| `actual_total` | REAL | final game total (set on settlement) |
| `settled_at` | TEXT | UTC ISO (set on settlement) |

`UNIQUE(game_slug, line, side)` — one row per game/line/side. Re-recording while `PENDENTE`
upserts the latest snapshot (captures line movement); a settled row is immutable.

## Status lifecycle

`PENDENTE` on record → on settlement `compute_status(side, line, actual_total)`:
- OVER: ACERTO if `actual > line`, ERRO if `actual < line`.
- UNDER: ACERTO if `actual < line`, ERRO if `actual > line`.
- `actual == line` (only on an integer line): `ANULADO` (push/void).

## API (`predictions_db.py`)
- `record_prediction(pred, db_path)` → id (upsert while pending)
- `settle_prediction(id, actual_total, db_path)` / `settle_game(slug, actual_total, db_path)`
- `get_predictions(db_path, status=None, game_date=None)`
- `summary(db_path)` → counts + win rate
- `compute_status(side, line, actual_total)`

Settlement is manual (`--settle-game` / `--settle-id`) or best-effort `--auto-settle` from the
MLB Stats API final linescores (network; no-op when egress is blocked).
