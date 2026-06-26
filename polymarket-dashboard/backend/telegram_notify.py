#!/usr/bin/env python3
"""Telegram delivery for new/upgraded entries.

Message shows the Unidade Sugerida and a LIVE/PRÉ-LIVE flag. It deliberately omits
the wallet and the position size, and carries NO model/wallet origin (entries reach
here already stripped of `source`). Config: TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID.
"""

from __future__ import annotations

import os
import sys

import results_combined as rc

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

_LIVE_ICON = {"LIVE": "🔴", "PRÉ-LIVE": "⏳"}


def format_entry(e: dict) -> str:
    unit = rc.unit_label(e.get("unit"))
    live = e.get("live") or "PRÉ-LIVE"
    icon = _LIVE_ICON.get(live, "")
    odds = float(e.get("odds") or 0)
    lines = [
        f"🎯 {e.get('event','')} — {e.get('side','')}",
        f"{e.get('category','')} · {e.get('subcategory','')} · {unit}",
        f"odds {odds:.2f} · {icon} {live}".strip(),
    ]
    if e.get("market_url"):
        lines.append(e["market_url"])
    return "\n".join(x for x in lines if x.strip())


def send(text: str, *, token: str | None = None, chat_id: str | None = None,
         client=None) -> bool:
    """POST to the Telegram Bot API. Best-effort; False if unconfigured or on failure."""
    tok = token if token is not None else TELEGRAM_BOT_TOKEN
    chat = chat_id if chat_id is not None else TELEGRAM_CHAT_ID
    if not tok or not chat:
        print("[telegram] not configured (TELEGRAM_BOT_TOKEN/CHAT_ID) — skipping",
              file=sys.stderr, flush=True)
        return False
    if client is None:
        import requests
        client = requests
    try:
        r = client.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                        json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
                        timeout=8)
        r.raise_for_status()
        return True
    except Exception as e:  # noqa: BLE001 - never break ingest on a Telegram failure
        print(f"[telegram] send failed: {e}", file=sys.stderr, flush=True)
        return False


def notify_entry(e: dict, **kw) -> bool:
    return send(format_entry(e), **kw)
