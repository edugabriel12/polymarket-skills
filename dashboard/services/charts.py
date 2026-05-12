"""Chart builders — return Plotly HTML fragments for HTMX swap-in.

All charts use Plotly's `to_html(include_plotlyjs=False, ...)` so the CDN
script in base.html is loaded once and reused.
"""

from __future__ import annotations

import json
from typing import Optional


_DARK_LAYOUT = {
    "template": "plotly_dark",
    "paper_bgcolor": "#1a1d23",
    "plot_bgcolor": "#1a1d23",
    "font": {"family": "ui-monospace, SFMono-Regular, Menlo, monospace",
             "size": 12, "color": "#cdd6dc"},
    "margin": {"t": 30, "r": 20, "b": 40, "l": 50},
}


def _to_html(fig_data: dict, fig_layout: dict, div_id: str) -> str:
    """Minimal Plotly HTML fragment — avoids the full plotly package dep
    by building the JSON manually. Requires plotly.js loaded globally."""
    layout = {**_DARK_LAYOUT, **fig_layout}
    payload = {"data": fig_data, "layout": layout,
               "config": {"displayModeBar": False, "responsive": True}}
    safe_json = json.dumps(payload).replace("</", "<\\/")
    return (
        f'<div id="{div_id}" style="width:100%;height:100%;min-height:300px"></div>'
        f'<script>(function(){{'
        f'var p = {safe_json};'
        f'Plotly.newPlot("{div_id}", p.data, p.layout, p.config);'
        f'}})();</script>'
    )


def pnl_by_trigger(triggers: dict, div_id: str = "chart_pnl_trigger") -> str:
    """Horizontal bar — realized P&L per trigger."""
    if not triggers:
        return '<div class="empty-chart">No cashouts yet in this window.</div>'
    labels = list(triggers.keys())
    values = [triggers[k]["total_pnl_usd"] for k in labels]
    colors = ["#4ade80" if v >= 0 else "#f87171" for v in values]
    data = [{
        "type": "bar", "orientation": "h",
        "x": values, "y": labels,
        "marker": {"color": colors},
        "text": [f"${v:+.2f}" for v in values], "textposition": "auto",
    }]
    layout = {"title": "Realized P&L by Cashout Trigger",
              "xaxis": {"title": "USD"},
              "yaxis": {"title": "", "automargin": True}}
    return _to_html(data, layout, div_id)


def win_rate_by_city(cities: dict, div_id: str = "chart_city_winrate") -> str:
    """Bar chart — win rate per city, top 10 by sample size."""
    if not cities:
        return '<div class="empty-chart">No closed positions yet.</div>'
    items = [(c, v) for c, v in cities.items() if v["n"] >= 2]
    items.sort(key=lambda kv: kv[1]["n"], reverse=True)
    items = items[:15]
    if not items:
        return '<div class="empty-chart">Not enough samples per city yet.</div>'
    labels = [c for c, _ in items]
    win_rates = [(v["win_rate"] or 0) * 100 for _, v in items]
    n_samples = [v["n"] for _, v in items]
    colors = ["#4ade80" if r >= 50 else "#f87171" for r in win_rates]
    data = [{
        "type": "bar", "x": labels, "y": win_rates,
        "marker": {"color": colors},
        "text": [f"{r:.0f}% (n={n})" for r, n in zip(win_rates, n_samples)],
        "textposition": "auto",
    }]
    layout = {"title": "Win Rate by City (n ≥ 2)",
              "yaxis": {"title": "Win rate %", "range": [0, 100]},
              "xaxis": {"title": ""}}
    return _to_html(data, layout, div_id)


def judge_calibration(judge: dict,
                      div_id: str = "chart_judge_cal") -> str:
    """Scatter or bar showing judge_prob bucket vs realized win rate."""
    cal = (judge or {}).get("calibration") or {}
    if not cal:
        return '<div class="empty-chart">No judge reviews resolved yet.</div>'
    labels = sorted(cal.keys())
    judge_probs = [cal[k].get("mean_judge_prob", 0) for k in labels]
    actual_rate = [cal[k].get("actual_win_rate", 0) for k in labels]
    n_samples = [cal[k].get("n", 0) for k in labels]
    data = [
        {"type": "bar", "x": labels, "y": [r * 100 for r in actual_rate],
         "name": "Actual %", "marker": {"color": "#60a5fa"},
         "text": [f"n={n}" for n in n_samples], "textposition": "auto"},
        {"type": "scatter", "x": labels, "y": [p * 100 for p in judge_probs],
         "name": "Mean judge prob %", "mode": "lines+markers",
         "line": {"color": "#fbbf24", "width": 2},
         "marker": {"size": 8}},
    ]
    layout = {"title": "Judge Calibration: predicted vs actual",
              "yaxis": {"title": "%", "range": [0, 100]},
              "xaxis": {"title": "Judge probability bucket"},
              "legend": {"orientation": "h", "y": -0.2}}
    return _to_html(data, layout, div_id)


