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
- **Copy into a paper portfolio**, automatically:
  - **BUY** — size the copy so weighted-avg fill **slippage stays ≤ 20%**, using the live order
    book and market volume. Capped at **$100**, floored at **$5**. Too thin / out of cash → the
    attempt is logged as **skipped** (never silently dropped).
  - **SELL** — mirror the **same fraction** the wallet sold. If that paper sell would breach
    **20% slippage**, it is **not executed** (logged as skipped).
  - **Settlement** — when a market resolves, open paper positions are closed and marked
    Acerto/Erro with realized P&L.
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
