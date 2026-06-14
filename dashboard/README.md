# Polymarket Weather Dashboard

Local read-only dashboard (FastAPI + HTMX + Plotly + SSE) for monitoring the
bot/judge/advisor stack. Replaces ad-hoc `portfolio_report.py` / SQL / `Get-Content`
commands with 4 dense, auto-refreshing tabs.

## Install + run

```powershell
# From repo root
pip install -r dashboard\requirements.txt
python -m uvicorn dashboard.main:app --host 127.0.0.1 --port 8765 --reload
```

Open <http://127.0.0.1:8765> in your browser.

> **Note**: use `python -m uvicorn` (not bare `uvicorn`) on Windows so it doesn't depend on `Scripts\` being in your `PATH`.

## Tabs

| Tab | What you see |
|---|---|
| **Overview** | 4 KPI cards (portfolio, open positions, realized P&L today, drawdown), cumulative P&L chart, last 10 events, top 5 open positions |
| **Positions** | All open positions with trigger-progress bars (P / T / C) per row + 🔥 indicator if a trigger would fire on next monitor check. Click any row → modal with `replay_entry` markdown |
| **Performance** | Period selector (7/14/30/90/all days) → 4 Plotly charts (P&L by trigger, win rate by city, judge calibration, counterfactual delta over time) + 3 detail tables |
| **Live Events** | SSE stream of `~/.polymarket-paper/weather_edge.jsonl` with client-side filters by event_type, level, actor |

## Data sources (all read-only)

- `~/.polymarket-paper/portfolio.db` — paper engine portfolio (cash, positions, trades, daily_snapshots)
- `~/.polymarket-paper/weather_edge.db` — bot state (entries, monitor_checks, cashouts, resolutions, counterfactuals, judge_reviews, advisor_runs)
- `~/.polymarket-paper/weather_edge.jsonl` — append-only event log

SQLite connections are opened with `?mode=ro`. Zero risk of corrupting the bot's state while it runs in parallel.

## Tuning

Edit `dashboard/settings.py`:

```python
REFRESH_KPI_SEC = 10            # KPI auto-refresh
REFRESH_RECENT_EVENTS_SEC = 5   # Recent-events list refresh
REFRESH_POSITIONS_SEC = 30      # Open-positions table refresh
SSE_POLL_MS = 500               # JSONL tail polling cadence
SSE_INITIAL_TAIL_LINES = 50     # Backfill on SSE connect
```

## Run the bot AND the dashboard together

```powershell
# Terminal 1: judge
python polymarket-analyzer\scripts\weather_edge_judge.py

# Terminal 2: bot daemon
python polymarket-analyzer\scripts\weather_edge_bot.py --daemon --min-edge-pp 25 --log-file bot.jsonl

# Terminal 3: dashboard
uvicorn dashboard.main:app --host 127.0.0.1 --port 8765
```

Dashboard reads from the same files the bot and judge write to — events appear within ~500ms of being logged.

## Tests

Each service file has inline tests. Run individually:

```powershell
python -m dashboard.services.portfolio
python -m dashboard.services.positions
python -m dashboard.services.analytics
python -m dashboard.services.events
python -m dashboard.services.charts
```

End-to-end smoke (requires `httpx`):

```powershell
pip install httpx
python -m pytest dashboard\  # if you add tests later
```

## Troubleshooting

**Dashboard shows empty everywhere**

Check that the bot has run at least once and produced data:
```powershell
@'
import sqlite3, os
db = os.path.expanduser(r"~\.polymarket-paper\weather_edge.db")
for r in sqlite3.connect(db).execute("SELECT status, COUNT(*) FROM entries GROUP BY status"):
    print(r)
'@ | python -
```

**"portfolio.db not found"**

The paper_engine hasn't executed any trades yet. Once the bot executes one
trade, the DB is created and KPI cards populate.

**SSE not connecting**

Verify `~/.polymarket-paper/weather_edge.jsonl` exists. The bot creates it on first
event. If it doesn't exist yet, the SSE endpoint will keep polling until it shows up.

**Charts not rendering**

Plotly comes from `cdn.plot.ly` — needs internet on first load (cached after).
If you need fully offline, change `base.html` to vendor Plotly locally.
