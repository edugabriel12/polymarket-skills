# Polymarket Sports — Dashboard (web app)

A modern, colorful UI to interact with the prediction models, with a **sport toggle**
(⚾ MLB total-runs · ⚽ Futebol total-goals + BTTS). The header switch drives both tabs.

- **MLB** runs in-process (`polymarket-mlb-totals`, Negative Binomial).
- **Soccer** runs the `polymarket-soccer-goals` Dixon-Coles model via subprocess (the two skills
  both define a `data_inputs` module, so importing both in one process would collide). Its
  predictions live in a separate DB.

Relevant env vars (optional): `PREDICTIONS_DB`, `SOCCER_PREDICTIONS_DB`, `SOCCER_RATINGS_CSV`
(team-ratings CSV passed to the soccer model), `FOOTBALL_DATA_TOKEN` (football-data.org key — the
Resultados tab auto-settles soccer from that results feed when set, and the soccer model
auto-calibrates each league's baseline goals/game from the current season), `APIFOOTBALL_KEY`
(api-sports.io key — automatic attack/defense + baseline for leagues Club Elo doesn't cover, e.g.
Brasileirão Série B). The backend subprocess inherits these, so the dashboard uses them automatically.
API endpoints take `?sport=mlb|soccer`.

### Auto-recalc + sharp-close capture (MLB) — built-in per-game scheduler

A game can only be predicted while it's **pregame** (in-progress games are filtered) and its Polymarket
volume builds toward first pitch — so the model is recomputed **~1 hour before each game starts**, not
on a fixed clock. With `ODDS_API_KEY` set, the backend schedules a recompute per game; near-simultaneous
starts are grouped into one **wave** = one Odds-API fetch covering the whole slate (so a block of games
costs a single call, staying well within the free quota). Each wave also snapshots the sharp line into a
season-long CSV that `GET /api/clv` scores recorded entries against. **No manual "Recalcular" button** —
the Análises tab just serves the auto-updated day cache (it shows the last `auto · HH:MM` time).

| Env var | Default | Purpose |
|---|---|---|
| `ODDS_API_KEY` | — | The Odds API key. **Required** to enable auto-recalc + the live sharp anchor. |
| `AUTO_RECALC` | `1` | Set `0` to disable the per-game recompute loop. |
| `WAVE_LEAD_MIN` | `60` | Minutes before first pitch to recompute a game. |
| `WAVE_BUCKET_MIN` | `10` | Merge starts within this window into one wave (one fetch). |
| `WAVE_POLL_SEC` | `300` | How often the loop checks for a due wave (no API cost). |
| `SHARP_CLOSE_CSV` | `<predictions-db dir>/sharp_close.csv` | Where the accumulated sharp lines/closes are written. |

Endpoints: `GET /api/clv?sport=mlb` (avg_CLV / beat_close), `GET /api/analyses?force=true` (hidden manual
recompute), `POST /api/capture-close` (force a snapshot), and `GET /api/health` reports the scheduler
state under `sharp_close`. The API key is never logged. Each wave = ~1 Odds-API call; a daily slate of
~5–8 start-blocks ≈ 150–240 calls/month.

Two tabs:

- **Análises** — the day's Over/Under entry suggestions, rendered as cards with the full NegBin
  math (μ, variance, P(Over)/P(Under), edge, payout, Kelly size). The heavy model calc runs
  **once per day** and is cached (backend `analysis_cache` table) until the next UTC day.
- **Resultados** — ROI, P&L, total/Over/Under win rate for **diário / semanal / mensal**, with
  charts and a recent-predictions table linking to each Polymarket market. **Every visit triggers
  settlement** from the authoritative MLB Stats API final total (a total is only reported once a game
  is Final), moving PENDENTE rows to ACERTO/ERRO. The Polymarket market's closed status is fetched to
  backfill links and is available as an optional extra guard (`require_closed`), off by default.

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
| `POST /api/cache/clear?date=` | Delete the analyses cache (all, or one date) |
| `POST /api/seed-demo?reset=` | Seed sample predictions (demo) |
| `GET /api/health` | Liveness |

### Clear the cache / recompute
```bash
# Recompute today and overwrite the cache (one shot):
curl "http://localhost:8000/api/analyses?force=true"

# Delete the whole analyses cache (next call recomputes):
curl -X POST "http://localhost:8000/api/cache/clear"

# Delete just one day:
curl -X POST "http://localhost:8000/api/cache/clear?date=2026-06-14"
```
In the UI, the **Recalcular** button on the Análises tab does the force-recompute. (On Windows
PowerShell use `curl.exe`.) Without the server, clear it directly:
`python -c "import sqlite3,os;c=sqlite3.connect(os.path.expanduser('~/.polymarket-mlb-totals/predictions.db'));c.execute('DELETE FROM analysis_cache');c.commit()"`

## Tests
- Backend: `cd backend && . .venv/bin/activate && python test_api.py`
- Model/analytics/settlement: see `../scripts/test_analytics.py`, `test_settlement.py`,
  `test_run_distribution.py`, `test_pipeline.py`.

## Notes
- The sandbox blocks live Polymarket/MLB egress and the Chromium download, so screenshots aren't
  captured here; the frontend builds clean (`npm run build`) and the stack serves seeded data.
- Not financial advice. Real trading involves risk of loss.
