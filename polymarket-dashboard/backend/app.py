#!/usr/bin/env python3
"""FastAPI backend for Polymarket Sports — the storefront.

It owns NO model logic and has NO notion of "model" vs "wallet": it just receives
entries via POST /api/copy/ingest from the brain (the wallet-dashboard), shows the
OPEN ones as cards grouped by category, fires a Telegram alert on each new/upgraded
entry, and serves the combined (model + wallets, together) settled results in
Unidade Sugerida. Read/display only.
"""

from __future__ import annotations

import os
import sys

from fastapi import FastAPI, Header, Query
from fastapi.middleware.cors import CORSMiddleware

import entries_store as es
import results_combined as rc
import telegram_notify as tg
import telegram_settings as ts

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_BACKEND_DIR, "..", ".."))


def _load_dotenv() -> list[str]:
    """Load KEY=VALUE from backend/.env, ../.env, or repo .env (real env wins)."""
    loaded = []
    for path in (os.path.join(_BACKEND_DIR, ".env"),
                 os.path.normpath(os.path.join(_BACKEND_DIR, "..", ".env")),
                 os.path.join(_REPO_ROOT, ".env")):
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    if key.strip():
                        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
            loaded.append(path)
        except OSError:
            pass
    return loaded


_DOTENV_FILES = _load_dotenv()
COPY_INGEST_TOKEN = os.environ.get("COPY_INGEST_TOKEN", "")

app = FastAPI(title="Polymarket Sports API", version="3.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "telegram": tg.configured(),
            "ingest_secured": bool(COPY_INGEST_TOKEN), "dotenv_loaded": _DOTENV_FILES}


@app.get("/api/telegram")
def telegram_status() -> dict:
    """Current Telegram config (never returns the token)."""
    cfg = ts.get_config()
    return {"configured": bool(cfg["token"] and cfg["chat_id"]), "chat_id": cfg["chat_id"]}


@app.post("/api/telegram")
def telegram_config(payload: dict) -> dict:
    """Save the bot token, auto-discover the chat id, and fire a test alert."""
    token = (payload.get("token") or "").strip() if isinstance(payload, dict) else ""
    if not token:
        return {"ok": False, "error": "informe o token do bot"}
    chat_id = ts.discover_chat_id(token)
    if not chat_id:
        return {"ok": False, "error": "Nenhuma conversa encontrada. Envie /start ao seu bot "
                "no Telegram e tente de novo."}
    ts.save_config(token, chat_id)
    tested = tg.send_test()
    return {"ok": True, "chat_id": chat_id, "tested": tested,
            "error": None if tested else "Config salva, mas o alerta de teste falhou."}


@app.post("/api/copy/ingest")
def ingest(payload: dict, authorization: str | None = Header(default=None)) -> dict:
    """Receive entries from the brain. Upserts them; fires Telegram on new/upgrade.

    Auth: when COPY_INGEST_TOKEN is set, require 'Authorization: Bearer <token>'.
    """
    if COPY_INGEST_TOKEN:
        token = (authorization or "").removeprefix("Bearer ").strip()
        if token != COPY_INGEST_TOKEN:
            return {"error": "unauthorized"}
    items = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return {"error": "expected {entries: [...]}"}
    counts = {"new": 0, "upgrade": 0, "settled": 0, "unchanged": 0}
    for e in items:
        if not isinstance(e, dict) or not e.get("key"):
            continue
        kind = es.upsert(e)
        counts[kind] = counts.get(kind, 0) + 1
        if kind in ("new", "upgrade"):
            tg.notify_entry(e)
    print(f"[ingest] {len(items)} entr(ies): {counts}", file=sys.stderr, flush=True)
    return {"ingested": len(items), **counts}


@app.get("/api/entries")
def entries(category: str | None = Query(None)) -> dict:
    """OPEN entries grouped by category (the cards). Categories with no open entry
    are absent. Optional ?category= filter."""
    rows = es.list_open()
    if category:
        rows = [r for r in rows if (r.get("category") or "").lower() == category.lower()]
    by_cat: dict[str, list] = {}
    for r in rows:
        by_cat.setdefault(r.get("category") or "Other", []).append(r)
    categories = [{"category": c, "entries": v} for c, v in by_cat.items()]
    categories.sort(key=lambda c: len(c["entries"]), reverse=True)
    return {"n_open": len(rows), "categories": categories}


@app.get("/api/results")
def results() -> dict:
    """Combined settled results (model + wallets together) in Unidade Sugerida."""
    return rc.combined(es.list_settled())


@app.get("/api/results/bets")
def results_bets(category: str | None = Query(None), page: int = Query(1, ge=1),
                 page_size: int = Query(20, ge=1, le=100)) -> dict:
    """Paginated list of the settled bets of a category (the drill-down detail)."""
    offset = (page - 1) * page_size
    return {"total": es.count_settled(category), "page": page, "page_size": page_size,
            "bets": es.list_settled_page(category, offset, page_size)}
