"""Tiny debug logger for the copy-trade pipeline.

Emits structured, step-by-step lines to stderr so each copied operation can be
traced end to end:
    1) ENTRADA DA CARTEIRA  (the tracked wallet's raw trade)
    2) ANÁLISE DE VOLUME/CAP (order-book slippage sizing + the paper cap decision)
    3) ENTRADA DO PAPER      (what the paper portfolio actually did, or why it skipped)

Gated by COPY_DEBUG (default on). Set COPY_DEBUG=0 to silence.
"""
from __future__ import annotations

import os
import sys

COPY_DEBUG = os.environ.get("COPY_DEBUG", "1") not in ("0", "", "false", "False")
_PREFIX = "copy-trader"


def enabled() -> bool:
    return COPY_DEBUG


def dbg(msg: str) -> None:
    if COPY_DEBUG:
        print(f"[{_PREFIX}] {msg}", file=sys.stderr, flush=True)


def section(title: str) -> None:
    if COPY_DEBUG:
        print(f"[{_PREFIX}] ── {title} ──", file=sys.stderr, flush=True)


def usd(v: float | None) -> str:
    if v is None:
        return "—"
    return f"${v:,.2f}"


def money_shares(shares: float | None) -> str:
    return "—" if shares is None else f"{shares:,.2f} sh"
