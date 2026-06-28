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
import traceback

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import auth
import email_send
import entries_store as es
import results_combined as rc
import telegram_notify as tg
import telegram_settings as ts
import users_store as us

try:
    from email_validator import EmailNotValidError, validate_email
except ImportError:  # email-validator is a new dep; fail loudly only when register is hit
    validate_email = None
    EmailNotValidError = Exception

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


@app.exception_handler(Exception)
async def _log_unhandled(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all for any unhandled error: full traceback to stderr + error in the 500 body."""
    traceback.print_exc(file=sys.stderr)
    print(f"[api] ERROR {request.method} {request.url.path}: {type(exc).__name__}: {exc}",
          file=sys.stderr, flush=True)
    return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})


@app.middleware("http")
async def _log_requests(request: Request, call_next):
    """One line per request; flags non-2xx so a failing flow is obvious in the log."""
    resp = await call_next(request)
    if resp.status_code >= 400:
        print(f"[api] {resp.status_code} {request.method} {request.url.path}",
              file=sys.stderr, flush=True)
    return resp


# ---------------------------------------------------------------------------
# Auth helpers (server-side session cookie). The whole dashboard is gated; entries
# and results stay SHARED across users, but only a logged-in user can read them.
# ---------------------------------------------------------------------------
def _require_user(request: Request) -> dict:
    """FastAPI dependency: the logged-in user, or HTTP 401."""
    u = auth.current_user(request)
    if not u:
        raise HTTPException(status_code=401, detail="não autenticado")
    return u


def _public_user(u: dict) -> dict:
    """User fields safe to return to the client (never the password hash)."""
    return {"id": u["id"], "full_name": u["full_name"], "email": u["email"],
            "email_verified": bool(u["email_verified"])}


@app.get("/api/health")
def health() -> dict:
    recipients = us.list_telegram_recipients()
    return {"status": "ok", "telegram": len(recipients) > 0,
            "telegram_recipients": len(recipients),
            "ingest_secured": bool(COPY_INGEST_TOKEN), "dotenv_loaded": _DOTENV_FILES}


# ---------------------------------------------------------------------------
# Accounts: register -> verify e-mail -> login. Anti-enumeration throughout
# (generic responses + uniform login timing). Tokens are single-use + hashed.
# ---------------------------------------------------------------------------
_REGISTER_OK = {"ok": True, "message": "Se o e-mail for válido, enviamos um link de "
                "confirmação. Verifique sua caixa de entrada (e o spam)."}
_RESET_OK = {"ok": True, "message": "Se esse e-mail estiver cadastrado, enviamos um link "
             "para redefinir a senha."}


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


@app.post("/api/auth/register")
def register(payload: dict, request: Request) -> dict:
    """Create an unverified account and e-mail a verification link. The response is identical
    whether or not the e-mail already exists (anti-enumeration)."""
    if not isinstance(payload, dict):
        return {"error": "payload inválido"}
    if auth.rate_limited(f"register:{_client_ip(request)}", limit=10, window_s=3600):
        return {"error": "Muitas tentativas. Tente novamente mais tarde."}
    full_name = (payload.get("full_name") or "").strip()
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""
    confirm = payload.get("password_confirm") or ""
    if not full_name:
        return {"error": "Informe seu nome completo."}
    if validate_email is None:
        return {"error": "Servidor sem email-validator instalado."}
    try:
        email = validate_email(email, check_deliverability=False).normalized.lower()
    except EmailNotValidError:
        return {"error": "E-mail inválido."}
    prob = auth.password_problem(password, confirm)
    if prob:
        return {"error": prob}

    existing = us.get_user_by_email(email)
    if existing:
        # Don't reveal the account exists. If it's still unverified, (re)send the link.
        if not existing["email_verified"]:
            us.invalidate_tokens(existing["id"], "verify")
            raw, h = auth.new_token()
            us.create_token(existing["id"], "verify", h, auth.VERIFY_TTL_HOURS)
            email_send.send_verification(email, raw)
        return _REGISTER_OK
    try:
        uid = us.create_user(full_name, email, auth.hash_password(password))
    except Exception:  # noqa: BLE001 — unique-race etc.: behave like "already exists"
        return _REGISTER_OK
    raw, h = auth.new_token()
    us.create_token(uid, "verify", h, auth.VERIFY_TTL_HOURS)
    email_send.send_verification(email, raw)
    return _REGISTER_OK


@app.post("/api/auth/verify")
def verify_email(payload: dict) -> dict:
    """Consume a verification token and activate the account."""
    token = (payload.get("token") or "").strip() if isinstance(payload, dict) else ""
    if not token:
        return {"ok": False, "error": "Token ausente."}
    uid = us.consume_token("verify", auth.hash_token(token))
    if not uid:
        return {"ok": False, "error": "Link inválido ou expirado. Solicite um novo."}
    us.mark_verified(uid)
    return {"ok": True}


@app.post("/api/auth/resend-verification")
def resend_verification(payload: dict, request: Request) -> dict:
    """Re-send the verification link for an unverified account (generic response)."""
    email = (payload.get("email") or "").strip().lower() if isinstance(payload, dict) else ""
    if not auth.rate_limited(f"resend:{email}:{_client_ip(request)}", limit=5, window_s=3600):
        u = us.get_user_by_email(email)
        if u and not u["email_verified"]:
            us.invalidate_tokens(u["id"], "verify")
            raw, h = auth.new_token()
            us.create_token(u["id"], "verify", h, auth.VERIFY_TTL_HOURS)
            email_send.send_verification(email, raw)
    return {"ok": True, "message": "Se houver uma conta não confirmada com esse e-mail, "
            "enviamos um novo link."}


@app.post("/api/auth/login")
def login(payload: dict, request: Request, response: Response) -> dict:
    """Verify credentials, require a confirmed e-mail, then open a server-side session."""
    email = (payload.get("email") or "").strip().lower() if isinstance(payload, dict) else ""
    password = (payload.get("password") or "") if isinstance(payload, dict) else ""
    if auth.rate_limited(f"login:{email}:{_client_ip(request)}", limit=10, window_s=900):
        return {"error": "Muitas tentativas. Aguarde alguns minutos e tente novamente."}
    u = us.get_user_by_email(email)
    ok, new_hash = auth.verify_password(u["password_hash"] if u else None, password)
    if not ok:
        return {"error": "E-mail ou senha inválidos."}            # generic (anti-enumeration)
    if not u["email_verified"]:
        return {"error": "Confirme seu e-mail para ativar a conta. Verifique sua caixa de "
                "entrada.", "needs_verification": True}
    if new_hash:                                                  # transparent Argon2 upgrade
        us.set_password(u["id"], new_hash)
    auth.issue_session(u["id"], response)
    return {"ok": True, "user": _public_user(u)}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response) -> dict:
    auth.clear_session(request, response)
    return {"ok": True}


@app.post("/api/auth/forgot-password")
def forgot_password(payload: dict, request: Request) -> dict:
    """E-mail a reset link if the account exists (generic response either way)."""
    email = (payload.get("email") or "").strip().lower() if isinstance(payload, dict) else ""
    if not auth.rate_limited(f"forgot:{email}:{_client_ip(request)}", limit=5, window_s=3600):
        u = us.get_user_by_email(email)
        if u:
            us.invalidate_tokens(u["id"], "reset")
            raw, h = auth.new_token()
            us.create_token(u["id"], "reset", h, auth.RESET_TTL_HOURS)
            email_send.send_reset(email, raw)
    return _RESET_OK


@app.post("/api/auth/reset-password")
def reset_password(payload: dict) -> dict:
    """Consume a reset token, set the new password, and revoke ALL of the user's sessions."""
    token = (payload.get("token") or "").strip() if isinstance(payload, dict) else ""
    password = (payload.get("password") or "") if isinstance(payload, dict) else ""
    confirm = (payload.get("password_confirm") or "") if isinstance(payload, dict) else ""
    if not token:
        return {"ok": False, "error": "Token ausente."}
    prob = auth.password_problem(password, confirm)
    if prob:
        return {"ok": False, "error": prob}
    uid = us.consume_token("reset", auth.hash_token(token))
    if not uid:
        return {"ok": False, "error": "Link inválido ou expirado. Solicite um novo."}
    us.set_password(uid, auth.hash_password(password))
    us.delete_user_sessions(uid)                                  # force re-login everywhere
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = Depends(_require_user)) -> dict:
    return {"user": _public_user(user)}


