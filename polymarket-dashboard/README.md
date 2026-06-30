# Polymarket Sports — Dashboard (web app)

A modern, colorful UI to interact with the **⚽ Futebol** prediction model (total-goals + BTTS).

- **Soccer** runs the `polymarket-soccer-goals` Dixon-Coles model via subprocess.

The model runs in a subprocess; its predictions live in a dedicated DB.

Relevant env vars (optional): `SOCCER_PREDICTIONS_DB`, `SOCCER_RATINGS_CSV`
(team-ratings CSV passed to the soccer model),
`FOOTBALL_DATA_TOKEN` (football-data.org key — the Resultados tab auto-settles soccer from that
results feed when set, and the soccer model auto-calibrates each league's baseline goals/game from
the current season), `APIFOOTBALL_KEY` (api-sports.io key — automatic attack/defense + baseline for
leagues Club Elo doesn't cover, e.g. Brasileirão Série B), `ODDS_API_KEY` (The Odds API key for the
live sharp anchor), `DASHBOARD_CACHE_DB` (override the day-cache DB location). The backend subprocess
inherits these, so the dashboard uses them automatically. API endpoints take `?sport=soccer`.

Two tabs:

- **Análises** — the day's entry suggestions, rendered as cards with the full model math (soccer:
  λ_home/λ_away, P(Over)/P(Under)/P(BTTS), edge, payout, Kelly size). An in-process scheduler
  recomputes **at the top of each hour**
  (01:00, 02:00, …, **Brasília time** by default) and refreshes the cache (a dedicated
  `analysis_cache` table in the dashboard cache DB), so the panel always shows the latest run —
  no manual recompute button. The whole dashboard clock (today, the hourly tick, timestamps)
  follows `RECALC_TZ` (default `America/Sao_Paulo`). Disable the loop with `AUTO_RECALC=0`.
- **Resultados** — ROI, P&L, win rate for **diário / semanal / mensal**, with charts and a
  recent-predictions table linking to each Polymarket market. **Every visit triggers settlement**,
  moving PENDENTE rows to ACERTO/ERRO. Soccer settles from the football-data.org results feed.

Dark/light theme toggle. Read/analysis only — it never places live trades (paper-first).

## Stack
- **Backend:** FastAPI (`backend/app.py`) driving the soccer model subprocess and its
  stdlib-only prediction store (`soccer_predictions`). The day-cache is a
  dedicated SQLite file (`DASHBOARD_CACHE_DB`).
- **Frontend:** React + Vite + TypeScript + TailwindCSS + shadcn-style components + Recharts +
  TanStack Query + framer-motion + lucide.

## Run (dev)

```bash
# 1) Backend
cd polymarket-dashboard/backend
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --port 8000 --reload          # http://localhost:8000

# 2) Frontend (new shell)
cd polymarket-dashboard/frontend
npm install
npm run dev                                    # http://localhost:5173  (proxies /api -> :8000)
```

Or `./dev.sh` from `webapp/` to start both.

### Windows (PowerShell / IntelliJ)
`dev.sh` is bash-only (and uses POSIX venv paths). On Windows use the PowerShell launcher
instead — it creates the venv, installs deps, and opens the backend + frontend in two windows:

```powershell
# from polymarket-dashboard
powershell -ExecutionPolicy Bypass -File dev.ps1
# or just double-click dev.bat
```

**IntelliJ run configs** ship in `.run/` at the repo root (open the project at the repo root so
they're detected): **Dashboard Frontend (npm dev)**, **Dashboard Backend (uvicorn)**, and the
combined **Dashboard (Full)** which runs both. Run `dev.ps1` once first to bootstrap the backend
`.venv` and `node_modules`; the backend config points at
`backend\.venv\Scripts\python.exe` (re-select the interpreter in the dropdown if your IDE asks).

### Demo data (offline)
Live games/settlement need network (Polymarket + the results feeds). To populate sample soccer
predictions so both tabs are usable offline:

```bash
curl -X POST "http://localhost:8000/api/seed-demo?sport=soccer&reset=true"
```

## API
| Endpoint | Purpose |
|---|---|
| `GET /api/analyses?sport=&date=&force=` | Day's suggestions (cached once/day; `force=true` recomputes) |
| `GET /api/results?sport=` | Runs settlement, then returns daily/weekly/monthly performance + recent |
| `GET /api/calibration?sport=` | Brier / log-loss / reliability + CLV over the shadow log |
| `GET /api/predictions?sport=&status=&date=` | Raw prediction rows |
| `POST /api/cache/clear?sport=&date=` | Delete the analyses cache (all, or one sport/date) |
| `POST /api/seed-demo?sport=&reset=` | Seed sample predictions (soccer demo) |
| `GET /api/health` | Liveness |

### Recompute / clear the cache
The backend recomputes every sport at the top of each UTC hour automatically (`AUTO_RECALC=1`,
the default). The endpoints below still allow a manual one-off if needed:
```bash
# Recompute today and overwrite the cache (one shot):
curl "http://localhost:8000/api/analyses?sport=soccer&force=true"

# Delete the whole analyses cache (next call recomputes):
curl -X POST "http://localhost:8000/api/cache/clear"

# Delete just one sport/day:
curl -X POST "http://localhost:8000/api/cache/clear?sport=soccer&date=2026-06-14"
```
(On Windows PowerShell use `curl.exe`.)

## Tests
- Backend: `cd backend && . .venv/bin/activate && python test_api.py`
- Models: see the skill's `scripts/test_*.py` (soccer in `polymarket-soccer-goals/scripts`).

## Notes
- The sandbox blocks live Polymarket egress and the Chromium download, so screenshots aren't
  captured here; the frontend builds clean (`npm run build`) and the stack serves seeded data.
- Not financial advice. Real trading involves risk of loss.
