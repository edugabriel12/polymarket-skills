"""FastAPI app for the Polymarket weather trading dashboard.

Read-only views over portfolio.db + weather_edge.db + weather_edge.jsonl.
Run with:
    uvicorn dashboard.main:app --host 127.0.0.1 --port 8765 --reload
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import (HTMLResponse, PlainTextResponse,
                                RedirectResponse, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import settings as S
from .services import (advisor, advisor_jobs, analytics, charts,
                        counterfactual, costs, events, live_trading,
                        notifier, onchain_history, portfolio, positions,
                        process_manager, settings_service,
                        suggestion_applier, wallet)

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
    ctx["acceptance"] = advisor.get_acceptance_stats()
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
def api_advisor_apply(request: Request, run_id: int, suggestion_id: str,
                       auto_restart: bool = False):
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
    result = applier.apply(run_id=run_id, suggestion=suggestion,
                            auto_restart=auto_restart)
    # Re-render the card with the new applied state. Pass restart_info
    # via the suggestion dict so the template can show feedback.
    fresh_applies = advisor.list_applies_for_run(run_id)
    return templates.TemplateResponse(
        request, "partials/suggestion_card.html",
        {"s": suggestion, "applied": fresh_applies.get(suggestion_id),
         "run": run, "restart_info": result.get("restart")})


@app.get("/api/advisor/judge-prompt-editor/{run_id}/{suggestion_id}",
         response_class=HTMLResponse)
def api_judge_prompt_editor(request: Request, run_id: int,
                              suggestion_id: str):
    """Render a modal with current weather-judge-prompt.md content +
    suggestion's rationale/proposed_value as guidance. Operator edits
    the textarea + clicks Save."""
    run = advisor.get_run(run_id)
    suggestion = next(
        (s for s in ((run or {}).get("payload") or {}).get("suggestions") or []
         if s.get("id") == suggestion_id), None)
    if not run or not suggestion:
        return HTMLResponse(
            '<div class="modal-error">Run or suggestion not found.</div>',
            status_code=404)
    current_text = suggestion_applier.JUDGE_PROMPT_MD.read_text(
        encoding="utf-8") if suggestion_applier.JUDGE_PROMPT_MD.exists() else ""
    return templates.TemplateResponse(
        request, "partials/judge_prompt_modal.html",
        {"run": run, "s": suggestion, "current_text": current_text})


@app.post("/api/advisor/apply/{run_id}/{suggestion_id}/judge-prompt",
          response_class=HTMLResponse)
def api_apply_judge_prompt(request: Request, run_id: int,
                              suggestion_id: str,
                              operator_text: str = Form(...),
                              auto_restart: bool = Form(False)):
    """Custom apply path for judge_prompt: receives the full new file
    content from the modal's textarea."""
    run = advisor.get_run(run_id)
    suggestion = next(
        (s for s in ((run or {}).get("payload") or {}).get("suggestions") or []
         if s.get("id") == suggestion_id), None)
    if not run or not suggestion:
        return HTMLResponse(
            '<div class="suggestion-card failed">Run or suggestion missing.</div>',
            status_code=404)
    applier = suggestion_applier.SuggestionApplier()
    result = applier.apply(run_id=run_id, suggestion=suggestion,
                            auto_restart=auto_restart,
                            operator_text=operator_text)
    fresh_applies = advisor.list_applies_for_run(run_id)
    return templates.TemplateResponse(
        request, "partials/suggestion_card.html",
        {"s": suggestion, "applied": fresh_applies.get(suggestion_id),
         "run": run, "restart_info": result.get("restart")})


@app.post("/api/advisor/run-now", response_class=HTMLResponse)
def api_advisor_run_now(request: Request,
                         since_days: int = Form(30),
                         per_trade_limit: int = Form(200)):
    """Spawn the advisor as a detached subprocess + return polling
    partial."""
    job = advisor_jobs.start_job(
        since_days=since_days, per_trade_limit=per_trade_limit)
    return templates.TemplateResponse(
        request, "partials/advisor_job_status.html", {"job": job})


@app.get("/api/advisor/jobs/{job_id}", response_class=HTMLResponse)
def api_advisor_job_status(request: Request, job_id: int):
    job = advisor_jobs.get_job(job_id)
    return templates.TemplateResponse(
        request, "partials/advisor_job_status.html", {"job": job})


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
    # v6: judge accuracy + queue health (best-effort, no-op if no data)
    try:
        judge = portfolio.get_judge_kpis(days=30)
    except Exception as e:
        judge = {"available": False, "reason": str(e)}
    try:
        queue = portfolio.get_queue_health()
    except Exception as e:
        queue = {"available": False, "reason": str(e)}
    return templates.TemplateResponse(
        request, "partials/kpi_cards.html",
        {"k": kpis, "judge": judge, "queue": queue})


# ---------------------------------------------------------------------------
# v6 Tier 4A: Notifications endpoints + crash detection
# ---------------------------------------------------------------------------

