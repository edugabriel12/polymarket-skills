"""FastAPI app for the Polymarket weather trading dashboard.

Read-only views over portfolio.db + weather_edge.db + weather_edge.jsonl.
Run with:
    uvicorn dashboard.main:app --host 127.0.0.1 --port 8765 --reload
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import (HTMLResponse, PlainTextResponse,
                                RedirectResponse, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import settings as S
from .services import (advisor, analytics, charts, costs, events,
                        portfolio, positions, suggestion_applier)

app = FastAPI(title="Polymarket Weather Dashboard")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")),
          name="static")


def _common_ctx(active_tab: str) -> dict:
    return {"active_tab": active_tab,
            "refresh_kpi_sec": S.REFRESH_KPI_SEC,
            "refresh_events_sec": S.REFRESH_RECENT_EVENTS_SEC,
            "refresh_positions_sec": S.REFRESH_POSITIONS_SEC}


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index():
    return RedirectResponse("/overview")


@app.get("/overview", response_class=HTMLResponse)
def page_overview(request: Request):
    return templates.TemplateResponse(
        request, "overview.html", _common_ctx("overview"))


@app.get("/positions", response_class=HTMLResponse)
def page_positions(request: Request):
    return templates.TemplateResponse(
        request, "positions.html", _common_ctx("positions"))


@app.get("/performance", response_class=HTMLResponse)
def page_performance(request: Request, days: int = 14):
    ctx = _common_ctx("performance")
    ctx["days"] = days
    return templates.TemplateResponse(request, "performance.html", ctx)


@app.get("/events", response_class=HTMLResponse)
def page_events(request: Request):
    return templates.TemplateResponse(
        request, "events.html", _common_ctx("events"))


@app.get("/advisor", response_class=HTMLResponse)
def page_advisor(request: Request):
    ctx = _common_ctx("advisor")
    ctx["kpis"] = advisor.get_summary_kpis()
    ctx["runs"] = advisor.list_runs(limit=50)
    return templates.TemplateResponse(request, "advisor.html", ctx)


@app.get("/api/advisor/runs/{run_id}", response_class=HTMLResponse)
def api_advisor_run(request: Request, run_id: int):
    run = advisor.get_run(run_id)
    applies = advisor.list_applies_for_run(run_id)
    return templates.TemplateResponse(
        request, "partials/advisor_report.html",
        {"run": run, "applies": applies})


@app.post("/api/advisor/apply/{run_id}/{suggestion_id}",
          response_class=HTMLResponse)
def api_advisor_apply(request: Request, run_id: int, suggestion_id: str):
    run = advisor.get_run(run_id)
    if not run or not run.get("payload"):
        return HTMLResponse(
            '<div class="suggestion-card failed">'
            'Run not found or payload missing.</div>',
            status_code=404)
    suggestion = next(
        (s for s in (run["payload"].get("suggestions") or [])
         if s.get("id") == suggestion_id), None)
    if not suggestion:
        return HTMLResponse(
            '<div class="suggestion-card failed">'
            'Suggestion not found in this run.</div>',
            status_code=404)
    applier = suggestion_applier.SuggestionApplier()
    result = applier.apply(run_id=run_id, suggestion=suggestion)
    # Re-render the card with the new applied state
    fresh_applies = advisor.list_applies_for_run(run_id)
    return templates.TemplateResponse(
        request, "partials/suggestion_card.html",
        {"s": suggestion, "applied": fresh_applies.get(suggestion_id),
         "run": run})


@app.get("/costs", response_class=HTMLResponse)
def page_costs(request: Request, days: int = 30):
    ctx = _common_ctx("costs")
    ctx["days"] = days
    return templates.TemplateResponse(request, "costs.html", ctx)


@app.get("/api/costs", response_class=HTMLResponse)
def api_costs(request: Request, days: int = 30):
    s = costs.get_cost_summary(days=days)
    daily_series = costs.get_daily_cost_series(days=days)
    top_reviews = costs.get_top_expensive_reviews(limit=15, days=days)
    advisor_runs = costs.get_advisor_run_history(days=max(days, 90))
    return templates.TemplateResponse(
        request, "partials/costs_body.html",
        {"s": s, "daily_series": daily_series,
         "top_reviews": top_reviews, "advisor_runs": advisor_runs,
         "charts": charts, "days": days})


# ---------------------------------------------------------------------------
# HTMX partials
# ---------------------------------------------------------------------------

@app.get("/api/kpis", response_class=HTMLResponse)
def api_kpis(request: Request):
    try:
        kpis = portfolio.get_kpis()
    except FileNotFoundError:
        kpis = {"error": "portfolio.db not found — bot has not run yet"}
    return templates.TemplateResponse(
        request, "partials/kpi_cards.html", {"k": kpis})


@app.get("/api/recent-events", response_class=HTMLResponse)
def api_recent_events(request: Request, limit: int = 10):
    evs = events.read_recent_events(limit=limit)
    return templates.TemplateResponse(
        request, "partials/recent_events.html", {"events": evs})


@app.get("/api/positions", response_class=HTMLResponse)
def api_positions(request: Request, top: int = 0, sort: str = "entry_id"):
    # Overview's mini-table uses sort=size to show the biggest stakes first;
    # the full Positions page sticks with entry_id (newest first).
    pos = positions.get_open_positions(sort_by=sort)
    if top > 0:
        pos = pos[:top]
    return templates.TemplateResponse(
        request, "partials/positions_table.html",
        {"positions": pos, "show_full": top == 0})


@app.get("/api/cumulative-pnl-chart", response_class=HTMLResponse)
def api_cum_pnl_chart(days: int = 30):
    series = analytics.get_cumulative_pnl_series(days=days)
    return HTMLResponse(charts.cumulative_pnl(series))


@app.get("/api/replay/{entry_id}", response_class=HTMLResponse)
def api_replay(request: Request, entry_id: int):
    md = analytics.replay_entry_md(entry_id)
    return templates.TemplateResponse(
        request, "partials/replay_modal.html",
        {"entry_id": entry_id, "md": md})


# ---------------------------------------------------------------------------
# Performance tab — full payload + individual chart fragments
# ---------------------------------------------------------------------------

@app.get("/api/performance", response_class=HTMLResponse)
def api_performance(request: Request, days: int = 14):
    data = analytics.get_all_analytics(days=days)
    return templates.TemplateResponse(
        request, "partials/performance_body.html",
        {"data": data, "charts": charts, "days": days})


# ---------------------------------------------------------------------------
# Live events — SSE
# ---------------------------------------------------------------------------

@app.get("/sse/events")
async def sse_events():
    async def event_stream():
        try:
            async for ev in events.tail_jsonl():
                yield f"data: {json.dumps(ev, default=str)}\n\n"
        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'err': str(e)})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform",
                 "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )


@app.get("/health", response_class=PlainTextResponse)
def health():
    """Simple liveness check."""
    return "ok"
