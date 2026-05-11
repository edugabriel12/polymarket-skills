"""Helpers for weather_strategy_advisor.py.

All functions read-only. Provides:
  - collect_extras: parser confidence, observed MAE per metric, city performance
  - read_current_config: CLI defaults + MAE constants + city count
  - has_new_data_since_last_run: skip if no new resolutions
  - write_advisor_report: persist markdown + JSON sidecar
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "polymarket-analyzer" / "scripts"
HELPERS_PATH = SCRIPTS_DIR / "weather_edge_helpers.py"
BOT_PATH = SCRIPTS_DIR / "weather_edge_bot.py"
CITIES_PATH = REPO_ROOT / "polymarket-analyzer" / "references" / "weather-cities.json"
JUDGE_PROMPT_PATH = REPO_ROOT / "polymarket-analyzer" / "references" / "weather-judge-prompt.md"

REPORTS_DIR = Path.home() / ".polymarket-paper" / "advisor_reports"


def collect_extras(conn: sqlite3.Connection, since_iso: str) -> dict:
    """Aggregate signals not covered by weather_edge_analyzer:
      - parser_confidence histogram (low/med/high counts)
      - observed MAE per threshold_unit (forecast vs realized observed_value)
      - per-city performance (n trades, win rate, mean P&L, mean parser_conf)
    """
    parser_hist = _parser_confidence_hist(conn, since_iso)
    mae_observed = _observed_mae_per_unit(conn, since_iso)
    city_perf = _city_performance(conn, since_iso)
    return {
        "parser_confidence_hist": parser_hist,
        "observed_mae_per_unit": mae_observed,
        "city_performance": city_perf,
    }


def _parser_confidence_hist(conn, since_iso: str) -> dict:
    rows = conn.execute(
        "SELECT parser_confidence FROM entries WHERE ts >= ?",
        (since_iso,),
    ).fetchall()
    buckets = {"high (>=0.9)": 0, "medium (0.7-0.9)": 0,
               "low (<0.7)": 0, "missing": 0}
    for r in rows:
        c = r[0]
        if c is None:
            buckets["missing"] += 1
        elif c >= 0.9:
            buckets["high (>=0.9)"] += 1
        elif c >= 0.7:
            buckets["medium (0.7-0.9)"] += 1
        else:
            buckets["low (<0.7)"] += 1
    return buckets


def _observed_mae_per_unit(conn, since_iso: str) -> dict:
    """Compute |forecast - observed| per threshold_unit using
    entries.forecast_snapshot_json + resolutions.observed_value.

    We extract forecast value from the snapshot using a heuristic that mirrors
    weather_edge_helpers.forecast_probability — for temp markets, look for
    'temp_max' or 'temp' in the daily forecast for entry's target_date.
    Conservative: skip rows without parseable forecast.
    """
    rows = conn.execute(
        "SELECT e.threshold_unit, e.forecast_snapshot_json, e.end_date, "
        "       r.observed_value "
        "FROM entries e JOIN resolutions r ON r.entry_id = e.entry_id "
        "WHERE e.ts >= ? AND r.observed_value IS NOT NULL "
        "  AND e.threshold_unit IS NOT NULL",
        (since_iso,),
    ).fetchall()

    by_unit: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        unit = r[0]
        snapshot = r[1]
        observed = r[3]
        try:
            snap = json.loads(snapshot) if isinstance(snapshot, str) else snapshot
        except Exception:
            continue
        forecast_val = _extract_forecast_value(snap, unit)
        if forecast_val is None:
            continue
        try:
            obs = float(observed)
        except (TypeError, ValueError):
            continue
        by_unit[unit].append(abs(forecast_val - obs))

    out = {}
    for unit, errs in by_unit.items():
        if not errs:
            continue
        out[unit] = {
            "n": len(errs),
            "mae": round(sum(errs) / len(errs), 3),
            "max_err": round(max(errs), 3),
        }
    return out


def _extract_forecast_value(snap: dict, unit: str) -> Optional[float]:
    """Best-effort extraction of forecast scalar from OpenWeather snapshot.
    Returns None if not parseable."""
    if not isinstance(snap, dict):
        return None
    # OpenWeather one-call: snap["daily"][i] = {"temp": {"max": ...}, ...}
    daily = snap.get("daily") or []
    if not daily:
        return None
    day = daily[0] if isinstance(daily, list) and daily else {}
    if unit in ("F", "C"):
        temp = day.get("temp")
        if isinstance(temp, dict):
            return temp.get("max") or temp.get("day")
        return None
    if unit in ("mm", "in"):
        return day.get("rain") or day.get("snow") or 0.0
    if unit.lower() in ("kph", "mph"):
        return day.get("wind_speed")
    return None


def _city_performance(conn, since_iso: str) -> dict:
    rows = conn.execute(
        "SELECT e.city_resolved, e.entry_id, e.parser_confidence, "
        "       r.final_outcome, c.realized_pnl_usd "
        "FROM entries e "
        "LEFT JOIN resolutions r ON r.entry_id = e.entry_id "
        "LEFT JOIN cashouts c ON c.entry_id = e.entry_id "
        "WHERE e.ts >= ? AND e.city_resolved IS NOT NULL "
        "  AND e.status IN ('EXECUTED', 'FAST_PATH')",
        (since_iso,),
    ).fetchall()

    agg: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "wins": 0, "losses": 0,
                 "pnl_usd": 0.0, "parser_conf_sum": 0.0,
                 "parser_conf_n": 0})
    for r in rows:
        city = r[0]
        a = agg[city]
        a["n"] += 1
        if r[2] is not None:
            a["parser_conf_sum"] += float(r[2])
            a["parser_conf_n"] += 1
        if r[3] == "YES" or r[3] == "NO":
            # Won if outcome matches the side they bought
            # Without side info here we approximate: any cashout pnl > 0 is win
            pass
        if r[4] is not None:
            pnl = float(r[4])
            a["pnl_usd"] += pnl
            if pnl > 0:
                a["wins"] += 1
            elif pnl < 0:
                a["losses"] += 1

    out = {}
    for city, v in agg.items():
        decided = v["wins"] + v["losses"]
        out[city] = {
            "n": v["n"],
            "wins": v["wins"],
            "losses": v["losses"],
            "win_rate": round(v["wins"] / decided, 3) if decided else None,
            "total_pnl_usd": round(v["pnl_usd"], 2),
            "mean_parser_confidence": (
                round(v["parser_conf_sum"] / v["parser_conf_n"], 3)
                if v["parser_conf_n"] else None
            ),
        }
    return out


def read_current_config() -> dict:
    """Parse current CLI defaults + MAE constants + city count from source.
    Used by the advisor to know what's currently configured."""
    cfg: dict = {"cli_defaults": {}, "mae_constants": {}, "cities_count": 0,
                 "judge_prompt_excerpt": ""}

    # MAE constants from weather_edge_helpers.py
    if HELPERS_PATH.exists():
        text = HELPERS_PATH.read_text()
        for name in ("MAE_TEMP_F", "MAE_TEMP_C", "MAE_PRECIP_MM", "MAE_WIND_KPH"):
            m = re.search(rf"^{name}\s*=\s*([0-9.]+)", text, re.MULTILINE)
            if m:
                cfg["mae_constants"][name] = float(m.group(1))

    # CLI defaults from weather_edge_bot.py
    if BOT_PATH.exists():
        text = BOT_PATH.read_text()
        for flag in ("--min-edge-pp", "--min-price", "--max-price",
                     "--profit-lock-pp", "--trailing-drawdown-pct",
                     "--convergence-pp", "--fast-path-ttr-min"):
            # Match: p.add_argument("--flag", ..., default=VALUE
            pat = (rf'add_argument\(\s*["\']{re.escape(flag)}["\'][^)]*?'
                   r'default\s*=\s*([0-9.\-]+)')
            m = re.search(pat, text)
            if m:
                cfg["cli_defaults"][flag] = float(m.group(1))

    # Cities count
    if CITIES_PATH.exists():
        try:
            cities = json.loads(CITIES_PATH.read_text())
            if isinstance(cities, dict):
                cfg["cities_count"] = sum(
                    len(v) if isinstance(v, list) else 0
                    for v in cities.values()
                )
            elif isinstance(cities, list):
                cfg["cities_count"] = len(cities)
        except Exception:
            cfg["cities_count"] = -1

    # Judge prompt excerpt (first 800 chars for brevity)
    if JUDGE_PROMPT_PATH.exists():
        cfg["judge_prompt_excerpt"] = JUDGE_PROMPT_PATH.read_text()[:800]

    return cfg


