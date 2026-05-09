# Polymarket Data API — endpoint reference

Base URL: `https://data-api.polymarket.com`

The Data API is a public, unauthenticated read-only API for Polymarket on-chain
data (positions, trades, holders, leaderboards). It complements:

- **Gamma API** (`gamma-api.polymarket.com`) — market metadata, tags
- **CLOB API** (`clob.polymarket.com`) — orderbooks, prices, authenticated trading

When this file says **"unverified"**, it means the shape was inferred from
public usage and Polymarket's frontend network calls; it has NOT been validated
inside this skill's sandbox (the sandbox blocks egress to `data-api.polymarket.com`).
**Verify with `--debug` flag on `lol_top_holders.py` before relying on output.**

## Endpoints used by `lol_top_holders.py`

### `GET /holders`

Top holders of a market or token.

**Tried query shapes (in order, first non-empty wins):**

| Params | Notes |
|---|---|
| `?market=<conditionId>&limit=N` | conditionId is the 0x-prefixed bytes32 from Gamma's `conditionId` field |
| `?token=<clobTokenId>&limit=N` | numeric string from Gamma's `clobTokenIds[i]` |
| `?market=<conditionId>&token=<clobTokenId>&limit=N` | both, when one side is needed |

**Response shape (normalized by client):**

```json
[
  {"proxyWallet": "0x...", "amount": 1234.56, "outcome": "Yes"},
  ...
]
```

The script accepts any of these key variants for the address field:
`proxyWallet`, `user`, `address`. For size: `amount`, `size`, `balance`. If the
response wraps the list in `{"data": [...]}` or `{"holders": [...]}`, both are
unwrapped automatically.

If all three shapes return empty/error, the script logs `0 holder(s)` for the
market and continues.

### `GET /trades`

Trade history for a wallet on a specific market.

**Tried query shapes:**

| Params | Notes |
|---|---|
| `?market=<conditionId>&user=<address>` | most common |
| `?market=<conditionId>&maker=<address>` | fallback if `user` is rejected |
| `?market=<conditionId>&address=<address>` | last resort |

**Response shape (per-trade, unverified):**

```json
{
  "tokenId": "12345...",     // CLOB token ID this trade was on
  "side": "BUY" | "SELL",     // or "BID" / "ASK"
  "price": 0.45,              // USDC per share
  "size": 100.0,              // shares
  "timestamp": 1234567890,
  "tx_hash": "0x..."
}
```

The P&L estimator is robust to missing fields — trades that fail to parse are
silently skipped, and if all trades for an address fail to parse, the address
gets `n_trades=0, total_pnl_usd=0` rather than an error.

## Endpoints not (yet) used

- `GET /positions?user=<address>` — current portfolio of a wallet
- `GET /value?user=<address>` — USD value snapshot
- `GET /activity?user=<address>` — combined trade + transfer feed
- `GET /leaderboard?market=<id>` — possibly equivalent to `/holders` ranked

These are documented here for future use; not invoked by `lol_top_holders.py`.

## How to verify shapes locally

The simplest check, on a machine with network egress:

```bash
# Pick a known resolved market, fetch its conditionId from Gamma
curl -s "https://gamma-api.polymarket.com/markets?slug=will-t1-win-worlds-2024-finals" | jq '.[0] | {slug, conditionId, clobTokenIds, outcomePrices}'

# Try the holders endpoint with that conditionId
curl -s "https://data-api.polymarket.com/holders?market=<conditionId>&limit=5" | jq

# And trades for one of the addresses returned
curl -s "https://data-api.polymarket.com/trades?market=<conditionId>&user=<address>" | jq '.[0:3]'
```

If the shape differs from what's documented here, update this file and adjust
`fetch_holders` / `fetch_trades` in `lol_top_holders.py` accordingly.

## Rate limits

No documented limits as of writing. The script defaults to 100ms between
requests (`--rate-limit`) with exponential backoff on 429. In practice, a single
run of `lol_top_holders.py --top 10 --days 30` makes:

- 1 Gamma `/markets` call per tag candidate (≤4)
- 1 Data `/holders` call per market (up to 3 retries with different shapes)
- 1 Data `/trades` call per holder per market (skipped if `--no-pnl`)

For 10 LoL markets × 10 holders, expect ~110 requests total → ~12s wallclock.

## Failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `0 holder(s)` for every market | Data API endpoint moved or shape changed | Run with `--debug`, inspect response, update `fetch_holders` |
| `n_trades=0` for every holder | Same, for trades endpoint | `--no-pnl` until shapes are mapped |
| Discovery returns 0 markets | tag_slug doesn't exist for LoL | Pass `--tag <slug>` explicitly, or rely on text-search fallback |
| 403 on Gamma | Egress blocked or API down | Check from a browser/curl outside the runtime |
