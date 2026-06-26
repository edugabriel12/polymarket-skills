#!/usr/bin/env python3
"""The brain's active cycles: run the models (hourly) and poll the watched wallets,
pushing every resulting entry to Polymarket Sports.

Both cycles funnel through the same push (push_client → Sports /api/copy/ingest),
so model entries (1U/PRÉ-LIVE) and wallet entries (unit by confidence) travel the
exact same pipeline and Sports can't tell them apart.
"""

from __future__ import annotations

import sys

import model_runner
import push_client
import watcher
import wallet_report as wr


def _vlog(msg: str) -> None:
    print(f"[brain] {msg}", file=sys.stderr, flush=True)


def run_models_once(date: str, *, pusher=None) -> dict:
    """Run soccer+tennis for `date` and push their entries. Returns the push result."""
    pusher = pusher or push_client.push_entries
    entries = model_runner.model_entries(date)
    _vlog(f"models {date}: {len(entries)} entry(ies)")
    return pusher(entries)


def run_watch_once(*, api=None, pusher=None) -> int:
    """Poll every watched wallet once and push new/settled entries. Returns count pushed."""
    api = api or wr.wa.APIClient()
    pusher = pusher or push_client.push_entries
    return watcher.run_once(api, pusher)
