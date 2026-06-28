#!/usr/bin/env python3
"""Authentication primitives for the Sports storefront.

Implements the OWASP checklist from the auth research:
  - Argon2id password hashing (argon2-cffi) with transparent rehash on login.
  - CSPRNG single-use tokens (`secrets`) — only the sha256 is stored; the raw goes in the link.
  - Server-side sessions via an HttpOnly+Secure+SameSite cookie (id regenerated on login).
  - Anti-enumeration: a dummy Argon2 verify for unknown accounts keeps login timing uniform.
  - A small in-process rate limiter for the auth endpoints.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time
from collections import defaultdict

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Request, Response

import users_store as us

_ph = PasswordHasher()  # Argon2id defaults (m=65536 KiB, t=3, p=4)

SESSION_COOKIE = "sps_session"
SESSION_TTL_DAYS = int(os.environ.get("SESSION_TTL_DAYS", "30"))
VERIFY_TTL_HOURS = int(os.environ.get("VERIFY_TTL_HOURS", "24"))
RESET_TTL_HOURS = int(os.environ.get("RESET_TTL_HOURS", "1"))
COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "1") not in ("0", "false", "False", "no", "")

# A fixed valid hash used to equalize login timing when the account doesn't exist
# (so "verify against the real hash" and "verify against this" cost the same).
_DUMMY_HASH = _ph.hash("dummy-password-for-timing-equalization")

PASSWORD_MIN = int(os.environ.get("PASSWORD_MIN_LEN", "8"))   # NIST: 15 sem MFA; 8 é o piso pragmático
PASSWORD_MAX = 128
_COMMON = {
    "12345678", "123456789", "1234567890", "password", "password1", "senha123",
    "qwertyui", "11111111", "00000000", "iloveyou", "abc12345",
}


# --- passwords -------------------------------------------------------------
def hash_password(pw: str) -> str:
    return _ph.hash(pw)


def verify_password(stored_hash: str | None, pw: str) -> tuple[bool, str | None]:
    """(ok, new_hash). ALWAYS runs one Argon2 verify (against a dummy hash when the account
    is unknown) so timing doesn't leak account existence. `new_hash` is set when the stored
    hash's params are outdated and should be re-saved on this successful login."""
    target = stored_hash or _DUMMY_HASH
    try:
        _ph.verify(target, pw)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return (False, None)
    if stored_hash is None:                      # password matched the dummy? impossible, but be safe
        return (False, None)
    new_hash = None
    try:
        if _ph.check_needs_rehash(stored_hash):
            new_hash = _ph.hash(pw)
    except Exception:  # noqa: BLE001
        new_hash = None
    return (True, new_hash)


def password_problem(pw: str, confirm: str) -> str | None:
    """Return a human message if the password is unacceptable, else None (NIST 800-63B-aligned)."""
    if pw != confirm:
        return "As senhas não coincidem."
    if len(pw) < PASSWORD_MIN:
        return f"A senha deve ter pelo menos {PASSWORD_MIN} caracteres."
    if len(pw) > PASSWORD_MAX:
        return f"A senha deve ter no máximo {PASSWORD_MAX} caracteres."
    if pw.lower() in _COMMON:
        return "Senha muito comum — escolha outra."
    return None


# --- tokens / hashing ------------------------------------------------------
def hash_token(raw: str) -> str:
    """sha256 hex of a raw secret (session id or e-mail token) — what we persist."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def new_token() -> tuple[str, str]:
    """(raw, hash). The raw goes in the e-mail link; only the hash is stored."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_token(raw)


# --- sessions --------------------------------------------------------------
def issue_session(user_id: int, response: Response, db_path: str = us.DEFAULT_DB) -> str:
    """Create a fresh server-side session (id regenerated every login) and set the cookie."""
    raw = secrets.token_urlsafe(32)
    us.create_session(user_id, hash_token(raw), ttl_days=SESSION_TTL_DAYS, db_path=db_path)
    response.set_cookie(SESSION_COOKIE, raw, max_age=SESSION_TTL_DAYS * 86400,
                        httponly=True, secure=COOKIE_SECURE, samesite="lax", path="/")
    return raw


def clear_session(request: Request, response: Response, db_path: str = us.DEFAULT_DB) -> None:
    raw = request.cookies.get(SESSION_COOKIE)
    if raw:
        us.delete_session(hash_token(raw), db_path=db_path)
    response.delete_cookie(SESSION_COOKIE, path="/")


def current_user(request: Request, db_path: str = us.DEFAULT_DB) -> dict | None:
    """The logged-in user from the session cookie, or None. Slides the expiry on each hit."""
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    h = hash_token(raw)
    sess = us.get_valid_session(h, db_path=db_path)
    if not sess:
        return None
    us.touch_session(h, ttl_days=SESSION_TTL_DAYS, db_path=db_path)
    return us.get_user_by_id(sess["user_id"], db_path=db_path)


# --- rate limiting (in-process; per-instance) ------------------------------
_ATTEMPTS: dict[str, list[float]] = defaultdict(list)


def rate_limited(key: str, limit: int, window_s: int) -> bool:
    """Record an attempt for `key` and return True if it now exceeds `limit` within `window_s`.
    In-process only — a multi-instance deploy would need a shared store (e.g. Redis)."""
    now = time.monotonic()
    bucket = _ATTEMPTS[key]
    cutoff = now - window_s
    bucket[:] = [t for t in bucket if t > cutoff]
    bucket.append(now)
    return len(bucket) > limit


# --- verification-resend throttle: cooldown + capped retries (in-process) ---
RESEND_COOLDOWN_S = int(os.environ.get("VERIFY_RESEND_COOLDOWN_S", "60"))  # min gap between resends
RESEND_MAX = int(os.environ.get("VERIFY_RESEND_MAX", "3"))                 # max resends per window
RESEND_RESET_S = int(os.environ.get("VERIFY_RESEND_RESET_S", "3600"))      # window after which it resets

_RESEND: dict[str, list[float]] = defaultdict(list)


def resend_throttle(key: str, *, now: float | None = None) -> dict:
    """Cooldown + capped retries for verification-email resends, keyed per request (email+IP).

    Returns ``{allowed, retry_after, remaining, reason}``:
      - at most ``RESEND_MAX`` resends within a rolling ``RESEND_RESET_S`` window — older attempts
        age out, so the counter RESETS over time and the user can try again later;
      - at least ``RESEND_COOLDOWN_S`` between attempts.
    Records the attempt only when allowed. Applied to EVERY request regardless of whether the
    account exists, so it never leaks account existence (anti-enumeration). In-process only — a
    multi-instance deploy would need a shared store (e.g. Redis). ``now`` is injectable for tests.
    """
    now = time.monotonic() if now is None else now
    hits = _RESEND[key]
    hits[:] = [t for t in hits if t > now - RESEND_RESET_S]    # the reset policy: window slides
    if hits and (now - hits[-1]) < RESEND_COOLDOWN_S:          # still within the 60s cooldown
        return {"allowed": False, "reason": "cooldown",
                "retry_after": int(RESEND_COOLDOWN_S - (now - hits[-1])) + 1,
                "remaining": max(0, RESEND_MAX - len(hits))}
    if len(hits) >= RESEND_MAX:                                # cap reached for this window
        return {"allowed": False, "reason": "max", "remaining": 0,
                "retry_after": max(1, int(RESEND_RESET_S - (now - hits[0])) + 1)}
    hits.append(now)
    return {"allowed": True, "reason": None, "retry_after": RESEND_COOLDOWN_S,
            "remaining": max(0, RESEND_MAX - len(hits))}
