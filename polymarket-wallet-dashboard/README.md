# Polymarket Wallet Dashboard

A web app (FastAPI + React) that analyzes a wallet's **bet-history CSV** and reports
**Win rate, número de apostas, P&L e ROI** — overall, **por nível de confiança** (Alta / Média /
Baixa), **por categoria** (Futebol, Baseball, Basquete, Combat Sports, Hockey, Tênis, …) and
**por sub-categoria** (Ambas Marcam, Over/Under, Moneyline, Run line, Spread, Vencedor da luta, …).

You **upload a CSV** (the `*_historico.csv` export) instead of typing an address. Read-only, no
private key. Event text is untrusted and only pattern-matched (CLAUDE.md rule #5).

## CSV format
`;`-delimited, Brazilian decimals (`,`), one settled bet per row:
```
Data;Evento;Aposta;Conf.;Odd;Investido;ROI%;Lucro
2026-06-25;"Curaçao vs. Côte d'Ivoire: O/U 3.5";UNDER;Média;1,79;19999,96;78,6;15714,32
```
A "bet" = one CSV row; a win = `Lucro > 0`; ROI = `ΣLucro / ΣInvestido`.

## How it works
- **`backend/csv_parser.py`** — parses the CSV; classifies each event into a **category** via team
  dictionaries (MLB / NBA / WNBA / NHL) + UFC + soccer signals (falling back to the wallet-analyzer
  keyword classifier), and a **sub-category** via the shared market-type classifier.
- **`backend/subcategory.py`** — layered market-type classifier (sport-specific overlay → universal
  fallback → "Outro"), from the event text + the picked side.
- **`backend/wallet_report.py`** — `rollup_csv`: overall + **by_confidence** + by_category (each
  nesting its subcategories AND its own confidence split), every bucket with the 4 metrics.
- **Frontend:** the Polymarket Sports stack (React + Vite + Tailwind + shadcn-style + Recharts).
  CSV dropzone → 4 KPI cards → **"Por confiança"** cards → P&L/win-rate charts → drill-down cards
  (category → subcategories + per-confidence). Dark/light theme.

> An on-chain `GET /api/wallet?address=…` endpoint (the original engine) is still available for
> analyzing a public address directly, but the UI is CSV-first.

## Run (dev)

```bash
# 1) Backend
cd polymarket-wallet-dashboard/backend
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --port 8001 --reload          # http://localhost:8001

# 2) Frontend (new shell)
cd polymarket-wallet-dashboard/frontend
npm install
npm run dev                                    # http://localhost:5174  (proxies /api -> :8001)
```

Or `./dev.sh` from `polymarket-wallet-dashboard/` to start both (macOS/Linux).

### Windows (PowerShell)
`dev.sh` is bash-only and uses POSIX venv paths (`.venv/bin/…`), which don't exist on Windows
(`.venv\Scripts\…`). Use the PowerShell launcher instead — it creates the venv, installs deps, and
opens the backend + frontend in two windows:

```powershell
# from polymarket-wallet-dashboard\
powershell -ExecutionPolicy Bypass -File dev.ps1
# or just double-click dev.bat
```

> **Port note:** the backend runs on **:8001** and the frontend on **:5174** — different from
> `polymarket-dashboard` (:8000 / :5173), so both apps can run simultaneously.

In the UI, drag a `*_historico.csv` onto the dropzone (or **Selecionar CSV**), or click **Ver demo**.

## Configuration (.env) — model keys

The brain (this app) runs the soccer/tennis models and pushes entries to Sports.
Copy `.env.example` to `backend/.env` — it is **auto-loaded at startup** (the real shell
environment always wins over the file). On Windows, just create `backend\.env`.

| Key | Effect |
|---|---|
| `ODDS_API_KEY` | Sharp odds anchor. Without it the model is Elo-only and large edges get capped. Biggest win. |
| `APIFOOTBALL_KEY` | Strength model for club leagues Club Elo lacks (e.g. Série B). |
| `FOOTBALL_DATA_TOKEN` | Results / baseline calibration (`track_soccer.py`). |

Check it loaded via `GET /api/health` → `dotenv_loaded` lists the files read.

## API
| Endpoint | Purpose |
|---|---|
| `POST /api/wallet/csv` (multipart `file`) | Analyze a bet-history CSV → overall + by_confidence + by_category (+ subcategories + per-confidence). |
| `GET /api/csv-demo` | Sample CSV-based report (offline). |
| `GET /api/wallet?address=0x…` | On-chain analysis of a public address (original engine). `address=demo` serves sample data. |
| `GET /api/health` | Liveness |

## Tests
```bash
cd backend && python test_csv_parser.py && python test_subcategory.py && python test_wallet_report.py
```
All offline.

## Notes
- The CSV flow is fully offline. The on-chain `?address=` flow hits the Polymarket Data API, whose
  host is egress-blocked in some sandboxes and whose shapes are inferred (see
  `polymarket-wallet-analyzer/references/data-api.md`).
- Read/analysis only. Not financial advice.