@app.post("/api/notify/test", response_class=HTMLResponse)
def api_notify_test(request: Request):
    """Send a test notification — operator wires creds, hits this once
    to confirm Telegram/email works, then trusts the bot to fire alerts."""
    result = notifier.send(
        "info", "Polymarket dashboard test",
        f"This is a test from /api/notify/test at {portfolio._ro_conn.__module__}. "
        f"If you see this, the notifier backend is wired correctly.",
        rate_limit_key="manual_test",
    )
    status_class = "toast-success" if result["status"] == "sent" else "toast-warn"
    msg = (f"Test → {result['status']}"
            + (f": {result.get('reason', '')}" if result.get("reason") else ""))
    return HTMLResponse(
        f'<div class="toast {status_class} toast-show inline">{msg}</div>'
    )


@app.get("/api/notify/status", response_class=HTMLResponse)
def api_notify_status(request: Request):
    s = notifier.get_status()
    return HTMLResponse(
        f'<div class="muted small">Backend: <code>{s["backend"]}</code> · '
        f'configured: <strong>{"yes" if s["configured"] else "no"}</strong> · '
        f'rate-limit window: {s["rate_limit_window_min"]}min · '
        f'recent keys: {len(s["recent_sent_keys"])}</div>'
    )


@app.on_event("startup")
async def _migrate_db_schema():
    """Run weather_edge_db.init_db() at startup so the schema is auto-
    migrated to the latest version. Without this, a dashboard launched
    against a DB last touched by an older bot/judge would crash on
    queries against newer tables (e.g., advisor_suggestion_applies in
    v4, advisor_jobs in v5)."""
    try:
        import sys as _sys
        _analyzer = (Path(__file__).resolve().parent.parent
                      / "polymarket-analyzer" / "scripts")
        if str(_analyzer) not in _sys.path:
            _sys.path.insert(0, str(_analyzer))
        import weather_edge_db
        weather_edge_db.init_db()
        print("[dashboard] weather_edge.db migrated to schema v"
              f"{weather_edge_db.SCHEMA_VERSION}", flush=True)
    except Exception as e:
        # Don't crash dashboard if migration fails — services are
        # defensive against missing tables.
        print(f"[dashboard] warning: schema migration failed: {e}",
              flush=True)


@app.on_event("startup")
async def _start_crash_watcher():
    """Background task: every 60s, check if bot/judge PID files claim a
    process that's no longer alive → notify ONCE per crash event."""
    import asyncio
    async def watch():
        seen_dead = set()
        while True:
            try:
                for target in ("bot", "judge"):
                    pf = process_manager.read_pidfile(target)
                    if pf is None:
                        continue
                    pid = pf.get("pid")
                    if not pid:
                        continue
                    if process_manager.is_alive(pid):
                        seen_dead.discard((target, pid))
                        continue
                    if (target, pid) in seen_dead:
                        continue
                    seen_dead.add((target, pid))
                    notifier.send(
                        "critical", f"{target} process crashed (PID {pid})",
                        f"PID file {target}.pid.json claims pid={pid}, "
                        f"but the process is no longer alive. Check logs at "
                        f"~/.polymarket-paper/{target}.out.log",
                        rate_limit_key=f"crash_{target}_{pid}",
                    )
            except Exception:
                pass
            await asyncio.sleep(60)
    asyncio.create_task(watch())


# ---------------------------------------------------------------------------
# v6 Tier 4B: Counterfactual replay UI
# ---------------------------------------------------------------------------

@app.get("/api/counterfactual/modal/{entry_id}", response_class=HTMLResponse)
def api_counterfactual_modal(request: Request, entry_id: int):
    """Open the counterfactual replay modal for a given entry."""
    initial = counterfactual.replay_with_params(entry_id)
    return templates.TemplateResponse(
        request, "partials/counterfactual_modal.html",
        {"entry_id": entry_id, "initial": initial})


@app.api_route("/api/counterfactual/replay", methods=["GET", "POST"],
                response_class=HTMLResponse)
async def api_counterfactual_replay(request: Request):
    """Re-render the result panel with the current slider values.
    Accepts both GET (initial load with defaults) and POST (form submit)."""
    if request.method == "POST":
        data = await request.form()
    else:
        data = request.query_params
    try:
        entry_id = int(data.get("entry_id"))
    except (TypeError, ValueError):
        return HTMLResponse(
            '<div class="modal-error">Missing entry_id.</div>', status_code=400)

    def _f(name: str, default: float) -> float:
        v = data.get(name)
        if v is None or v == "":
            return default
        try:
            return float(v)
        except ValueError:
            return default

    result = counterfactual.replay_with_params(
        entry_id,
        profit_lock_pp=_f("profit_lock_pp", 50.0),
        trailing_drawdown_pct=_f("trailing_drawdown_pct", 30.0),
        trailing_min_gain_pp=_f("trailing_min_gain_pp", 20.0),
        convergence_pp=_f("convergence_pp", 5.0),
        bid_slippage_pct=_f("bid_slippage_pct", 0.0),
        fee_rate=_f("fee_rate", 0.0),
    )
    return templates.TemplateResponse(
        request, "partials/counterfactual_result.html", {"result": result})


