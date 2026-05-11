---
name: polymarket-analyzer
description: >
  Use this skill whenever the user wants to find trading opportunities, detect arbitrage,
  analyze a market, perform edge detection, find mispricing, do probability analysis,
  evaluate orderbook depth, find momentum signals, or assess Polymarket market quality.
  Triggers: "find opportunities", "detect arbitrage", "analyze market", "edge detection",
  "mispricing", "probability analysis", "orderbook analysis", "momentum scanner",
  "market inefficiency", "price gap", "volume surge", "trading edge", "market analysis".
---

# Polymarket Analyzer Skill

Detect trading edges and opportunities across Polymarket prediction markets using
real-time data from the Gamma and CLOB APIs. Zero authentication required -- all
analysis is read-only.

## Available Scripts

### 1. Find Arbitrage Edges (`scripts/find_edges.py`)

Scans all active markets for pricing inefficiencies:

- **Underpriced**: YES + NO < $1.00 (guaranteed profit if you buy both sides)
- **Overpriced**: YES + NO > $1.02 (sell opportunity)
- Calculates profit after fees for each opportunity
- Outputs market name, prices, sum, potential profit, and fee impact

```bash
python scripts/find_edges.py
python scripts/find_edges.py --min-edge 0.02 --limit 500
```

### 2. Analyze Order Book (`scripts/analyze_orderbook.py`)

Deep analysis of a single market's order book:

- Spread, mid-price, bid/ask depth (top N levels)
- Bid-ask imbalance ratio (signals directional pressure)
- Thin vs thick book classification
- Liquidity concentration analysis

```bash
python scripts/analyze_orderbook.py --token-id <TOKEN_ID>
python scripts/analyze_orderbook.py --token-id <TOKEN_ID> --depth 10
```

### 3. Momentum Scanner (`scripts/momentum_scanner.py`)

Detect markets with unusual activity:

- **Volume surges**: 24h volume significantly exceeds 7-day average
- **Price momentum**: recent price moves in one direction
- **Liquidity changes**: markets gaining or losing depth
- Ranked output by signal strength

```bash
python scripts/momentum_scanner.py
python scripts/momentum_scanner.py --min-volume 10000 --limit 300
```

### 4. Correlation Tracker (`scripts/correlation_tracker.py`)

Detect hidden correlated exposure in your portfolio:

- Groups positions by topic (crypto, politics, sports, geopolitics, etc.)
- Detects shared qualifiers ("insider trading", "FIFA World Cup", etc.)
- Warns when correlated clusters exceed concentration limits
- Outputs diversification score (0-100)

```bash
python scripts/correlation_tracker.py
python scripts/correlation_tracker.py --json
python scripts/correlation_tracker.py --threshold 0.10
```

### 5. LoL Top Holders (`scripts/lol_top_holders.py`)

Scan resolved League of Legends markets and identify the top holders of the
winning side, with optional P&L estimation. Useful for copy-trading research
(find wallets that consistently bet correctly) and whale tracking.

- Pulls closed markets from Gamma API (tries `league-of-legends`, `lol`, `esports`
  tag slugs; falls back to text search)
- Determines winning outcome from `outcomePrices` (skips disputed/canceled)
- Fetches top N holders of the winning CLOB token via the Polymarket Data API
- Optionally fetches each holder's trades and computes weighted-avg entry +
  realized/unrealized P&L
- Aggregates cross-market: top 20 wallets globally by total P&L

```bash
python scripts/lol_top_holders.py                       # last 30d, top 10, JSON
python scripts/lol_top_holders.py --days 90 --top 20
python scripts/lol_top_holders.py --tag esports
python scripts/lol_top_holders.py --markets slug-1 slug-2 --no-pnl
python scripts/lol_top_holders.py --output csv --out /tmp/holders.csv
```

See `references/data-api.md` for the assumed Data API endpoint shapes and how
to verify them against the live API.

### 6. Weather Edge Bot (`scripts/weather_edge_bot.py`) + AI Judge

24/7 daemon that finds edges between OpenWeather forecasts and Polymarket
weather market prices. Targets markets resolving in the next 48h with prices
in [0.20, 0.70]. Sizes by orderbook liquidity (≤20% slippage). Cashes out
opportunistically when forecast deteriorates AND bid permits break-even+.

Two-process architecture:
- **Bot** (`weather_edge_bot.py`): discovery + monitor + cashout. Emits proposals.
- **Judge** (`weather_edge_judge.py`): Claude API daemon that cross-checks every
  proposal against NWS / Visual Crossing / web search before approving execution.
- **Analyzer** (`weather_edge_analyzer.py`): on-demand counterfactual analysis +
  threshold tuning suggestions.

```bash
# Smoke test (one cycle, no API costs):
python scripts/weather_edge_bot.py --once --dry-run --judge-mode=off --debug

# Daemon mode (deploy via systemd; see agent/weather-edge-bot.service):
python scripts/weather_edge_bot.py --daemon

# After ≥30 trades, generate analysis report:
python scripts/weather_edge_analyzer.py --since 2026-05-01 > report.md

# Replay any decision:
python scripts/weather_edge_analyzer.py --replay-entry 42
```

Persistence at `~/.polymarket-paper/weather_edge.db` (SQLite). Logs at
`~/.polymarket-paper/weather_edge.jsonl`. See
`references/weather-edge-strategy.md` for the strategy doc and
`references/weather-judge-prompt.md` for the judge's system prompt.

Paper-only by design (CLAUDE.md §4 compliance — paper-first before live).

### 7. Weather Strategy Advisor (`scripts/weather_strategy_advisor.py`)

Weekly meta-agent (Claude Opus 4.7) that ingests the analyzer's aggregate
output plus per-city/per-trigger extras and proposes concrete tuning
suggestions: threshold defaults, MAE constants, city blacklist/whitelist,
judge-prompt edits, data-source swaps. **Read-only — operator applies any
change manually.** Backed by `references/strategy-advisor-prompt.md`.

```bash
# Preview without API call
python scripts/weather_strategy_advisor.py --dry-run --since-days 14

# Real run (uses ANTHROPIC_API_KEY; ~$1-2/run with cache)
python scripts/weather_strategy_advisor.py --once --since-days 14
```

Reports: `~/.polymarket-paper/advisor_reports/YYYY-MM-DD_strategy_report.{md,json}`.
Run history: `advisor_runs` table in `weather_edge.db` (schema v3).
Deploy weekly via `agent/weather-strategy-advisor.{service,timer}` (systemd).

## Workflow

1. Run `find_edges.py` to scan for arbitrage across all active markets
2. For interesting markets, run `analyze_orderbook.py` to check if the edge is executable
3. Run `momentum_scanner.py` to find markets with directional momentum
4. Combine findings to identify the best opportunities

## Fee Awareness

Most Polymarket markets are fee-free. Crypto 5-min/15-min markets have dynamic taker
fees: `fee = baseRate * min(price, 1 - price) * size`. See `references/fee-model.md`
for the full fee calculator and breakeven analysis.

## Strategy Reference

See `references/viable-strategies.md` for the four strategies that still work in 2026
with win rates, expected returns, and risk profiles.

## Important Disclaimers

- This skill performs read-only analysis only -- no trades are executed
- Past patterns do not guarantee future results
- Always verify opportunities manually before trading
- Not financial advice
