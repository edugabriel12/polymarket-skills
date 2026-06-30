#!/usr/bin/env python3
"""Model performance for the wallet-dashboard's separated Resultados — by category
only (the Modelo entity has no confidence axis). The model is soccer, so its single
category is Futebol (soccer store).

Metrics mirror the wallet CSV rollup ($-based) so the two entities are comparable:
win rate, n_bets, P&L, ROI — from the store's settled predictions.
"""

from __future__ import annotations

import os
import sys

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_BACKEND_DIR, "..", ".."))
for _d in (os.path.join(_REPO_ROOT, "polymarket-soccer-goals", "scripts"),
           os.path.join(_REPO_ROOT, "polymarket-category-watcher", "scripts")):
    if _d not in sys.path:
        sys.path.append(_d)


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def aggregate(rows: list[dict], category: str) -> dict:
    """{category, n_bets, wins, losses, win_rate, invested, total_pnl, roi} over settled rows.

    ACERTO → size_usd*(1/entry_price − 1); ERRO → −size_usd; ANULADO/other excluded.
    """
    settled = [r for r in rows if r.get("status") in ("ACERTO", "ERRO")]
    n = len(settled)
    wins = sum(1 for r in settled if r.get("status") == "ACERTO")
    invested = sum(_f(r.get("size_usd")) for r in settled)
    pnl = 0.0
    for r in settled:
        s, ep = _f(r.get("size_usd")), _f(r.get("entry_price"))
        pnl += (s * ((1.0 / ep) - 1.0) if ep > 0 else 0.0) if r.get("status") == "ACERTO" else -s
    return {
        "category": category, "n_bets": n, "wins": wins, "losses": n - wins,
        "win_rate": round(wins / n, 4) if n else None,
        "invested": round(invested, 2), "total_pnl": round(pnl, 2),
        "roi": round(pnl / invested, 4) if invested > 0 else None,
    }


def settle_pending_models() -> dict:
    """Liquida (best-effort) as previsões PENDENTE cujo jogo já terminou — chamado ao abrir a
    aba Resultados, para que apareçam nos resultados em vez de ficarem pendentes.
    Futebol: results feed (FOOTBALL_DATA_TOKEN). Falha (sem token / rede / import) é logada e
    não interrompe a página."""
    def log(m: str) -> None:
        print(f"[settle] {m}", file=sys.stderr, flush=True)

    out: dict = {}
    try:
        import soccer_results as sr
        import soccer_predictions as spdb
        db = os.environ.get("SOCCER_PREDICTIONS_DB", spdb.DEFAULT_DB)
        res = sr.settle_pending(db, vlog=log)
        out["soccer"] = {"matched": res.get("games_matched", 0),
                         "settled_rows": len(res.get("settled", []))}
        log(f"soccer -> {out['soccer']}")
    except Exception as e:  # noqa: BLE001
        log(f"soccer skipped: {type(e).__name__}: {e}")
    return out


def model_results() -> dict:
    """Best-effort per-category model performance. Empty categories are omitted.
    Settles finished PENDENTE predictions first so they move into the results."""
    settle_pending_models()
    by_category = []
    try:
        import soccer_predictions as spdb
        rows = spdb.get_predictions(os.environ.get("SOCCER_PREDICTIONS_DB", spdb.DEFAULT_DB))
        agg = aggregate(rows, "Futebol")
        if agg["n_bets"]:
            by_category.append(agg)
    except Exception as e:  # noqa: BLE001
        print(f"[model-results] soccer skipped: {e}", file=sys.stderr, flush=True)

    by_category.sort(key=lambda c: c["total_pnl"], reverse=True)
    return {"entity": "Modelo", "by_category": by_category, "by_confidence": None}


_STATUS_MAP = {"ACERTO": "WON", "ERRO": "LOST", "ANULADO": "VOID"}
import re as _re  # noqa: E402
_PRETTY = _re.compile(r"^[a-z0-9]+-([a-z0-9]+)-([a-z0-9]+)-", _re.I)


def _pretty(slug: str) -> str:
    m = _PRETTY.match(slug or "")
    return f"{m.group(1).upper()} vs {m.group(2).upper()}" if m else (slug or "")