@app.get("/api/telegram")
def telegram_status(user: dict = Depends(_require_user)) -> dict:
    """The logged-in user's Telegram config (never returns the token)."""
    cfg = us.get_user_telegram(user["id"])
    return {"configured": bool(cfg["token"] and cfg["chat_id"]), "chat_id": cfg["chat_id"]}


@app.post("/api/telegram")
def telegram_config(payload: dict, user: dict = Depends(_require_user)) -> dict:
    """Save THIS user's bot token, auto-discover the chat id, and fire a test alert."""
    token = (payload.get("token") or "").strip() if isinstance(payload, dict) else ""
    if not token:
        return {"ok": False, "error": "informe o token do bot"}
    chat_id = ts.discover_chat_id(token)
    if not chat_id:
        return {"ok": False, "error": "Nenhuma conversa encontrada. Envie /start ao seu bot "
                "no Telegram e tente de novo."}
    us.set_user_telegram(user["id"], token, chat_id)
    tested = tg.send_test(token=token, chat_id=chat_id)
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
    # Entries are SHARED; Telegram is per-user. Fan out each new/upgrade to every verified
    # user with a configured Telegram (best-effort: one failure never blocks the others).
    recipients = us.list_telegram_recipients()
    counts = {"new": 0, "upgrade": 0, "settled": 0, "unchanged": 0}
    for e in items:
        if not isinstance(e, dict) or not e.get("key"):
            continue
        kind = es.upsert(e)
        counts[kind] = counts.get(kind, 0) + 1
        if kind in ("new", "upgrade"):
            for r in recipients:
                tg.notify_entry(e, token=r["token"], chat_id=r["chat_id"])
    print(f"[ingest] {len(items)} entr(ies): {counts}; fan-out to {len(recipients)} user(s)",
          file=sys.stderr, flush=True)
    return {"ingested": len(items), **counts}


