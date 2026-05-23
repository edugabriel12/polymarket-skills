# Deferred tunings — revisit after N=30+ resolutions

Operator-facing memo of all calibration/strategy changes that were intentionally
**not applied immediately** during the v9.5-v9.13 calibration sprint. Each item
requires real outcome data (≥30 resolved trades post-fix) before deciding.

Last update: 2026-05-24 after the v9.12 / v9.13 calibration tightening.

---

## How to evaluate at decision time

Once you have N≥30 closed positions on the `/positions/history` page after the
relevant fix went live:

1. Run `python polymarket-analyzer/scripts/weather_strategy_advisor.py --days 14`
   — the advisor produces cohort breakdowns by city, MAE bucket, side, comparison,
   range_penalty_mult, ladder position.
2. Inspect `judge_accuracy`, `ladder_breakdown`, and the per-cohort win rates.
3. Use the trigger conditions below to decide which deferred item (if any) to ship.

---

## 1. Drop clip ceiling further (0.80 → 0.75)

**Why deferred:** v9.12 already moved from 0.90 → 0.80. Going to 0.75 is more
aggressive and would filter out additional marginal trades.

**Trigger to ship:**
- Win rate of trades with `bot_prob ≥ 0.78` (near new ceiling) is still <50% after N=30
- Rule 6 overrides still firing on >25% of verdicts despite v9.12 tighter clip
- Average pnl_per_dollar in the high-confidence cohort is negative

**How to ship:**
```python
# weather_edge_helpers.py
PROB_CLIP_HIGH = 0.75
PROB_CLIP_LOW = 0.25
```
Plus update Tests 4, 8, J1-J5 assertions accordingly.

---

## 2. Wider MAE base (5°F → 7°F)

**Why deferred:** v9.12 + v9.13 already widen MAE conditionally (graduated OM
spread, range penalty). Bumping the floor uniformly is the next lever if those
conditional widenings aren't enough.

**Trigger to ship:**
- A+D from v9.12 + v9.13 didn't move win rate above 50% with N≥30
- Cohort analysis shows ALL temperature brackets (not just range) have poor
  calibration — i.e., the bot is broadly overconfident, not narrowly on range
- forecast_history shows MAE realized > 5°F consistently per city

**How to ship:**
```python
# weather_edge_helpers.py
MAE_TEMP_F = 7.0
MAE_TEMP_C = 3.89   # = 7°F / 1.8
```

---

## 3. Calibration via judge feedback (Path C from calibration plan)

**Why deferred:** needs ≥50 resolutions where bot_prob, judge_prob, AND
final_outcome are all known. Below that sample, the calibration shift is noise.

**Trigger to ship:**
- N≥50 resolutions with judge_reviews + resolutions joined
- Visible systematic bias in calibration curve: when bot says X, actual win
  rate is consistently X-Y for some shift Y
- Per-city or per-cohort shifts that exceed 10pp

**How to ship:**
- New `compute_calibration_shift(conn, days)` in `weather_edge_analyzer.py` that
  returns `{cohort: shift_pp}` (e.g., `{'Beijing': -8, 'Madrid': +3, ...}`)
- Apply learned shift in `forecast_probability()` post-Gaussian, pre-clip
- Surface in advisor's weekly markdown report
- Operator reviews and accepts before applying

---

## 4. Block range brackets entirely (Option 3 from NO-bias plan)

**Why deferred:** v9.13's 1.5x MAE penalty is the surgical fix. If that proves
insufficient, the radical option is to filter range comparisons entirely and
keep only `at_least` / `at_most` directional brackets.

**Trigger to ship:**
- Range bets win rate <40% after N≥30 trades with v9.13 active
- pnl_per_dollar of range cohort is negative AND magnitude worse than at_least
  cohort
- Operator wants tighter NO-bias control regardless of trade volume reduction

**How to ship:**
```python
# weather_edge_bot.py argparse
p.add_argument("--no-range-brackets", action="store_true",
               help="Skip 'range' comparison markets entirely. Only "
                    "discover at_least/at_most directional brackets.")

# in run_discovery loop, after parse_market:
if args.no_range_brackets and spec.comparison == "range":
    skipped["range_excluded"] += 1
    continue
```

Trade-off: cuts ~60% of trade flow. Only ship if data clearly shows range is
structurally -EV.

---

## 5. Multi-source confluence requirement for range bets (Option 4)

