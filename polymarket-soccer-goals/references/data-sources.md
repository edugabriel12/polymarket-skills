# Data sources & adapters (soccer)

Implemented in `scripts/data_inputs.py`. Each layer is best-effort and degrades independently; if
nothing resolves, the model falls back to market-implied λ (zero edge). The model only needs, per
match, an **expected total goals** and a **home supremacy** — everything below feeds those two.

> Sandbox note: live egress is blocked here, so the network layers return empty and only the
> **ratings CSV** path (local file IO) and the deterministic engine are exercised offline.

## 0. Automatic resolution order (`data_inputs.get_match_inputs`)
Strength is resolved automatically per match: **ratings CSV** (if given) → **xG** (soccerdata) →
**Elo** (national-team Elo for international games, Club Elo for club leagues). First hit wins; if
none covers the match → market-implied fallback (zero edge). The static tables live in
`ratings_sources.py` (`NATIONAL_ELO` snapshot + country aliases; `CLUB_ELO_ALIASES`).

## 1. Ratings CSV (`--ratings-csv`, ToS-clean, overrides the auto sources)
Supply your own per-team ratings. Columns (case-insensitive; team key = `team`/`abbr`):
```
team,elo,att_factor,def_factor
ars,1850,1.10,0.95
che,1700,1.02,1.05
```
- `elo` → home supremacy via `supremacy_from_elo`.
- `att_factor`/`def_factor` (1.0 = league average) → scale the league baseline total via `adjust_total`.
Use either or both. This is the cleanest, fully-offline path.

## 2. Club Elo (`--use-clubelo`, free, no key)
`GET http://api.clubelo.com/<ClubName>` returns the club's daily Elo history (CSV). Free for personal
use. **Caveat:** Club Elo keys clubs by full name (e.g. `ManCity`), which rarely matches a Polymarket
abbreviation — provide the `elo` column in the ratings CSV for reliable values, or extend the lookup
with a club-name alias map. Best-effort; failures degrade to the CSV / market-implied path.

## 3. xG via `soccerdata` (optional, ToS-flagged)
`scripts/data_inputs.fetch_xg` lazily imports `soccerdata` (FBref/Understat xG). Returns `{}` when the
library or data is unavailable, so the pipeline still runs on Elo/ratings. A full integration needs a
per-league team-name mapping and respects FBref/Understat scraping ToS (rate-limited; gray-area for
commercial use). xG (rolling xG-for/against) is the most predictive input when available — total from
the sum, supremacy from the difference.

## League baselines & slug parsing (`scripts/leagues.py`)
- `LEAGUE_BASELINES`: average total goals/game per league (tunable). `NEUTRAL_PREFIXES`: competitions
  with no home advantage (World Cup, Euro). `SOCCER_PREFIXES`: what discovery treats as soccer.
- `parse_teams(slug, home_first=True)`: the two team tokens after the league prefix; **home/away order
  is assumed and configurable** (`--home-first/--away-first`) — Polymarket's convention is not
  guaranteed, and it only matters via home advantage (which is 0 for neutral competitions).

## Recommended free stack
1. **Ratings:** maintain a `--ratings-csv` (Elo and/or attack/defense factors) — the reliable input.
2. **Club Elo** for live strength once a club-name map exists.
3. **xG via soccerdata** as an enhancement (ToS-aware, cached).
4. Historical odds for backtesting: football-data.co.uk (O/U 2.5 closing) — note it has **no BTTS
   column** (derive the BTTS result from goals, or use The Odds API for BTTS odds).

## ToS / production
FBref/Understat scraping and Club Elo are personal-use friendly but commercial use is restricted;
prefer a licensed feed for production. The ratings-CSV path keeps ingestion ToS-clean.
