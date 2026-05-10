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

The simplest rule that captures the operator's intent:

```python
if forecast_prob_now < entry_implied:
    # Our edge has disappeared
    if current_bid >= entry_price:
        cash_out()        # break-even or profit
    else:
        hold()            # bid too low; wait for recovery
else:
    hold()                # edge still intact
```

**No hard stop-loss.** If the forecast continues to support the position, we hold to resolution. If it deteriorates but the market won't pay break-even, we still hold — losing only when the market's resolution itself goes against us.

This protects against panic-selling on transient forecast wobbles AND lets us capture cleanly when the bid recovers to entry.

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