def _store_for(category: str):
    if category == "Futebol":
        import soccer_predictions as s
        return s, os.environ.get("SOCCER_PREDICTIONS_DB", s.DEFAULT_DB), "Soccer"
    return None, None, None


def _row_to_bet(r: dict, category: str, cat_en: str) -> dict:
    import subcategory as sc
    slug = r.get("game_slug") or r.get("match_slug") or ""
    ep = _f(r.get("entry_price"))
    odds = _f(r.get("decimal_odds")) or ((1.0 / ep) if ep > 0 else 0.0)
    title = r.get("market_question") or _pretty(slug)
    return {
        "key": f"model-{slug}-{r.get('side','')}", "event": title, "category": category,
        "subcategory": sc.classify(cat_en, title, slug, ""), "side": r.get("side") or "",
        "odds": round(odds, 4), "entry_price": round(ep, 4), "unit": 1.0, "confidence": "Alta",
        "live": "PRÉ-LIVE", "market_url": r.get("market_url"),
        "status": _STATUS_MAP.get(r.get("status"), "VOID"), "pnl": None,
        "updated_at": r.get("settled_at") or r.get("updated_at"),
    }


def model_bets(category: str, offset: int, limit: int) -> dict:
    """Paginated settled model predictions of a category (Futebol)."""
    store, db, cat_en = _store_for(category)
    if not store:
        return {"total": 0, "bets": []}
    try:
        rows = [r for r in store.get_predictions(db) if r.get("status") in _STATUS_MAP]
    except Exception as e:  # noqa: BLE001
        print(f"[model-bets] {category} skipped: {e}", file=sys.stderr, flush=True)
        return {"total": 0, "bets": []}
    rows.sort(key=lambda r: (r.get("settled_at") or ""), reverse=True)
    page = rows[offset:offset + limit]
    return {"total": len(rows), "bets": [_row_to_bet(r, category, cat_en) for r in page]}


def _row_to_open_bet(r: dict, category: str, cat_en: str) -> dict:
    """A PENDENTE prediction → the open-bet row shape (no result, no P&L yet)."""
    import subcategory as sc
    slug = r.get("game_slug") or r.get("match_slug") or ""
    ep = _f(r.get("entry_price"))
    odds = _f(r.get("decimal_odds")) or ((1.0 / ep) if ep > 0 else 0.0)
    title = r.get("market_question") or _pretty(slug)
    return {
        "key": f"model-open-{slug}-{r.get('side','')}", "event": title, "category": category,
        "subcategory": sc.classify(cat_en, title, slug, ""), "side": r.get("side") or "",
        "odds": round(odds, 4), "entry_price": round(ep, 4), "unit": 1.0, "confidence": "Alta",
        "live": "PRÉ-LIVE", "market_url": r.get("market_url"),
        "status": "OPEN", "pnl": None,
        "updated_at": r.get("updated_at") or r.get("created_at"),
    }


def _open_rows_for(category: str) -> list[dict]:
    """All PENDENTE predictions of one model category, mapped to open-bet rows."""
    store, db, cat_en = _store_for(category)
    if not store:
        return []
    try:
        rows = [r for r in store.get_predictions(db) if r.get("status") == "PENDENTE"]
    except Exception as e:  # noqa: BLE001
        print(f"[model-open-bets] {category} skipped: {e}", file=sys.stderr, flush=True)
        return []
    return [_row_to_open_bet(r, category, cat_en) for r in rows]


def model_open_bets(category: str | None, offset: int, limit: int) -> dict:
    """Paginated OPEN (PENDENTE) model predictions (category=None → Futebol).

    On the first page (offset 0 = tab open) finished PENDENTE rows are settled first, so the
    pending list shows only what's genuinely still open. Skipped on later pages to avoid
    re-settling on every pagination click."""
    if offset == 0:
        settle_pending_models()
    cats = [category] if category else ["Futebol"]
    bets: list[dict] = []
    for c in cats:
        bets.extend(_open_rows_for(c))
    bets.sort(key=lambda b: (b.get("updated_at") or ""), reverse=True)
    return {"total": len(bets), "bets": bets[offset:offset + limit]}
