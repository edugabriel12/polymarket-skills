# Polymarket Copy-Trader (Paper)

A **separate, self-contained** copy-trade flow: save public Polymarket wallets, have their
**buys and sells** tracked continuously, and mirror them into a **paper (mock) portfolio**
funded with **$10,000 fake USD**. Read-only against public APIs — **no private key, no funds
at risk**.

> Paper trading simulation — not financial advice. Real trading involves risk of loss.

## What it does

- **Save wallets** by name + address (`wallets` table).
- **Track every buy/sell** of each saved wallet by polling the Data API `/trades` endpoint
  (`side=BUY/SELL`). Only trades made *after* you save a wallet are copied (baseline snapshot).
- **Weather markets only** (default) — non-weather trades are ignored, detected by the same
  keyword set as the weather edge bot plus the Gamma `weather` tag. Set `COPY_WEATHER_ONLY=0` to
  copy all markets.
- **Copy into a paper portfolio**, automatically:
  - **BUY** — copy the **same USD value the wallet bought**, clamped to **[$5, $100]** (below $5 →
    $5; above $100 → $100). A **20% slippage guard** may reduce the size using the live order book;
    if that falls below the $5 floor (book too thin) or the portfolio is out of cash → logged as
    **skipped** (never silently dropped).
  - **SELL** — mirror the **same fraction** the wallet sold. If that paper sell would breach
    **20% slippage**, it is **not executed** (logged as skipped).
  - **Settlement** — a position pays out ($1 per winning share) **only when the market has
    genuinely resolved** (Gamma `closed` flag or a past end date), never merely because the
    live price is pinned near 0/1. This prevents a heavy favorite that is only *trading* at
    0.98 from paying out prematurely and inflating the paper cash. Until resolution, open
    positions are marked to the live price.
- Every attempt (executed or skipped) is a row in the `entries` table, linked to its wallet.

## Tabs

- **Carteiras** — add/save wallets, pause/resume, delete, trigger an immediate check.
- **Entradas** — every copy entry: value, **LIVE result** (Acerto / Erro / preço atual), wallet,
  market, slippage. Filter by wallet and status.
- **Resultados** — portfolio KPIs + per-wallet **P&L, ROI, win rate, slippage médio,
  % executadas vs falhas**. Click a wallet to drill into its paginated entry list.

## Reuses (no reimplementation)

- Slippage sizer/gate — `polymarket-analyzer/.../weather_edge_helpers.py::compute_max_size_for_slippage`
- Book-walk fill — `polymarket-paper-trader/.../paper_engine.py::_simulate_fill`
- Wallet trade feed — `polymarket-wallet-analyzer/.../analyze_wallet.py::fetch_trades`
- Order book — CLOB `/book` (normalized in `backend/deps.py`)

See `backend/deps.py` for the exact wiring.

## Run (dev)

```bash
cd polymarket-copy-trader
./dev.sh
# Backend  -> http://localhost:8002
# Frontend -> http://localhost:5175
```

Or separately:

```bash
# Backend
cd backend && python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --port 8002 --reload

# Frontend
cd frontend && npm install && npm run dev
```

### Config (env)

| Var | Default | Purpose |
|---|---|---|
| `COPYTRADE_DB` | `~/.polymarket-copy-trader/copytrade.db` | SQLite path |
| `COPY_POLL_SEC` | `60` | Background poll interval (seconds) |
| `AUTO_POLL` | `1` | Set `0` to disable the background loop (poll via `POST /api/poll`) |
| `COPY_DEBUG` | `1` | Detailed per-operation debug logs to stderr. Set `0` to silence. |
| `COPY_WEATHER_ONLY` | `1` | Copy only weather markets. Set `0` to copy all markets. |

### Debug logs

With `COPY_DEBUG=1` (default), every copied operation is traced to stderr in three steps —
`1) ENTRADA DA CARTEIRA` (the tracked wallet's raw trade) → `2) ANÁLISE DE VOLUME/CAP`
(order-book slippage sizing + the paper cap decision) → `3) ENTRADA DO PAPER` (what the paper
portfolio did, or why it skipped) — plus a `LIQUIDAÇÃO` line when a market resolves. Example:

```
[copy-trader] ── carteira 'Whale' (0xffff…ffff) — 2 novo(s) trade(s) desde ts=0 ──
[copy-trader] 1) ENTRADA DA CARTEIRA: BUY 'Rain in NYC' @ 0.5000 × 200.00 sh (cond=0xcond1…)
[copy-trader] 2) ANÁLISE DE VOLUME/CAP: best_ask=0.5000 vol24h=$25,000.00
[copy-trader]    slippage-max ≤20% = $1,050.00 (avg 0.5250, slip 5.00%)
[copy-trader]    alvo = min($1,050.00, teto $100.00) = $100.00 | piso $5.00 | caixa $10,000.00
[copy-trader] 3) ENTRADA DO PAPER: EXECUTED — $100.00 → 200.00 sh @ 0.5000 (slip 0.00%) | caixa→$9,900.00
```

## Tests (offline, no network)

```bash
cd backend && . .venv/bin/activate
python test_db.py
python test_copy_engine.py
python test_poller.py
```

## API

`GET /api/health` · `GET|POST /api/wallets` · `PATCH|DELETE /api/wallets/{id}` ·
`GET /api/entries` · `GET /api/wallets/{id}/entries` · `GET /api/results` ·
`GET /api/portfolio` · `POST /api/poll` · `POST /api/portfolio/reset` · `GET /api/config`