**Why deferred:** more nuanced version of #4. Allows range bets only when
multiple forecast sources confirm low uncertainty.

**Trigger to ship:**
- v9.13's flat 1.5x MAE penalty for range is undershooting (range cohort still
  -EV) but blocking all range (option #4) drops too many trades
- Forecast_history shows OW/VC/OM agreement varies a lot across cities

**How to ship:**
```python
# in _build_ladder_candidates or run_discovery, after parse_market:
if spec.comparison == "range":
    needs_confluence = True
    if (om_data and om_data.get("agree")  # spread <2C
        and abs(ow_value - vc_value) < 1.0   # OW vs VC <1C
        and forecast_point_inside_bracket(spec, ow_value)):
        # ok — range bet allowed
        pass
    else:
        skipped["range_no_confluence"] += 1
        continue
```

---

## 6. Per-event exposure cap (`--max-event-exposure-usd`)

**Why deferred:** today the cap is per-`market_slug` (per-leg). With 3-bin
ladders 3 legs of the same event can each consume the per-leg cap → up to 3x
the intended event exposure.

**Trigger to ship:**
- Snapshot shows any single ladder group consuming >$150 total stake (3 × $50)
- Multiple groups in same event firing back-to-back
- Operator wants tighter per-event risk

**How to ship:**
```python
# weather_edge_bot.py argparse
p.add_argument("--max-event-exposure-usd", type=float, default=100.0,
               help="Total $ exposure cap per ladder_event_slug (sums all "
                    "legs of all groups in the same Gamma event).")

# in _build_ladder_candidates after grouping:
event_exposure = sum(... existing entries with this event_slug ...)
if event_exposure + planned_total > args.max_event_exposure_usd:
    # truncate or skip
```

---

## 7. Bundle judging (1 LLM call per ladder, not per leg)

**Why deferred:** today each leg goes through judge independently (~$0.04 each,
so a 3-leg ladder costs $0.12). Bundling = 1 call evaluates all 3 legs together.

**Trigger to ship:**
- Daily judge cost consistently >$15 (we're at ~$3-6/day now)
- Operator wants to switch to Haiku permanently and the per-call overhead
  matters more
- Most ladders today are still 2-bin (cost $0.08); 3-bin scaling would push
  cost up

**How to ship:**
- New prompt template in `weather-judge-prompt.md` for bundle evaluation
  (judge sees all 3 legs in one user message, returns verdicts as JSON array)
- New `review_ladder_bundle()` in `weather_edge_judge.py` that takes a
  list of legs and returns `{leg_entry_id: verdict_dict}`
- Atomic gate skips per-leg judging when group_id present, calls bundle judge
  once per group instead

---

## 8. 5-bin Hans323-style tail extension

**Why deferred:** operator explicitly excluded long-shot tail bets (the
`--min-entry-price 0.30` decision). Hans323's strategy is cheap-tail
($0.01-$0.05 brackets that pay 20-100x). Operator's preference is mid-band
high-prob (the current 0.30-0.85 band).

**Trigger to ship:**
- Operator changes mind about tail strategy after seeing mid-band results
- Mid-band strategy proves marginal and the operator wants to diversify
  variance source
- Sample size justifies splitting capital across two approaches

**How to ship:**
- Extend `select_ladder_brackets` to optionally pick 2 tail brackets beyond
  the 3-bin core (positions: `tail_low`, `tail_high`)
- New `--ladder-tail-mode {off, 5bin}` flag
- Tail legs use very small Kelly weights (capped at 5% of total stake each)

---

## 9. WebSocket live prices

**Why deferred:** polling at 60s monitor / 30s dashboard refresh is adequate
for current operation pace. WS would reduce latency for trigger evaluation.

**Trigger to ship:**
- Cashout misses observed: bid swept through trigger price between two
  monitor cycles, losing 20-40pp of potential profit on the cashout
- Operator scales to >100 concurrent positions where polling becomes a
  bottleneck

**How to ship:**
- New `services/clob_websocket.py` that maintains a persistent WS connection
  to wss://ws-subscriptions-clob.polymarket.com/ws/market
- Pushes bid/ask updates into a shared cache that monitor and dashboard read
- Polling fallback when WS disconnects

---

## 10. Dynamic --min-edge-pp based on calibration confidence

**Why deferred:** static thresholds. Could be dynamic per cohort.

**Trigger to ship:**
- Calibration tracker (item 3) is live and shows per-city/per-cohort
  reliability varies widely
- Some cohorts can be trusted at edge 8pp, others need 20pp

**How to ship:**
- Per-cohort `min_edge_pp` lookup table populated by advisor
- Default falls back to global --min-edge-pp / --ladder-min-leg-edge-pp

---

## 11. Per-side Kelly cap (asymmetric payoff awareness)

**Why deferred:** v9.13's range penalty addresses the main mechanism. But
NO bets structurally have -100%/+25% asymmetric payoff, so even with right
math the variance is asymmetric.

**Trigger to ship:**
- After N≥30 resolutions, NO-side bets show negative pnl_per_dollar even
  with v9.13 range penalty active
- Operator wants stricter per-trade NO-side risk

**How to ship:**
```python
# in compute_kelly_split or sizing logic:
if leg["side"] == "NO":
    leg["stake_usd"] *= 0.6  # haircut for asymmetric payoff
```
Or surface as `--kelly-no-side-haircut 0.6` flag.

---

## 12. Bot daemon health monitor + alert

**Why deferred:** the v9.11 IndexError bug ran silent for 15h. Need a
heartbeat-based alert system to catch this kind of failure faster.

**Trigger to ship:**
- Another silent main_loop crash event happens
- Operator wants proactive notification when bot loop misses N consecutive
  ticks

**How to ship:**
- Add `last_successful_loop_iteration` timestamp written to a sentinel file
  every loop iteration (separate from heartbeat)
- Cron / systemd timer checks staleness — if last write >10 min ago,
  notify via existing notifier service
- Or: dashboard `/api/health` endpoint that returns staleness; alert if
  stale on overview page

---

## Decision matrix template

When the operator considers shipping any of the above, fill in:

| Item | N at decision | Win rate / metric | Trigger condition met? | Decision |
|---|---|---|---|---|
| 1. Clip 0.75 | | | | |
| 2. MAE 7°F | | | | |
| 3. Calibration tracker | | | | |
| 4. Block range | | | | |
| 5. Confluence range | | | | |
| 6. Event exposure cap | | | | |
| 7. Bundle judging | | | | |
| 8. 5-bin tail | | | | |
| 9. WebSocket | | | | |
| 10. Dynamic edge | | | | |
| 11. NO-side haircut | | | | |
| 12. Daemon health monitor | | | | |

---

## Currently shipped baselines (as of 2026-05-24)

For reference when measuring deltas:

| Setting | Value | From |
|---|---|---|
| `--min-edge-pp` | 20 | v9.2 |
| `--ladder-min-leg-edge-pp` | 10 | v9.2 |
| `--execute-min-edge-pp` | 8 | v9.2 |
| `--ladder-execute-min-leg-edge-pp` | 4 | v9.2 |
| `--min-entry-price` | 0.30 | v9.1 |
| `--ladder-min-leg-price` | 0.10 | v9.1 |
| `--max-entry-price` | 0.85 | v9.0 |
| `--min-ttr-hours` | 12 | v9.3 |
| `--ladder-min-ttr-hours` | 6 | v9.3 |
| `--convergence-pp` | 0.0 (disabled) | v9.5 |
| `--profit-lock-pp` | 50 | v9.0 |
| `--trailing-drawdown-pct` | 15 | pre-v9 |
| `--ladder-mode` | 3bin | v9.0 |
| `--ladder-stake-split` | kelly | v9.0 |
| `--max-market-exposure-usd` | 50 | v9.0 |
| `max_concurrent_positions` | 50 | v9 (CLAUDE.md §2) |
| `PROB_CLIP_HIGH` | 0.80 | v9.12 |
| `PROB_CLIP_LOW` | 0.20 | v9.12 |
| `MAE_TEMP_F` | 5.0 | pre-v9 |
| `MAE_TEMP_C` | 2.78 | pre-v9 |
| OM spread mult | 1.0/1.3/2.0/3.0 graduated | v9.12 |
| Range bracket MAE mult | 1.5x | v9.13 |
| `JUDGE_PREJUDGE_MIN_EDGE_PP` | 15 (env) | v9 |
| `JUDGE_DAILY_BUDGET_USD` | 20 (env) | v9 |
| `CLAUDE_JUDGE_MODEL` | sonnet-4-6 (operator-reverted) | (Haiku had thinking bug) |