# ---------------------------------------------------------------------------
# Settings + Live trading pages
# ---------------------------------------------------------------------------

@app.get("/settings", response_class=HTMLResponse)
def page_settings(request: Request):
    ctx = _common_ctx("settings")
    ctx["settings"] = settings_service.get_displayable_settings()
    return templates.TemplateResponse(request, "settings.html", ctx)


@app.post("/api/settings/env", response_class=HTMLResponse)
def api_settings_update(request: Request,
                          key: str = Form(...),
                          value: str = Form("")):
    """Update a single env var. Returns the re-rendered settings form."""
    try:
        settings_service.update_env_var(key, value)
        # Re-render the whole form so the row's "Current" column updates
        ctx = {"settings": settings_service.get_displayable_settings()}
        return templates.TemplateResponse(
            request, "partials/settings_form.html", ctx)
    except ValueError as e:
        return HTMLResponse(
            f'<form><div class="modal-error">⚠ {str(e)}</div></form>',
            status_code=400)


@app.get("/live", response_class=HTMLResponse)
def page_live(request: Request):
    ctx = _common_ctx("live")
    return templates.TemplateResponse(request, "live.html", ctx)


@app.get("/api/wallet/balance", response_class=HTMLResponse)
def api_wallet_balance(request: Request, refresh: int = 0):
    w = wallet.get_wallet_info(force_refresh=bool(refresh))
    return templates.TemplateResponse(
        request, "partials/wallet_balance.html", {"w": w})


@app.get("/api/wallet/onchain", response_class=HTMLResponse)
def api_wallet_onchain(request: Request, refresh: int = 0, limit: int = 50):
    """On-chain transaction history via Polygonscan."""
    w = wallet.get_wallet_info()
    if not w.get("configured"):
        return templates.TemplateResponse(
            request, "partials/onchain_history.html",
            {"h": {"configured": False,
                    "reason": w.get("reason", "no wallet")}})
    h = onchain_history.get_transactions(
        w["address"], limit=limit, force_refresh=bool(refresh))
    return templates.TemplateResponse(
        request, "partials/onchain_history.html", {"h": h})


@app.get("/api/live/mode", response_class=HTMLResponse)
def api_live_mode(request: Request):
    mode = live_trading.get_live_mode()
    return templates.TemplateResponse(
        request, "partials/live_mode_banner.html", {"mode": mode})


@app.get("/api/live/badge", response_class=HTMLResponse)
def api_live_badge(request: Request):
    mode = live_trading.get_live_mode()
    return templates.TemplateResponse(
        request, "partials/live_badge.html", {"mode": mode})


@app.get("/api/live/killswitch-status", response_class=HTMLResponse)
def api_killswitch_status(request: Request):
    armed = live_trading.is_killswitch_armed()
    return templates.TemplateResponse(
        request, "partials/killswitch_toggle.html",
        {"armed": armed, "path": str(live_trading._halt_file())})


@app.post("/api/live/killswitch", response_class=HTMLResponse)
def api_killswitch_toggle(request: Request, action: str):
    if action == "arm":
        live_trading.arm_killswitch()
    elif action == "disarm":
        live_trading.disarm_killswitch()
    else:
        return HTMLResponse(
            '<div class="modal-error">action must be arm|disarm</div>',
            status_code=400)
    armed = live_trading.is_killswitch_armed()
    return templates.TemplateResponse(
        request, "partials/killswitch_toggle.html",
        {"armed": armed, "path": str(live_trading._halt_file())})


@app.get("/api/live/readiness", response_class=HTMLResponse)
def api_live_readiness(request: Request, refresh: int = 0):
    r = live_trading.get_readiness(force_refresh=bool(refresh))
    return templates.TemplateResponse(
        request, "partials/live_readiness.html", {"r": r})


@app.get("/api/live/trades", response_class=HTMLResponse)
def api_live_trades(request: Request, limit: int = 50):
    trades = live_trading.read_live_trades(limit=limit)
    return templates.TemplateResponse(
        request, "partials/live_trades_table.html", {"trades": trades})


@app.get("/api/live/daily-spend", response_class=HTMLResponse)
def api_live_daily_spend(request: Request):
    s = live_trading.get_daily_spent_usd()
    return templates.TemplateResponse(
        request, "partials/daily_spend.html", {"s": s})


@app.get("/api/skipped-entries", response_class=HTMLResponse)
def api_skipped_entries(request: Request, limit: int = 20):
    """v6: Recently SKIPPED entries panel for the overview page."""
    skipped = positions.get_recent_skipped(limit=limit)
    return templates.TemplateResponse(
        request, "partials/skipped_entries.html", {"skipped": skipped})


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
