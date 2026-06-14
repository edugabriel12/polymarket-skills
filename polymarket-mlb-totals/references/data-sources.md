# Data sources & adapters

Implemented in `scripts/data_inputs.py`. Every layer is **best-effort** and degrades
independently; if all fail, the model falls back to the market-implied mean (zero edge).

> **Sandbox note:** egress to MLB/Statcast/weather hosts is blocked in this environment, so the
> network layers return empty here and only the **projections-CSV** path (local file IO) and the
> deterministic core are exercised offline. End-to-end runs need a networked environment.

## Layers

### 1. MLB Stats API — schedule & probable pitchers (free, no auth)
`GET https://statsapi.mlb.com/api/v1/schedule?sportId=1&date=YYYY-MM-DD&hydrate=probablePitcher,team`
Matches the Polymarket game to the real game and identifies home/away + probable starters.
**ToS:** content is MLB Advanced Media copyright — individual/non-commercial/non-bulk only;
a betting model is plausibly "commercial." Prefer a licensed feed for production.

### 2. Projections / season retrospect — via `--projections-csv` (ToS-clean)
Team season run-rates feed `home_off/away_off` (offense) and `home_sp/away_sp` (starter+bullpen
run-suppression). Supplying your own exported CSV is the clean route (avoids scraping FanGraphs,
whose ToS forbids automated access).

**CSV schema** (header row; case-insensitive; team key = `team` or `abbr`, lowercase MLB abbrev):

```
team,off_factor,pitch_factor      # factors relative to league (1.0 = average)
kc,1.08,0.96
hou,1.03,1.01
```
or raw rates (converted with `LEAGUE_RPG = 4.25`):
```
team,rs_per_game,ra_per_game
col,5.10,4.80
```
`off_factor = team runs scored / league avg`; `pitch_factor = team runs allowed / league avg`.
Unknown teams are skipped (that side stays neutral).

### 3. Weather — `api.weather.gov` (US parks, no key)
`GET https://api.weather.gov/points/{lat},{lon}` → hourly forecast → temperature (°F) and wind
(mph), fed to `adjust_mu` (`temp_f`, `wind_out_mph`). Only a `User-Agent` is required. Park
coordinates are in `scripts/ballparks.py` (Toronto/Rogers Centre is outside NWS coverage — use a
global API there). Wind **direction** relative to park orientation is not modeled, so wind is
applied at modest weight.

### 4. Home/away
Home team is the second abbreviation in the slug (`mlb-<away>-<home>-DATE`). The home park's run
factor comes from `scripts/ballparks.py` (a baked-in table, override with live Statcast park
factors when available) and a small `home_field` run delta is added when a real game is matched.

## Park factors (`scripts/ballparks.py`)

A static **runs index** (100 = average) plus stadium coordinates per team. Values are approximate
multi-year estimates (Coors ~118, Great American ~109, Fenway/Yankee elevated; Oracle/T-Mobile/
Petco suppressed) and are **meant to be overridden** by live Statcast park factors
(`baseballsavant.mlb.com/leaderboard/statcast-park-factors`). Recalibrate per season.

## What is NOT fetched (by design)
Short-term recent form and head-to-head team records — both are noise (see `run-model.md`). The
predictive matchup (lineup vs opposing starter, handedness/platoon) is captured via the offense/
pitching factors, not via team-vs-team history.

## Production recommendation
For live/commercial use, source projections + park factors through a **licensed** provider
(e.g. Sportradar, SportsDataIO) and store the **closing** Polymarket price for CLV evaluation.
