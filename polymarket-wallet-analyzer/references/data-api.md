# Data API — wallet endpoints used by `analyze_wallet.py`

Base URL: `https://data-api.polymarket.com`

Public, unauthenticated, read-only. Complements the Gamma API
(`gamma-api.polymarket.com`, market metadata + tags) and the CLOB API
(`clob.polymarket.com`, orderbooks/prices/authenticated trading).

> **Verification status:** the Polymarket sandbox blocks egress to
> `data-api.polymarket.com`, so the exact response shapes below were inferred
> from Polymarket's public frontend network calls and the existing
> `polymarket-analyzer/scripts/lol_top_holders.py`. The script is written to be
> tolerant of shape drift (multiple query-param attempts, key-name fallbacks,
> list/wrapped-list unwrapping). **Run with `--debug` to confirm shapes on a
> machine with network egress before trusting the numbers.**

## `GET /positions`

Current holdings of a wallet, with Polymarket's own computed P&L per position.

**Tried query shapes (first non-empty wins), paginated by `offset`:**

| Params | Notes |
|---|---|
| `?user=<addr>&limit=500&offset=N&sizeThreshold=0` | primary |
| `?user=<addr>&limit=500&offset=N` | fallback |
| `?address=<addr>&limit=500&offset=N` | last resort |

**Fields consumed (per position):**

| Field | Used for |
|---|---|
| `conditionId` (or `market`) | market identity / dedupe key |
| `title`, `slug`, `eventSlug` | display + category classification |
| `cashPnl` | total P&L for the position (realized + unrealized) |
| `realizedPnl` | realized split (unrealized derived as `cashPnl - realizedPnl`) |
| `initialValue` / `totalBought` | amount invested |
| `currentValue` | current mark value |
| `curPrice` | resolution detection (~0 or ~1 ⇒ resolved) |
| `redeemable` | resolution detection |
| `endDate` | resolution detection (past ⇒ resolved) |

Wrapped responses (`{"data":[...]}` / `{"positions":[...]}`) are unwrapped
automatically.

## `GET /trades`

Full trade history for a wallet, across all markets.

**Tried query shapes (first non-empty wins), paginated by `offset`:**

| Params | Notes |
|---|---|
| `?user=<addr>&limit=500&offset=N` | primary |
| `?address=<addr>&limit=500&offset=N` | fallback |
| `?maker=<addr>&limit=500&offset=N` | last resort |

**Fields consumed (per trade):**

| Field | Used for |
|---|---|
| `proxyWallet` (or `user`/`address`/`maker`) | **owner attribution — ownership filter** |
| `conditionId` (or `market`) | market identity |
| `asset` / `tokenId` / `token_id` | per-outcome cost basis |
| `side` (`BUY`/`SELL`, also `BID`/`ASK`) | direction |
| `price`, `size` | average-cost P&L reconstruction |
| `title`, `slug`, `eventSlug` | display + category for exited markets |

> **Ownership is verified client-side.** The wallet query param above was
> inferred from the public frontend, never confirmed live — and it has been
> observed returning trades that do **not** belong to the requested wallet (an
> unfiltered / counterparty feed). `fetch_trades` therefore keeps only records
> whose owner address (`proxyWallet`/`user`/`address`/`maker`) matches the
> requested wallet (`owned_trades`), so a wallet is never credited with — nor
> copied on — another wallet's activity. If no record carries a recognizable
> owner field, the shape is unknown and the raw feed is returned with a stderr
> warning. **When verifying shapes locally, confirm the owner field name.**

## `GET /events` (Gamma, only with `--enrich-tags`)

`https://gamma-api.polymarket.com/events?slug=<eventSlug>` → the event's `tags`
(`[{label, slug}, ...]`). Tag slugs are mapped to categories via
`TAG_TO_CATEGORY` in the script. Used to override keyword classification when
`--enrich-tags` is passed.

## P&L & win-rate methodology

- **Authoritative source:** `/positions.cashPnl` for any market the wallet
  currently holds (or can redeem). Polymarket computes this; we trust it.
- **Reconstruction:** for markets absent from `/positions` (fully exited), P&L is
  rebuilt from `/trades` with average-cost accounting: BUYs accumulate cost,
  SELLs realize `(price − avg_entry) × size`. Residual open shares (rare in an
  exited market) are marked at the last trade price.
- **Resolved:** `redeemable` true, OR `endDate` in the past, OR `curPrice`
  within 0.02 of 0/1. Only resolved markets count toward win rate.
- **Win:** `total_pnl > 0`. Magnitudes below `1e-6` are scratches (excluded).
- **ROI:** `total_pnl / invested` per bucket; `null` when nothing was invested.

### Known limitations

- Redemption payouts that never appear as a SELL trade are approximated by the
  resolution mark; a market the wallet held to resolution and redeemed is valued
  via `/positions` when still present, otherwise via the last trade price. If
  Polymarket exposes a closed-position or PnL-history endpoint, prefer it.
- Keyword categorization is heuristic. Ambiguous names ("US Open", "the Open",
  generic "vs") may misclassify — use `--enrich-tags` for tag-based accuracy.

## How to verify shapes locally

```bash
ADDR=0x...   # target wallet

curl -s "https://data-api.polymarket.com/positions?user=$ADDR&limit=3" | jq '.[0]'
curl -s "https://data-api.polymarket.com/trades?user=$ADDR&limit=3"    | jq '.[0]'

# If keys differ from this doc, update fetch_positions / fetch_trades /
# reconstruct_trade_pnl in scripts/analyze_wallet.py and amend this file.
```

## Rate limits

No documented limit. The script defaults to 100ms between calls (`--rate-limit`)
with exponential backoff on HTTP 429. A typical run is a handful of paginated
`/positions` + `/trades` calls; `--enrich-tags` adds one Gamma `/events` call per
distinct event.
