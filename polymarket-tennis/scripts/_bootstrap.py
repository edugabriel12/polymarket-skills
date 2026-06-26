#!/usr/bin/env python3
"""Wire sys.path so the tennis skill can import the sibling skills it reuses.

Reused (by import, never modified):
  - category_common  (polymarket-category-watcher) — market discovery
  - scoring / calibration_core / forecast (polymarket-forecasting) — the shared
    pure-stdlib calibrated-forecasting cores (CRPS/Brier/log-loss, ECE/reliability,
    pmf entropy) reused across all sports.
"""

from __future__ import annotations

import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))

_REUSED_SCRIPT_DIRS = (
    os.path.join(_REPO_ROOT, "polymarket-category-watcher", "scripts"),
    os.path.join(_REPO_ROOT, "polymarket-forecasting", "scripts"),
)

# This skill's own scripts dir must win on name collisions, so put it FIRST and
# APPEND the reused dirs after it.
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)
for _d in _REUSED_SCRIPT_DIRS:
    if os.path.isdir(_d) and _d not in sys.path:
        sys.path.append(_d)


def reused_dirs() -> tuple[str, ...]:
    return _REUSED_SCRIPT_DIRS