def has_new_data_since_last_run(conn: sqlite3.Connection) -> bool:
    """True if any new resolution/cashout/entry exists since the most recent
    successful advisor_run.ts. Always True if no prior run exists."""
    row = conn.execute(
        "SELECT MAX(ts) FROM advisor_runs WHERE status = 'ok'"
    ).fetchone()
    last_ts = row[0] if row else None
    if not last_ts:
        return True
    new_resolutions = conn.execute(
        "SELECT COUNT(*) FROM resolutions WHERE ts_resolved > ?", (last_ts,)
    ).fetchone()[0]
    new_cashouts = conn.execute(
        "SELECT COUNT(*) FROM cashouts WHERE ts > ?", (last_ts,)
    ).fetchone()[0]
    new_entries = conn.execute(
        "SELECT COUNT(*) FROM entries WHERE ts > ?", (last_ts,)
    ).fetchone()[0]
    return (new_resolutions + new_cashouts + new_entries) > 0


def write_advisor_report(payload: dict, since_iso: str,
                         analyzer_md: str) -> tuple[Path, Path]:
    """Persist markdown report + JSON sidecar. Returns (md_path, json_path)."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # If multiple runs same day, suffix with timestamp
    base = REPORTS_DIR / f"{today}_strategy_report"
    md_path = Path(f"{base}.md")
    json_path = Path(f"{base}.json")
    if md_path.exists():
        ts_suffix = datetime.now(timezone.utc).strftime("%H%M%S")
        md_path = Path(f"{base}_{ts_suffix}.md")
        json_path = Path(f"{base}_{ts_suffix}.json")

    md = _format_markdown(payload, since_iso, analyzer_md)
    md_path.write_text(md)
    json_path.write_text(json.dumps(payload, indent=2, default=str))
    return md_path, json_path


def _format_markdown(payload: dict, since_iso: str, analyzer_md: str) -> str:
    out = [
        "# Weather Edge — Weekly Strategy Advisor Report",
        "",
        f"_Generated {datetime.now(timezone.utc).isoformat()}_  ",
        f"_Window analyzed: since {since_iso}_  ",
        f"_Trades analyzed: {payload.get('n_trades_analyzed', 'n/a')}_",
        "",
        "## Executive Summary",
        "",
        payload.get("summary", "_(no summary)_"),
        "",
        "## Suggestions",
        "",
    ]
    suggestions = payload.get("suggestions", [])
    if not suggestions:
        out.append("_No actionable suggestions this run. See research notes below._")
    for s in suggestions:
        out.append(f"### [{s.get('priority', '?').upper()}] {s.get('title', '(no title)')}")
        out.append("")
        out.append(f"- **Category**: `{s.get('category', '?')}`  "
                   f"**Confidence**: {s.get('confidence', '?')}")
        if "current_value" in s and "proposed_value" in s:
            out.append(f"- **Change**: `{s.get('param_path', '?')}`: "
                       f"`{s['current_value']}` → `{s['proposed_value']}`")
        elif "param_path" in s:
            out.append(f"- **Target**: `{s['param_path']}`")
        out.append(f"- **Rationale**: {s.get('rationale', '')}")
        if s.get("counterfactual"):
            out.append(f"- **Counterfactual**: {s['counterfactual']}")
        if s.get("supporting_data"):
            out.append(f"- **Supporting data**: `{json.dumps(s['supporting_data'])}`")
        if s.get("web_citations"):
            out.append("- **Web citations**:")
            for cit in s["web_citations"]:
                out.append(f"  - <{cit.get('url', '')}> — {cit.get('snippet', '')}")
        out.append("")

    if payload.get("research_notes"):
        out.append("## Research Notes")
        out.append("")
        out.append(payload["research_notes"])
        out.append("")

    out.append("## Source Analyzer Report")
    out.append("")
    out.append("<details><summary>Click to expand the underlying analyzer report</summary>")
    out.append("")
    out.append(analyzer_md)
    out.append("")
    out.append("</details>")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Inline tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    import tempfile

    # Test read_current_config
    cfg = read_current_config()
    assert "cli_defaults" in cfg and "mae_constants" in cfg
    assert "--profit-lock-pp" in cfg["cli_defaults"], cfg["cli_defaults"]
    assert cfg["cli_defaults"]["--profit-lock-pp"] == 50.0, cfg["cli_defaults"]
    assert "MAE_TEMP_F" in cfg["mae_constants"], cfg["mae_constants"]
    assert cfg["cities_count"] > 0
    print(f"Test 1 PASS: read_current_config — cli_defaults has "
          f"{len(cfg['cli_defaults'])} flags, MAE_TEMP_F={cfg['mae_constants'].get('MAE_TEMP_F')}, "
          f"cities={cfg['cities_count']}")

    # Test has_new_data + collect_extras with synthetic DB
    tmp = tempfile.mkdtemp()
    db_path = Path(tmp) / "test.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # Minimal schema: entries, resolutions, cashouts, advisor_runs
    conn.executescript("""
        CREATE TABLE entries (entry_id INTEGER PRIMARY KEY, ts TEXT,
            parser_confidence REAL, city_resolved TEXT, threshold_unit TEXT,
            forecast_snapshot_json TEXT, status TEXT, end_date TEXT);
        CREATE TABLE resolutions (resolution_id INTEGER PRIMARY KEY,
            entry_id INTEGER, ts_resolved TEXT, final_outcome TEXT,
            observed_value REAL);
        CREATE TABLE cashouts (cashout_id INTEGER PRIMARY KEY,
            entry_id INTEGER, ts TEXT, realized_pnl_usd REAL);
        CREATE TABLE advisor_runs (run_id INTEGER PRIMARY KEY, ts TEXT,
            status TEXT);
    """)
    # Insert: 3 entries in São Paulo (1 win $5, 1 loss -$3, 1 still open)
    conn.execute(
        "INSERT INTO entries VALUES (1, '2026-05-01T00:00:00Z', 0.95, "
        "'São Paulo', 'C', '{\"daily\":[{\"temp\":{\"max\":24.0}}]}', "
        "'EXECUTED', '2026-05-02T00:00:00Z')")
    conn.execute(
        "INSERT INTO entries VALUES (2, '2026-05-02T00:00:00Z', 0.80, "
        "'São Paulo', 'C', '{\"daily\":[{\"temp\":{\"max\":18.0}}]}', "
        "'EXECUTED', '2026-05-03T00:00:00Z')")
    conn.execute(
        "INSERT INTO entries VALUES (3, '2026-05-03T00:00:00Z', 0.60, "
        "'São Paulo', 'C', '{\"daily\":[{\"temp\":{\"max\":22.0}}]}', "
        "'EXECUTED', '2026-05-05T00:00:00Z')")
    conn.execute("INSERT INTO resolutions VALUES (1, 1, '2026-05-02T12:00:00Z', "
                 "'YES', 23.0)")
    conn.execute("INSERT INTO resolutions VALUES (2, 2, '2026-05-03T12:00:00Z', "
                 "'NO', 20.0)")
    conn.execute("INSERT INTO cashouts VALUES (1, 1, '2026-05-02T13:00:00Z', 5.0)")
    conn.execute("INSERT INTO cashouts VALUES (2, 2, '2026-05-03T13:00:00Z', -3.0)")
    conn.commit()

    extras = collect_extras(conn, "2026-04-01T00:00:00Z")
    assert extras["parser_confidence_hist"]["high (>=0.9)"] == 1
    assert extras["parser_confidence_hist"]["medium (0.7-0.9)"] == 1
    assert extras["parser_confidence_hist"]["low (<0.7)"] == 1
    assert "C" in extras["observed_mae_per_unit"]
    # MAE: |24-23|=1, |18-20|=2 → mean 1.5
    assert extras["observed_mae_per_unit"]["C"]["mae"] == 1.5, extras["observed_mae_per_unit"]
    assert extras["observed_mae_per_unit"]["C"]["n"] == 2
    sp = extras["city_performance"]["São Paulo"]
    assert sp["n"] == 3 and sp["wins"] == 1 and sp["losses"] == 1
    assert sp["total_pnl_usd"] == 2.0
    print(f"Test 2 PASS: collect_extras — São Paulo {sp}, MAE_C "
          f"{extras['observed_mae_per_unit']['C']}")

    # has_new_data_since_last_run: no prior run → True
    assert has_new_data_since_last_run(conn) is True
    print("Test 3 PASS: has_new_data — True when no prior run")

    # Insert a prior run timestamp AFTER all entries; should return False
    conn.execute("INSERT INTO advisor_runs VALUES (1, '2026-12-31T00:00:00Z', 'ok')")
    conn.commit()
    assert has_new_data_since_last_run(conn) is False
    print("Test 4 PASS: has_new_data — False when last run after all data")

    # Insert a new entry and verify True again
    conn.execute(
        "INSERT INTO entries VALUES (4, '2027-01-01T00:00:00Z', 0.95, 'X', 'C', "
        "'{}', 'EXECUTED', '2027-01-02T00:00:00Z')")
    conn.commit()
    assert has_new_data_since_last_run(conn) is True
    print("Test 5 PASS: has_new_data — True after new entry inserted")

    # write_advisor_report smoke
    payload = {
        "n_trades_analyzed": 47,
        "summary": "Test summary",
        "suggestions": [{
            "id": "sug_001", "category": "threshold", "priority": "high",
            "confidence": "high", "title": "Lower profit-lock-pp",
            "current_value": 50, "proposed_value": 35,
            "param_path": "weather_edge_bot.py:--profit-lock-pp",
            "rationale": "Lock too late.",
            "counterfactual": "Delta -$12 over 47 trades.",
            "supporting_data": {"n_samples": 15},
        }],
    }
    # Write to tmp dir to not pollute home
    orig_dir = REPORTS_DIR
    globals()["REPORTS_DIR"] = Path(tmp) / "reports"
    md_p, json_p = write_advisor_report(payload, "2026-04-01T00:00:00Z",
                                         "# fake analyzer report\n\nbody")
    globals()["REPORTS_DIR"] = orig_dir
    assert md_p.exists() and json_p.exists()
    md_text = md_p.read_text()
    assert "Lower profit-lock-pp" in md_text
    assert "Delta -$12" in md_text
    assert "fake analyzer report" in md_text
    json_payload = json.loads(json_p.read_text())
    assert json_payload["suggestions"][0]["title"] == "Lower profit-lock-pp"
    print(f"Test 6 PASS: write_advisor_report — wrote {md_p.name}, "
          f"{len(md_text)} chars md, {len(json_p.read_text())} chars json")

    print("\nAll strategy_advisor_helpers tests PASS")
