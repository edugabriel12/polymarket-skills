# Polymarket Wallet Dashboard

A web app (FastAPI + React) that analyzes **any public Polymarket wallet** by address and reports
**Win rate, número de apostas, P&L e ROI** — overall, **por categoria** (Futebol, Tênis, Baseball,
LoL, CS2, Dota, Basquete, …) and **por sub-categoria** (Ambas Marcam, Over/Under, Moneyline,
Vencedor de mapa, Total de mapas, …).

Read-only — it uses the public Polymarket Data API via the `polymarket-wallet-analyzer` engine and
needs **no private key**. Market text is untrusted and only pattern-matched (CLAUDE.md rule #5).

## How it works
- **Engine:** reuses `polymarket-wallet-analyzer/scripts/analyze_wallet.py` — `/positions` + `/trades`,
  average-cost realized P&L, category, resolved/won. A "bet" is one market (conditionId).
- **Sub-categories** (`backend/subcategory.py`): a layered classifier — a sport-specific overlay
  first, then a universal market-type fallback (Moneyline / Totals / BTTS / Handicap / Outright /
  Prop), then "Outro". Derived from the market slug suffix + title regex.
- **Rollup** (`backend/wallet_report.py`): overall → category → subcategory, each with the 4 metrics.
- **Frontend:** the Polymarket Sports stack (React + Vite + Tailwind + shadcn-style + Recharts +
  TanStack Query). Address input → 4 KPI cards → P&L/win-rate charts → drill-down cards
  (category → subcategory). Dark/light theme.

## Run (dev)

```bash
# 1) Backend
cd polymarket-wallet-dashboard/backend
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --port 8000 --reload          # http://localhost:8000

# 2) Frontend (new shell)
cd polymarket-wallet-dashboard/frontend
npm install
npm run dev                                    # http://localhost:5173  (proxies /api -> :8000)
```

Or `./dev.sh` from `polymarket-wallet-dashboard/` to start both.

In the UI, paste a `0x…` address and click **Analisar**, or click **Ver demo** for sample data.

## API
| Endpoint | Purpose |
|---|---|
| `GET /api/wallet?address=0x…&trade_limit=&enrich_tags=` | Full nested report (overall + by_category + subcategories). `address=demo` serves sample data. |
| `GET /api/health` | Liveness |

## Tests
```bash
cd backend && python test_subcategory.py && python test_wallet_report.py
```
All offline (the demo fixture exercises the full rollup without network).

## Notes
- The Polymarket Data API host is egress-blocked in some sandboxes, and the exact response shapes
  are inferred (see `polymarket-wallet-analyzer/references/data-api.md`); run a real address on a
  networked machine and use `?debug=true` to confirm shapes. The **Ver demo** button works offline.
- Read/analysis only. Not financial advice.
