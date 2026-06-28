#!/usr/bin/env python3
"""Telegram delivery for new/upgraded entries.

The card header is the LIVE/PRÉ-LIVE flag (🔴/⏳) — NOT the source wallet — followed by the
market, then Lado / Cotação / Unidade sugerida / Confiança / Encerra and a "Ver mercado" link.
It deliberately omits the wallet name and the position size (e.g. how much was invested), and
carries NO model/wallet origin (entries reach here already stripped of `source`). Sent as HTML
so the link renders as clickable text. Config: per-user bot token + chat id.
"""

from __future__ import annotations

import html
import sys

_LIVE_ICON = {"LIVE": "🔴", "PRÉ-LIVE": "⏳"}
_CONF_BARS = {"Alta": "■ ■ ■", "Média": "■ ■", "Baixa": "■"}


def _fmt_unit(u) -> str:
    """1.0 -> '1.0', 0.5 -> '0.5', 0.25 -> '0.25' (keeps at least one decimal)."""
    s = f"{float(u or 0):.2f}".rstrip("0").rstrip(".")
    return s if "." in s else f"{s}.0"


def _fmt_date(s) -> str:
    """An ISO date/datetime -> 'YYYY-MM-DD' (empty when absent)."""
    return str(s)[:10] if s else ""


def format_entry(e: dict) -> str:
    """Build the Telegram card (HTML). Header = LIVE/PRÉ-LIVE flag; no wallet, no position size."""
    live = e.get("live") or "PRÉ-LIVE"
    icon = _LIVE_ICON.get(live, "")
    price = float(e.get("entry_price") or 0)
    odds = float(e.get("odds") or 0)
    conf = str(e.get("confidence") or "")
    lines = [
        f"{icon} <b>{html.escape(live)}</b>",
        f"<b>{html.escape(str(e.get('event', '')))}</b>",
        "",
        f"Lado: <b>{html.escape(str(e.get('side', '')))}</b>",
        f"Cotação: <b>{price * 100:.1f}%</b> (Odd {odds:.2f})",
        f"Unidade sugerida: <b>{_fmt_unit(e.get('unit'))}</b>",
    ]
    if conf:
        lines.append(f"Confiança: {_CONF_BARS.get(conf, '')} {html.escape(conf)}".rstrip())
    date = _fmt_date(e.get("game_start"))
    if date:
        lines.append(f"⏰ Encerra: {date}")
    if e.get("market_url"):
        lines.append(f'<a href="{html.escape(str(e["market_url"]), quote=True)}">🔗 Ver mercado</a>')
    return "\n".join(lines)


def configured() -> bool:
    import telegram_settings as ts
    c = ts.get_config()
    return bool(c["token"] and c["chat_id"])


def send(text: str, *, token: str | None = None, chat_id: str | None = None,
         parse_mode: str | None = None, client=None) -> bool:
    """POST to the Telegram Bot API. Token/chat default to the saved settings (then env).
    `parse_mode` (e.g. 'HTML') is sent only when provided. Best-effort; False if unconfigured
    or on failure."""
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
    payload = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        r = client.post(f"https://api.telegram.org/bot{tok}/sendMessage", json=payload, timeout=8)
        r.raise_for_status()
        return True
    except Exception as e:  # noqa: BLE001 - never break ingest on a Telegram failure
        print(f"[telegram] send failed: {e}", file=sys.stderr, flush=True)
        return False


def notify_entry(e: dict, **kw) -> bool:
    """Send an entry card. Rendered as HTML (so 'Ver mercado' is a clickable link)."""
    kw.setdefault("parse_mode", "HTML")
    return send(format_entry(e), **kw)


def send_test(*, token: str | None = None, chat_id: str | None = None, client=None) -> bool:
    """Send a fixed connection-test message (used right after saving the config)."""
    msg = ("✅ Polymarket Sports conectado!\n"
           "Você vai receber aqui as entradas com a Unidade Sugerida e LIVE/PRÉ-LIVE.")
    return send(msg, token=token, chat_id=chat_id, client=client)