def counterfactual_cumulative(series: list[dict],
                              div_id: str = "chart_cf") -> str:
    """Line — cumulative counterfactual delta. Negative means our cashouts
    left money on the table vs holding to resolution."""
    if not series:
        return '<div class="empty-chart">No counterfactual data yet.</div>'
    x = [p["ts"][:10] for p in series]
    y = [p["cumulative"] for p in series]
    data = [{
        "type": "scatter", "mode": "lines+markers",
        "x": x, "y": y,
        "line": {"color": "#a78bfa", "width": 2},
        "marker": {"size": 6},
        "fill": "tozeroy", "fillcolor": "rgba(167,139,250,0.15)",
    }]
    layout = {
        "title": "Cumulative counterfactual delta (cashouts vs hold)",
        "yaxis": {"title": "USD (neg = cashouts hurt)"},
        "xaxis": {"title": ""},
    }
    return _to_html(data, layout, div_id)


def cumulative_pnl(series: list[dict],
                   div_id: str = "chart_cum_pnl") -> str:
    """Cumulative realized P&L over time."""
    if not series:
        return '<div class="empty-chart">No realized P&L yet.</div>'
    x = [p["date"] for p in series]
    y = [p["cumulative_pnl"] for p in series]
    final_color = "#4ade80" if (y[-1] if y else 0) >= 0 else "#f87171"
    data = [{
        "type": "scatter", "mode": "lines",
        "x": x, "y": y,
        "line": {"color": final_color, "width": 2.5},
        "fill": "tozeroy",
        "fillcolor": "rgba(74,222,128,0.12)" if (y[-1] if y else 0) >= 0 else "rgba(248,113,113,0.12)",
    }]
    layout = {"title": "Cumulative realized P&L",
              "yaxis": {"title": "USD"},
              "xaxis": {"title": ""}}
    return _to_html(data, layout, div_id)


# ---------------------------------------------------------------------------
# Inline tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Empty cases
    for fn, args in [
        (pnl_by_trigger, ({},)),
        (win_rate_by_city, ({},)),
        (judge_calibration, ({},)),
        (counterfactual_cumulative, ([],)),
        (cumulative_pnl, ([],)),
    ]:
        html = fn(*args)
        assert "empty-chart" in html, f"{fn.__name__} empty case"
    print("Test 1 PASS: all charts handle empty input")

    # Populated cases
    triggers = {
        "profit_lock": {"n": 5, "total_pnl_usd": 35.0, "mean_pnl_usd": 7.0,
                        "min_pnl_usd": 2.0, "max_pnl_usd": 12.0},
        "trailing_stop": {"n": 3, "total_pnl_usd": -8.0, "mean_pnl_usd": -2.7,
                          "min_pnl_usd": -5.0, "max_pnl_usd": 1.0},
    }
    html = pnl_by_trigger(triggers)
    assert "profit_lock" in html and "Plotly.newPlot" in html
    print(f"Test 2 PASS: pnl_by_trigger renders Plotly call ({len(html)} chars)")

    cities = {"Tokyo": {"n": 12, "win_rate": 0.75},
              "Manhattan": {"n": 10, "win_rate": 0.20},
              "SmallCity": {"n": 1, "win_rate": 1.0}}
    html = win_rate_by_city(cities)
    assert "Tokyo" in html and "Manhattan" in html
    assert "SmallCity" not in html  # filtered by n>=2
    print("Test 3 PASS: win_rate_by_city filters n<2")

    judge = {"calibration": {
        "0.0-0.2": {"mean_judge_prob": 0.10, "actual_win_rate": 0.15, "n": 12},
        "0.6-0.8": {"mean_judge_prob": 0.70, "actual_win_rate": 0.65, "n": 8},
    }}
    html = judge_calibration(judge)
    assert "Plotly.newPlot" in html
    print("Test 4 PASS: judge_calibration renders")

    cf = [{"ts": "2026-05-01T00:00:00Z", "cumulative": 5.0},
          {"ts": "2026-05-02T00:00:00Z", "cumulative": 8.0},
          {"ts": "2026-05-03T00:00:00Z", "cumulative": 6.0}]
    html = counterfactual_cumulative(cf)
    assert "Plotly.newPlot" in html and "2026-05-01" in html
    print("Test 5 PASS: counterfactual_cumulative")

    pnl = [{"date": "2026-05-01", "cumulative_pnl": 10.0},
           {"date": "2026-05-02", "cumulative_pnl": 25.0}]
    html = cumulative_pnl(pnl)
    assert "Plotly.newPlot" in html and "25.0" in html or "25" in html
    print("Test 6 PASS: cumulative_pnl")

    print("\nAll charts tests PASS")
