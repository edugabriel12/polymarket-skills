#!/usr/bin/env python3
"""Tests for the Sports auth feature: register → verify → login (incl. blocked-before-verify),
password reset, session gating, per-user Telegram, and the ingest fan-out. All offline:
e-mail is captured in-memory (dev mode) and Telegram is stubbed."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp()
os.environ["SPORTS_ENTRIES_DB"] = os.path.join(_TMP, "entries.db")
os.environ.pop("RESEND_API_KEY", None)         # dev mode: no real e-mail provider
os.environ.pop("COPY_INGEST_TOKEN", None)      # ingest open in tests
os.environ["SESSION_COOKIE_SECURE"] = "0"      # TestClient speaks http

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fastapi.testclient import TestClient  # noqa: E402
import app as backend  # noqa: E402
import auth as _auth  # noqa: E402
import email_send  # noqa: E402
import users_store as us  # noqa: E402

# Capture the RAW verify/reset tokens (normally delivered only by e-mail). app.py calls
# email_send.send_verification/send_reset via the module, so patching the attrs is enough.
_TOKENS: dict[str, list[str]] = {"verify": [], "reset": []}
email_send.send_verification = lambda to, token, **kw: (_TOKENS["verify"].append(token), True)[1]
email_send.send_reset = lambda to, token, **kw: (_TOKENS["reset"].append(token), True)[1]

_PW = "supersecret123"


def _client() -> TestClient:
    return TestClient(backend.app)


def _register(c: TestClient, email: str, name: str = "User One", pw: str = _PW):
    return c.post("/api/auth/register", json={
        "full_name": name, "email": email, "password": pw, "password_confirm": pw})


def _verified(email: str, name: str = "User One", pw: str = _PW) -> int:
    uid = us.create_user(name, email, _auth.hash_password(pw))
    us.mark_verified(uid)
    return uid


class TestRegisterVerifyLogin(unittest.TestCase):
    def setUp(self):
        _TOKENS["verify"].clear(); _TOKENS["reset"].clear(); _auth._ATTEMPTS.clear()

    def test_register_then_blocked_until_verify_then_login(self):
        c = _client()
        r = _register(c, "alice@example.com").json()
        self.assertTrue(r.get("ok"))
        self.assertIn("message", r)
        self.assertTrue(_TOKENS["verify"])                  # a verification token was issued
        token = _TOKENS["verify"][-1]

        # cannot login before verifying — explicit message + flag
        before = c.post("/api/auth/login",
                        json={"email": "alice@example.com", "password": _PW}).json()
        self.assertIn("error", before)
        self.assertTrue(before.get("needs_verification"))
        self.assertIn("Confirme seu e-mail", before["error"])

        v = c.post("/api/auth/verify", json={"token": token}).json()
        self.assertTrue(v.get("ok"))
        # token is single-use
        self.assertFalse(c.post("/api/auth/verify", json={"token": token}).json().get("ok"))

        li = c.post("/api/auth/login", json={"email": "alice@example.com", "password": _PW}).json()
        self.assertTrue(li.get("ok"))
        self.assertEqual(li["user"]["email"], "alice@example.com")
        self.assertEqual(c.get("/api/me").json()["user"]["email"], "alice@example.com")

    def test_login_wrong_password_is_generic(self):
        c = _client()
        _verified("bob@example.com")
        r = c.post("/api/auth/login",
                   json={"email": "bob@example.com", "password": "totally-wrong-xyz"}).json()
        self.assertEqual(r.get("error"), "E-mail ou senha inválidos.")
        self.assertNotIn("needs_verification", r)           # don't leak that the account exists

    def test_login_unknown_email_is_generic(self):
        c = _client()
        r = c.post("/api/auth/login",
                   json={"email": "ghost@example.com", "password": _PW}).json()
        self.assertEqual(r.get("error"), "E-mail ou senha inválidos.")

    def test_password_mismatch_rejected(self):
        c = _client()
        r = c.post("/api/auth/register", json={
            "full_name": "X", "email": "mismatch@example.com",
            "password": _PW, "password_confirm": "different123"}).json()
        self.assertIn("error", r)
        self.assertFalse(_TOKENS["verify"])                 # nothing issued

    def test_register_anti_enumeration_identical_response(self):
        c = _client()
        r1 = _register(c, "dup@example.com").json()
        r2 = _register(c, "dup@example.com").json()          # same email again
        self.assertEqual(r1, r2)                              # identical generic body


class TestGating(unittest.TestCase):
    def setUp(self):
        _auth._ATTEMPTS.clear()

    def test_protected_routes_401_without_session(self):
        c = _client()
        for path in ("/api/entries", "/api/results", "/api/results/bets", "/api/me",
                     "/api/telegram"):
            self.assertEqual(c.get(path).status_code, 401, path)

    def test_logout_revokes_session(self):
        c = _client()
        _verified("log@example.com")
        c.post("/api/auth/login", json={"email": "log@example.com", "password": _PW})
        self.assertEqual(c.get("/api/me").status_code, 200)
        c.post("/api/auth/logout")
        self.assertEqual(c.get("/api/me").status_code, 401)


class TestReset(unittest.TestCase):
    def setUp(self):
        _TOKENS["verify"].clear(); _TOKENS["reset"].clear(); _auth._ATTEMPTS.clear()

    def test_forgot_reset_single_use_and_invalidates_sessions(self):
        c = _client()
        _verified("rita@example.com")
        c.post("/api/auth/login", json={"email": "rita@example.com", "password": _PW})
        self.assertEqual(c.get("/api/me").status_code, 200)

        fr = c.post("/api/auth/forgot-password", json={"email": "rita@example.com"}).json()
        self.assertTrue(fr.get("ok"))
        self.assertTrue(_TOKENS["reset"])
        token = _TOKENS["reset"][-1]

        rr = c.post("/api/auth/reset-password", json={
            "token": token, "password": "brandnewpass123",
            "password_confirm": "brandnewpass123"}).json()
        self.assertTrue(rr.get("ok"))
        self.assertEqual(c.get("/api/me").status_code, 401)  # old session revoked
        # token single-use
        again = c.post("/api/auth/reset-password", json={
            "token": token, "password": "another-pass-123",
            "password_confirm": "another-pass-123"}).json()
        self.assertFalse(again.get("ok"))
        # new password works
        li = c.post("/api/auth/login",
                    json={"email": "rita@example.com", "password": "brandnewpass123"}).json()
        self.assertTrue(li.get("ok"))

    def test_forgot_unknown_email_is_generic_and_silent(self):
        c = _client()
        r = c.post("/api/auth/forgot-password", json={"email": "nobody@example.com"}).json()
        self.assertTrue(r.get("ok"))                         # generic — no leak
        self.assertFalse(_TOKENS["reset"])                   # no token generated


class TestPerUserTelegramAndFanout(unittest.TestCase):
    def setUp(self):
        _TOKENS["verify"].clear(); _TOKENS["reset"].clear(); _auth._ATTEMPTS.clear()
        self.sent: list[dict] = []
        self._orig_notify = backend.tg.notify_entry
        self._orig_disc = backend.ts.discover_chat_id
        self._orig_test = backend.tg.send_test
        backend.tg.notify_entry = lambda e, **kw: (self.sent.append(kw), True)[1]
        backend.tg.send_test = lambda **kw: True

    def tearDown(self):
        backend.tg.notify_entry = self._orig_notify
        backend.ts.discover_chat_id = self._orig_disc
        backend.tg.send_test = self._orig_test

    def _configure_telegram(self, c: TestClient, email: str, chat: str):
        _verified(email)
        c.post("/api/auth/login", json={"email": email, "password": _PW})
        backend.ts.discover_chat_id = lambda token: chat
        r = c.post("/api/telegram", json={"token": f"bot{chat}"}).json()
        self.assertTrue(r.get("ok"))
        self.assertEqual(r["chat_id"], chat)

    def test_telegram_is_per_user(self):
        ca, cb = _client(), _client()
        self._configure_telegram(ca, "ta@example.com", "1001")
        self._configure_telegram(cb, "tb@example.com", "1002")
        self.assertEqual(ca.get("/api/telegram").json()["chat_id"], "1001")
        self.assertEqual(cb.get("/api/telegram").json()["chat_id"], "1002")

    def test_ingest_fans_out_to_all_configured(self):
        ca, cb, cc = _client(), _client(), _client()
        self._configure_telegram(ca, "fa@example.com", "2001")
        self._configure_telegram(cb, "fb@example.com", "2002")
        _verified("fc@example.com")                          # verified but NO telegram → excluded
        self.sent.clear()
        ing = _client().post("/api/copy/ingest", json={"entries": [{
            "key": "fan1", "event": "X vs Y", "category": "Soccer", "subcategory": "O/U",
            "side": "OVER", "odds": 1.8, "entry_price": 0.55, "unit": 1.0, "confidence": "Alta",
            "live": "PRÉ-LIVE", "market_url": "u", "status": "OPEN"}]})
        self.assertEqual(ing.json()["new"], 1)
        chats = sorted(k.get("chat_id") for k in self.sent)
        self.assertEqual(chats, ["2001", "2002"])           # only the two configured users


if __name__ == "__main__":
    unittest.main(verbosity=2)
