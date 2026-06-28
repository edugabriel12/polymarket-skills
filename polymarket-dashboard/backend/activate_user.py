#!/usr/bin/env python3
"""Activate a Sports account stuck in e-mail verification — ADMIN/DEV override.

The Sports storefront blocks login until the user clicks the e-mail verification link. When that
link can't be delivered or clicked (no ``RESEND_API_KEY`` in dev — the link is only logged to
stderr —, an inaccessible inbox, or during testing), this CLI flips a pending account to
*verified* directly in the DB, bypassing the e-mail step.

Default DB: ``~/.polymarket-dashboard/entries.db`` (override with ``SPORTS_ENTRIES_DB`` or
``--db``) — the same file the backend uses; accounts live in the ``users`` table.

Safe by design: DRY-RUN by default (prints what it WOULD do). Pass ``--apply`` to actually
activate.

    python activate_user.py --list                       # who is pending verification
    python activate_user.py --email foo@bar.com          # dry-run for one account
    python activate_user.py --email foo@bar.com --apply  # activate that account
    python activate_user.py --all --apply                # activate ALL pending accounts
    python activate_user.py --email foo@bar.com --apply --db /path/to/entries.db

Use deliberately: this skips the e-mail ownership check.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import users_store as us  # noqa: E402


def _activate(user: dict, apply: bool, db: str) -> None:
    # `list_unverified` rows omit email_verified (all pending); get_user_by_email includes it.
    if user.get("email_verified"):
        print(f"  já verificada: {user['email']} (#{user['id']}) — nada a fazer")
        return
    if not apply:
        print(f"  [dry-run] ativaria: {user['email']} (#{user['id']})")
        return
    us.mark_verified(user["id"], db)
    us.invalidate_tokens(user["id"], "verify", db)   # drop the now-pointless verification token
    print(f"  ativada: {user['email']} (#{user['id']}) ✓")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Ativar manualmente uma conta do Sports parada na verificação de e-mail.")
    ap.add_argument("--db", default=us.DEFAULT_DB, help="caminho do entries.db")
    ap.add_argument("--email", help="ativar a conta com este e-mail")
    ap.add_argument("--all", action="store_true", help="ativar TODAS as contas pendentes")
    ap.add_argument("--list", action="store_true", help="apenas listar as contas pendentes")
    ap.add_argument("--apply", action="store_true", help="efetivar (default: dry-run)")
    args = ap.parse_args()

    db = args.db
    if not os.path.isfile(db):
        print(f"[activate_user] nada a fazer — BD não encontrado: {db}")
        return 0

    print(f"[activate_user] BD: {db}")

    # Listing mode: --list, or no target given -> show who's pending and stop.
    if args.list or (not args.email and not args.all):
        pending = us.list_unverified(db)
        if not pending:
            print("[activate_user] nenhuma conta pendente de verificação.")
        else:
            print(f"[activate_user] {len(pending)} conta(s) pendente(s):")
            for u in pending:
                print(f"  #{u['id']:<4} {u['email']:32s} criada em {u['created_at']}")
            if not args.list:
                print("[activate_user] use --email <e-mail> --apply (ou --all --apply) p/ ativar.")
        return 0

    # Activation mode (--email or --all).
    scope = "TODAS as pendentes" if args.all else f"e-mail {args.email}"
    print(f"[activate_user] alvo: {scope}  ({'APLICAR' if args.apply else 'dry-run'})")

    if args.all:
        pending = us.list_unverified(db)
        if not pending:
            print("[activate_user] nada a ativar — nenhuma conta pendente.")
            return 0
        for u in pending:
            _activate(u, args.apply, db)
    else:
        user = us.get_user_by_email(args.email, db)
        if not user:
            print(f"[activate_user] nenhuma conta com o e-mail: {args.email}")
            return 0
        _activate(user, args.apply, db)

    if not args.apply:
        print("[activate_user] DRY-RUN — nada alterado. Rode de novo com --apply.")
    else:
        print("[activate_user] pronto. A(s) conta(s) já pode(m) logar normalmente.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
