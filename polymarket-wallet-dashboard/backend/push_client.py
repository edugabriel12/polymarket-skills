#!/usr/bin/env python3
"""Push entries (and settlement updates) from the brain (wallet-dashboard) to the
Polymarket Sports ingest endpoint.

Sends the public view of each entry (source stripped — Sports never learns whether
an entry came from the model or a wallet). Authenticated with a shared bearer token.
Best-effort: a Sports outage never breaks detection; failures are logged and skipped.

Config (env):
  SPORTS_INGEST_URL   e.g. http://localhost:8000   (Sports backend base URL)
  COPY_INGEST_TOKEN   shared secret (must match the Sports side)
"""

from __future__ import annotations

import os
import sys

import entries as en

SPORTS_INGEST_URL = os.environ.get("SPORTS_INGEST_URL", "").rstrip("/")
COPY_INGEST_TOKEN = os.environ.get("COPY_INGEST_TOKEN", "")


def _vlog(msg: str) -> None:
    print(f"[push] {msg}", file=sys.stderr, flush=True)


def push_entries(entry_list, *, base_url: str | None = None, token: str | None = None,
                 client=None) -> dict:
    """POST entries to {base_url}/api/copy/ingest. Returns {sent, skipped, error?}.

    `client` is an injectable object with .post(url, json=, headers=, timeout=) (defaults to
    requests). Strips `source` from every entry before sending.
    """
    base = (base_url if base_url is not None else SPORTS_INGEST_URL).rstrip("/")
    tok = token if token is not None else COPY_INGEST_TOKEN
    payload = [en.public_view(e) for e in (entry_list or [])]
    if not payload:
        return {"sent": 0, "skipped": 0}
    if not base:
        _vlog("SPORTS_INGEST_URL not set — skipping push (detection still works)")
        return {"sent": 0, "skipped": len(payload), "error": "no SPORTS_INGEST_URL"}
    if client is None:
        import requests
        client = requests
    headers = {"Authorization": f"Bearer {tok}"} if tok else {}
    try:
        r = client.post(f"{base}/api/copy/ingest", json={"entries": payload},
                        headers=headers, timeout=8)
        r.raise_for_status()
        _vlog(f"sent {len(payload)} entry(ies) to {base}")
        return {"sent": len(payload), "skipped": 0}
    except Exception as e:  # noqa: BLE001 - never break detection on a push failure
        _vlog(f"push failed ({e}); {len(payload)} entry(ies) not delivered")
        return {"sent": 0, "skipped": len(payload), "error": str(e)}
