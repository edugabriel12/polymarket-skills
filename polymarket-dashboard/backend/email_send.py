#!/usr/bin/env python3
"""Transactional e-mail (verification + password reset) via Resend's REST API.

Reuses the `requests` dependency already in the backend. If ``RESEND_API_KEY`` is unset it
falls back to DEV mode: the link is printed to stderr so the whole flow works locally without
any credentials. Never raises into the request path — returns False on failure (the caller
still answers with a generic message, so e-mail issues don't leak account existence).

Env: RESEND_API_KEY, EMAIL_FROM, APP_BASE_URL (used to build the links).
"""

from __future__ import annotations

import os
import sys

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "Polymarket Sports <onboarding@resend.dev>")
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:5173")


def verify_link(token: str) -> str:
    return f"{APP_BASE_URL.rstrip('/')}/verify?token={token}"


def reset_link(token: str) -> str:
    return f"{APP_BASE_URL.rstrip('/')}/reset?token={token}"


def _send(to: str, subject: str, html: str, *, client=None) -> bool:
    if not RESEND_API_KEY:
        # DEV fallback: no provider configured — log the message so local testing works.
        print(f"[email:DEV] to={to} | {subject}\n{html}", file=sys.stderr, flush=True)
        return True
    if client is None:
        import requests
        client = requests
    try:
        r = client.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            json={"from": EMAIL_FROM, "to": [to], "subject": subject, "html": html},
            timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:  # noqa: BLE001 — never break the request on an e-mail failure
        print(f"[email] send failed: {e}", file=sys.stderr, flush=True)
        return False


def send_verification(to: str, token: str, *, client=None) -> bool:
    link = verify_link(token)
    html = (
        "<p>Bem-vindo ao <b>Polymarket Sports</b>!</p>"
        "<p>Confirme seu e-mail para ativar sua conta:</p>"
        f'<p><a href="{link}">Confirmar meu e-mail</a></p>'
        f'<p>Ou copie e cole no navegador:<br>{link}</p>'
        "<p>O link expira em 24 horas.</p>")
    return _send(to, "Confirme seu e-mail — Polymarket Sports", html, client=client)


def send_reset(to: str, token: str, *, client=None) -> bool:
    link = reset_link(token)
    html = (
        "<p>Recebemos um pedido para redefinir a senha da sua conta no Polymarket Sports.</p>"
        f'<p><a href="{link}">Redefinir minha senha</a></p>'
        f'<p>Ou copie e cole no navegador:<br>{link}</p>'
        "<p>O link expira em 1 hora. Se não foi você, ignore este e-mail.</p>")
    return _send(to, "Redefinir senha — Polymarket Sports", html, client=client)
