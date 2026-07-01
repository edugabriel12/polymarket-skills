---
name: polymarket-copy-trader
description: >-
  Use this skill to run a separate paper copy-trade flow that follows public Polymarket
  wallets. Save wallets by name+address, continuously track their BUYS and SELLS via the
  Data API /trades endpoint, and mirror them into a $10k fake-USD paper portfolio. By default only
  weather markets are copied (COPY_WEATHER_ONLY=0 to copy all). Buys copy the same USD value the
  wallet spent, clamped to [$5, $100], with a 20% slippage guard; sells are skipped if they would
  exceed 20% slippage. Includes a FastAPI backend + React dashboard (Carteiras,
  Entradas, Resultados). Read-only, no private key. Trigger on: copy trade, copy trading,
  follow a wallet, mirror a wallet, track wallet buys and sells, paper copy portfolio,
  slippage-bounded copy, wallet entries dashboard.
version: 1.0.0
author: polymarket-skills
---

# Polymarket Copy-Trader (Paper)

Follow public Polymarket wallets and mirror their **buys and sells** into a **$10,000 fake-USD
paper portfolio**, automatically and slippage-bounded. Read-only against public APIs — **no
private key, no funds at risk**.

**Paper trading simulation — not financial advice. Real trading involves risk of loss.**

## When to use

- The user wants to *copy-trade* / *follow* / *mirror* one or more public wallets in paper mode.
- The user wants a dashboard of copied entries with live results and per-wallet P&L/ROI/win-rate.

This is distinct from `polymarket-wallet-analyzer` (one-shot analysis of a wallet) and from the
`polymarket-wallet-dashboard` watcher (tier-based position forwarding). This skill copies
**individual buy/sell trades** into a simulated portfolio.

## Rules enforced

- **Weather markets only** (default) — non-weather trades are ignored (keyword set from the weather
  edge bot + Gamma `weather` tag). `COPY_WEATHER_ONLY=0` copies all markets.
- **BUY** copies the **same USD value the wallet spent**, clamped to **[$5, $100]**. A **20%
  slippage guard** (`compute_max_size_for_slippage`, reused) may reduce the size; if it drops below
  the $5 floor, or the portfolio is out of cash → skipped (logged, never silently dropped).
- **SELL** mirrors the fraction the wallet sold; if it would exceed 20% slippage → not executed.
- **Untrusted market text** (CLAUDE.md rule #5): all titles/slugs sanitized, only displayed.
- **Paper only**: never touches live execution or private keys.

## Quick start

```bash
cd polymarket-copy-trader && ./dev.sh
# Backend :8002 · Frontend :5175
```

Add a wallet in the **Carteiras** tab (name + `0x…` address). The backend baselines its history,
then copies only new trades every `COPY_POLL_SEC` (default 60s). Watch the **Entradas** and
**Resultados** tabs populate.

## Architecture

See `README.md`. Backend is a thin orchestration layer over reused in-repo helpers (wired in
`backend/deps.py`): the slippage sizer (`polymarket-analyzer`), the book-walk fill simulator
(`polymarket-paper-trader`), and the wallet trade feed (`polymarket-wallet-analyzer`).

Data lives in two user-facing SQLite tables — `wallets` and `entries` (linked by `wallet_id`) —
plus internal support tables for paper positions and cash.
