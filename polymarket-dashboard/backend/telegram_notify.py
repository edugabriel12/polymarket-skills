#!/usr/bin/env python3
"""Telegram delivery for new/upgraded entries.

Message shows the Unidade Sugerida and a LIVE/PRÉ-LIVE flag. It deliberately omits
the wallet and the position size, and carries NO model/wallet origin (entries reach
here already stripped of `source`). Config: TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID.
"""

from __future__ import annotations

import sys

import results_combined as rc

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


def configured() -> bool:
    import telegram_settings as ts
    c = ts.get_config()
    return bool(c["token"] and c["chat_id"])


def send(text: str, *, token: str | None = None, chat_id: str | None = None,
         client=None) -> bool:
    """POST to the Telegram Bot API. Token/chat default to the saved settings (then env).
    Best-effort; False if unconfigured or on failure."""
    if token is None or chat_id is None:
        import telegram_settings as ts
        cfg = ts.get_config()
        token = cfg["token"] if token is None else token
        chat_id = cfg["chat_id"] if chat_id is None else chat_id
    tok, chat = token, chat_id
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


def send_test(*, token: str | None = None, chat_id: str | None = None, client=None) -> bool:
    """Send a fixed connection-test message (used right after saving the config)."""
    msg = ("✅ Polymarket Sports conectado!\n"
           "Você vai receber aqui as entradas com a Unidade Sugerida e LIVE/PRÉ-LIVE.")
    return send(msg, token=token, chat_id=chat_id, client=client)
