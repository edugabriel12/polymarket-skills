#!/usr/bin/env python3
"""Wire sys.path so this skill can import the sibling skills it reuses.

Reused (by import, never modified):
  - category_common  (polymarket-category-watcher)
  - advisor.kelly_half (polymarket-strategy-advisor)
  - execute_paper / paper_engine (polymarket-paper-trader)
  - congruence / forecast / scoring / calibration_core (polymarket-forecasting) — shared cores
"""

from __future__ import annotations

import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))

_REUSED_SCRIPT_DIRS = (
    os.path.join(_REPO_ROOT, "polymarket-category-watcher", "scripts"),
    os.path.join(_REPO_ROOT, "polymarket-strategy-advisor", "scripts"),
    os.path.join(_REPO_ROOT, "polymarket-paper-trader", "scripts"),
    os.path.join(_REPO_ROOT, "polymarket-forecasting", "scripts"),
)

# This skill's own scripts dir must win on name collisions (e.g. its own
# `data_inputs`/`_bootstrap`), so put it FIRST and APPEND the reused dirs after it.
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)
for _d in _REUSED_SCRIPT_DIRS:
    if os.path.isdir(_d) and _d not in sys.path:
        sys.path.append(_d)


def reused_dirs() -> tuple[str, ...]:
    return _REUSED_SCRIPT_DIRS
