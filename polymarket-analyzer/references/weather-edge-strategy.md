# Weather Edge Strategy

How the weather edge bot identifies, sizes, and exits trades on Polymarket weather markets, and how the AI judge filters those signals before execution.

## TL;DR

1. **Discover** weather markets resolving in the next 48h with volume ≥ $100.
2. **Compute** P(YES) from the OpenWeather forecast for the city + threshold.
3. **Find edge**: `edge_pp = P(forecast) - P(implied)`. Take the side with edge ≥ 10pp.
4. **Filter price band**: chosen side's price in [0.05, 0.95] (only excludes near-zero and near-one extremes; the asymmetric tail brackets are exactly where edges are biggest).
5. **Size by liquidity**: walk the orderbook for max size at ≤ 20% slippage.
6. **AI judge** (Claude Sonnet 4.6) cross-checks with NWS / Visual Crossing / web before approving.
7. **Monitor** approved positions. Cash out only if `forecast_prob_now < entry_implied` AND `best_bid >= entry_price` (break-even or profit). Otherwise, hold.
8. **Persist** every decision to SQLite for retrospective analysis.

## Edge formula

For a parsed market `(city, threshold, comparison, target_date)` and OpenWeather forecast:

### Temperature thresholds
```
z = (forecast_high_or_low - threshold) / MAE_TEMP
P(YES) = norm.cdf(z)   # for "exceed" / "at_least"
P(YES) = norm.cdf(-z)  # for "below" / "at_most"
```
Default `MAE_TEMP = 5°F` (typical 3-5 day OpenWeather error). Tune via the analyzer's calibration output.

**v7: Dynamic MAE per (city, target_date).** The bot stores every
forecast snapshot in `forecast_history` table during discovery. When
computing P(YES), it queries the last 5 snapshots for the same city +
target date and uses `mae_dynamic = max(MAE_static, std_dev × 1.5)`.
This means cities with volatile forecasts (Lucknow, Hong Kong) get
larger MAE → more conservative probabilities → fewer false-edge trades.
Falls back to static MAE when fewer than 2 history samples exist.

**v7: Multi-source consensus** (CLI `--multi-source`, auto-ON if
`VISUAL_CROSSING_API_KEY` is set). Discovery fetches Visual Crossing
alongside OpenWeather, persists both to `forecast_history`. When the
two sources disagree by > 3.6°F (~2°C), MAE is multiplied by 1.5 as a
proxy for forecast uncertainty. VC responses cached 6h to fit free
tier (1000 req/day shared).

### Precipitation
- Binary "will it rain" → use `precip_probability / 100` directly.
- "More than X mm" → normal CDF on `precip_mm` with `MAE_PRECIP = 3mm`.

### Edge selection
```
edge_yes = P(forecast) - yes_ask
edge_no = (1 - P(forecast)) - no_ask
side = argmax(edge_yes, edge_no) IF edge ≥ 10pp ELSE skip
```

## Why 10pp default

OpenWeather 5-day MAE ≈ 5°F → ~10pp uncertainty in P(YES) for typical thresholds. 10pp absolute edge gives 1× margin over noise floor. After the analyzer accumulates ≥30 resolved trades, calibration will tell us if 10pp is right. Common adjustments:
- If win rate in 10-15pp bucket is < 50%, raise to 15pp.
- If forecasts of 70% only resolve YES 50% of the time, MAE assumption is too low — increase it.

## Position sizing

We walk the orderbook to find the max size where weighted-avg fill stays within `(1 + 0.20) × best_ask` for buys (or `(1 - 0.20) × best_bid` for sells). Cross-checked against:
- Per-trade cap: 10% of paper portfolio (CLAUDE.md §2)
- Per-market cap: 20%
- Max concurrent positions: 5
- Daily loss limit: 5% of starting balance

Final size = `min(slippage_max, kelly_size, all caps)`. Minimum trade $10.

## Cash-out logic

Multi-trigger policy — any of four triggers fires → cashout. Implemented in
`weather_edge_helpers.py:evaluate_cashout_triggers`.