def _guard(name: str, fn):
    """Run a read endpoint, logging any exception (full traceback to stderr) and
    surfacing the error class+message in the HTTP 500 body — so both the server
    log AND the browser console capture exactly what failed."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        print(f"[{name}] ERROR: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}") from e


@app.get("/api/entries")
def entries(category: str | None = Query(None),
            user: dict = Depends(_require_user)) -> dict:
    """OPEN entries grouped by category (the cards), SHARED across users (login required).
    Categories with no open entry are absent. Optional ?category= filter."""
    def _build() -> dict:
        rows = es.list_open()
        if category:
            rows = [r for r in rows if (r.get("category") or "").lower() == category.lower()]
        by_cat: dict[str, list] = {}
        for r in rows:
            by_cat.setdefault(r.get("category") or "Other", []).append(r)
        categories = [{"category": c, "entries": v} for c, v in by_cat.items()]
        categories.sort(key=lambda c: len(c["entries"]), reverse=True)
        print(f"[entries] {len(rows)} open in {len(categories)} categor(ies)",
              file=sys.stderr, flush=True)
        return {"n_open": len(rows), "categories": categories}
    return _guard("entries", _build)


@app.get("/api/results")
def results(user: dict = Depends(_require_user)) -> dict:
    """Combined settled results (model + wallets together) in Unidade Sugerida (login required)."""
    def _build() -> dict:
        settled = es.list_settled()
        out = rc.combined(settled)
        print(f"[results] {len(settled)} settled -> {len(out['by_category'])} categor(ies)",
              file=sys.stderr, flush=True)
        return out
    return _guard("results", _build)


@app.get("/api/results/bets")
def results_bets(category: str | None = Query(None), page: int = Query(1, ge=1),
                 page_size: int = Query(20, ge=1, le=100),
                 user: dict = Depends(_require_user)) -> dict:
    """Paginated list of the settled bets of a category (the drill-down detail; login required)."""
    def _build() -> dict:
        offset = (page - 1) * page_size
        return {"total": es.count_settled(category), "page": page, "page_size": page_size,
                "bets": es.list_settled_page(category, offset, page_size)}
    return _guard("results/bets", _build)
