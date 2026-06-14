# MLB Totals — Dashboard (web app)

A modern, colorful UI to interact with the `polymarket-mlb-totals` model. Two tabs:

- **Análises** — the day's Over/Under entry suggestions, rendered as cards with the full NegBin
  math (μ, variance, P(Over)/P(Under), edge, payout, Kelly size). The heavy model calc runs
  **once per day** and is cached (backend `analysis_cache` table) until the next UTC day.
- **Resultados** — ROI, P&L, total/Over/Under win rate for **diário / semanal / mensal**, with
  charts and a recent-predictions table linking to each Polymarket market. **Every visit triggers
  cross-source settlement** (MLB Stats API final total + Polymarket market closed) to move PENDENTE
  rows to ACERTO/ERRO.

Dark/light theme toggle. Read/analysis only — it never places live trades (paper-first).

## Stack
- **Backend:** FastAPI (`backend/app.py`) wrapping the skill's Python (`suggest_totals`,
  `predictions_db`, `analytics`, `settlement`). SQLite at `~/.polymarket-mlb-totals/predictions.db`.
- **Frontend:** React + Vite + TypeScript + TailwindCSS + shadcn-style components + Recharts +
  TanStack Query + framer-motion + lucide.

## Run (dev)

```bash
# 1) Backend
cd polymarket-mlb-totals/webapp/backend
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --port 8000 --reload          # http://localhost:8000

# 2) Frontend (new shell)
cd polymarket-mlb-totals/webapp/frontend
npm install
npm run dev                                    # http://localhost:5173  (proxies /api -> :8000)
```

Or `./dev.sh` from `webapp/` to start both.

### Windows (PowerShell / IntelliJ)
`dev.sh` is bash-only (and uses POSIX venv paths). On Windows use the PowerShell launcher
instead — it creates the venv, installs deps, and opens the backend + frontend in two windows:

```powershell
# from polymarket-mlb-totals\webapp
powershell -ExecutionPolicy Bypass -File dev.ps1
# or just double-click dev.bat
```

**IntelliJ run configs** ship in `.run/` at the repo root (open the project at the repo root so
they're detected): **Dashboard Frontend (npm dev)**, **Dashboard Backend (uvicorn)**, and
**MLB Dashboard (Full)** which runs both. Run `dev.ps1` once first to bootstrap the backend
`.venv` and `node_modules`; the backend config points at
`backend\.venv\Scripts\python.exe` (re-select the interpreter in the dropdown if your IDE asks).

### Demo data (offline)
Live games/settlement need network (Polymarket + MLB Stats API). To populate sample predictions
so both tabs are fully usable offline:

```bash
curl -X POST "http://localhost:8000/api/seed-demo?reset=true"
# or: python ../scripts/seed_demo.py --reset
```

## API
| Endpoint | Purpose |
|---|---|
| `GET /api/analyses?date=&force=` | Day's suggestions (cached once/day; `force=true` recomputes) |
| `GET /api/results` | Runs settlement, then returns daily/weekly/monthly performance + recent |
| `GET /api/predictions?status=&date=` | Raw prediction rows |
| `POST /api/seed-demo?reset=` | Seed sample predictions (demo) |
| `GET /api/health` | Liveness |

## Tests
- Backend: `cd backend && . .venv/bin/activate && python test_api.py`
- Model/analytics/settlement: see `../scripts/test_analytics.py`, `test_settlement.py`,
  `test_run_distribution.py`, `test_pipeline.py`.

## Notes
- The sandbox blocks live Polymarket/MLB egress and the Chromium download, so screenshots aren't
  captured here; the frontend builds clean (`npm run build`) and the stack serves seeded data.
- Not financial advice. Real trading involves risk of loss.
