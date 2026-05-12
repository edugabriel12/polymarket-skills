"""Shared settings for the dashboard."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path.home() / ".polymarket-paper"

WEATHER_EDGE_DB = DATA_DIR / "weather_edge.db"
PORTFOLIO_DB = DATA_DIR / "portfolio.db"
JSONL_PATH = DATA_DIR / "weather_edge.jsonl"
ADVISOR_REPORTS_DIR = DATA_DIR / "advisor_reports"

# Refresh intervals (seconds) for HTMX polling
REFRESH_KPI_SEC = 10
REFRESH_RECENT_EVENTS_SEC = 5
REFRESH_POSITIONS_SEC = 30

# SSE poll cadence for JSONL tail
SSE_POLL_MS = 500
SSE_INITIAL_TAIL_LINES = 50

# Make sibling skill scripts importable
for sub in ("polymarket-analyzer/scripts", "polymarket-paper-trader/scripts"):
    p = REPO_ROOT / sub
    if str(p) not in sys.path and p.exists():
        sys.path.insert(0, str(p))
