#!/usr/bin/env python3
"""Wire sys.path so this skill can import the sibling skills it reuses.

The reused scripts use bare sibling imports (e.g. `from category_common import
...`), so their `scripts/` directories must be on sys.path. Importing this
module performs that wiring as a side effect; it does not re-export anything.

Reused:
  - category_common, list_games_today  (polymarket-category-watcher)
  - advisor (kelly_half)               (polymarket-strategy-advisor)
  - execute_paper, paper_engine        (polymarket-paper-trader)
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
)

# Own scripts dir first (so our modules resolve), then the reused dirs.
for _d in (_THIS_DIR, *_REUSED_SCRIPT_DIRS):
    if os.path.isdir(_d) and _d not in sys.path:
        sys.path.insert(0, _d)


def reused_dirs() -> tuple[str, ...]:
    """Return the reused script dirs (for diagnostics)."""
    return _REUSED_SCRIPT_DIRS