```
Trigger 1: profit_lock        bid - entry >= 50pp  (default; --profit-lock-pp)
Trigger 2: trailing_stop      peak >= entry + 20pp AND bid <= peak * 0.70
                              (default 30%; --trailing-drawdown-pct)
Trigger 3: convergence        bid >= fair_value - 5pp where
                              fair = forecast_prob_yes (YES) or 1 - forecast_prob_yes (NO)
                              (default 5pp; --convergence-pp)
Trigger 4: forecast_reversal  forecast_prob_now < entry_implied AND bid >= entry
                              (existing logic; break-even backstop)

Guard: if bid < entry_price, triggers 1-3 are suppressed (never sell at a loss).
```

### Why this design

The original "forecast_below_entry → cashout if bid >= entry" rule never fired
for tail-bracket NO bets. Example: NO @ $0.13 with forecast P(NO)=0.95 →
`forecast_prob_now < entry_implied` is `0.95 < 0.13` → always False. The
position would ride all the way to resolution, exposed to:

- the 5% loss tail (paying $0 instead of $1)
- capital lock-up that prevents redeployment
- evaporated paper gains if bid spikes to $0.80 then crashes back to $0.40

The new policy captures profit during the convergence path:

- **profit_lock** locks a known good outcome (4-5x typical on tail bets)
- **trailing_stop** protects realized paper gains against reversal
- **convergence** exits when the orderbook reaches forecast-fair (no more edge to mine)
- **forecast_reversal** retained as backstop for break-even exits

### Persistence

`entries.peak_bid_seen` (added in schema v2) tracks the highwater bid per
position. Updated on every monitor check when current bid exceeds prior peak.
`monitor_checks.decision_reason` records which trigger fired (`profit_lock`,
`trailing_stop`, `convergence`, `forecast_reversal`, or `none` for HOLD).

The analyzer aggregates by trigger so the operator can tune thresholds based
on observed P&L per trigger.

## Monitor cadence

Per open position:
- TTR < 24h → check every 30 min
- 24h ≤ TTR ≤ 48h → check every 60 min

The bot itself ticks every 60s and dispatches checks based on per-position last-run.

## AI Judge

The judge sits between bot proposals and execution. Its purpose: **don't trade on a single forecast source**. OpenWeather alone has systematic errors (heat domes, atmospheric rivers, frontal passages it can underestimate). Cross-checking with NWS + Visual Crossing + news catches those.

The judge:
1. Fetches NWS (US cities) and Visual Crossing forecasts independently.
2. Uses Claude's web_search server tool to find any anomalous catalysts (heat wave warnings, severe weather events, climate news for that city/date).
3. Compares its synthesized P(YES) against the bot's. If they're within 20pp and at least 2 sources agree, APPROVE. Otherwise REJECT or ADJUST size.

See `weather-judge-prompt.md` for the exact guidelines and verdict schema.

### Cost

~5-15 reviews/day × ~$0.05-0.15 each = ~$0.25-2/day. Cap with `JUDGE_DAILY_BUDGET_USD=5` (default). When exceeded, judge skips remaining proposals for the rest of the day UTC.

### When the judge can be bypassed

`--judge-mode=off` flag on the bot allows direct execution without judge approval. Use only:
- During development / smoke tests.
- When `ANTHROPIC_API_KEY` is unavailable temporarily.
- For markets with TTR < `--fast-path-ttr-min` (default 60 min) where waiting for judge would miss the window.

## Persistence + observability

All decisions persist in `~/.polymarket-paper/weather_edge.db` (SQLite, 6 tables). Schema versioned via `PRAGMA user_version`. JSONL stream at `~/.polymarket-paper/weather_edge.jsonl` mirrors stdout (journald).

### Replay any decision

```bash
python weather_edge_analyzer.py --replay-entry 42
```

Prints the OpenWeather snapshot the bot saw, the judge's verdict and rationale, the cashout (if any), the resolution outcome, and the counterfactual delta. Lets you verify why a decision was made and whether updated parameters would have changed it.

## Tuning loop (closed)

After ~50 resolved trades:
```bash
python weather_edge_analyzer.py --since 2026-05-01 --output report-md > report.md
```

Read the suggestions section. Common fixes:
- Raise `--min-edge-pp` if low buckets show negative held P&L.
- Increase `MAE_TEMP_F` constant in helpers if forecast probs are over-calibrated.
- Tighten cashout trigger for low-TTR bucket if cashouts there are mostly suboptimal.

Apply changes manually (the analyzer suggests; the operator decides). Re-run the analyzer on subsequent windows to verify the change improved metrics.
