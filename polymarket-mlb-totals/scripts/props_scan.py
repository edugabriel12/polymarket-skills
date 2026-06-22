#!/usr/bin/env python3
"""Feasibility scan: do LIQUID MLB player props exist on Polymarket?

The deep research (references/edge-pathways-deep-research.md) says the only "superior
prediction" path in MLB lives in PLAYER PROPS (strikeouts/HR/hits) — and that Polymarket
(MLB's prediction-market partner) added props in 2026, but their liquidity is thinner
than game lines. Before building a prop model, this verifies the premise: it discovers
the day's MLB markets, classifies them (moneyline / game total / PLAYER prop / team prop),
and measures liquidity (24h volume, on-book liquidity, spread, and order-book depth for
the top props). The verdict tells you whether a prop model is worth building.

Run on a networked machine (the sandbox blocks Polymarket). Pure classification is
offline-testable; the scan/depth calls are best-effort.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics as stx
import sys
from collections import defaultdict

import _bootstrap  # noqa: F401

from category_common import (APIClient, CLOB_API, discover_markets, game_date,
                             resolve_category, to_float)

# Player-stat keywords (question text) -> the prop is about an individual player.
PLAYER_STATS = {
    "strikeouts": r"strikeout|\bk'?s\b|\bks\b", "home_runs": r"home run|\bhr\b|homer",
    "hits": r"\bhits?\b|record a hit|get a hit", "total_bases": r"total bases",
    "rbis": r"\brbi", "stolen_bases": r"stolen base|steal a base",
    "runs": r"runs scored|score a run", "walks": r"\bwalks?\b|base on balls",
    "doubles": r"\bdoubles?\b", "outs_recorded": r"outs recorded|pitching outs",
    "earned_runs": r"earned runs", "pitcher_wins": r"record (?:the |a )?win|pitcher win",
}
_GAME_TOTAL = re.compile(r"-total-\d{1,2}(?:pt5)?$|total runs")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_OU = ("over", "under")
_YN = ("yes", "no")


def _norm(s) -> str:
    return (s or "").strip().lower()


def stat_of(question: str) -> str | None:
    """Which player stat a prop question is about, or None."""
    q = _norm(question)
    for stat, pat in PLAYER_STATS.items():
        if re.search(pat, q):
            return stat
    return None


def classify(market: dict) -> tuple[str, str | None]:
    """Return (kind, stat). kind ∈ moneyline | game_total | player_prop | team_prop | other."""
    slug = _norm(market.get("event_slug") or market.get("slug"))
    q = _norm(market.get("question"))
    outs = [_norm(o) for o in (market.get("outcomes") or [])]
    stat = stat_of(q)
    if stat:
        # A player stat keyword + a player-style question (not a team total) -> player prop.
        return ("player_prop", stat)
    if _GAME_TOTAL.search(slug) or "total runs" in q or (set(outs) == set(_OU)):
        return ("game_total", None)
    if "team total" in q or "team to score" in q:
        return ("team_prop", None)
    # 2 distinct non-OU/YN labels on a base game slug -> moneyline.
    if len(outs) == 2 and not (set(outs) & set(_OU + _YN)) and _DATE_RE.search(slug):
        return ("moneyline", None)
    return ("other", None)


# ---------------------------------------------------------------------------
# Liquidity probes (network, best-effort)
# ---------------------------------------------------------------------------


def fetch_book(api: APIClient, token_id: str) -> dict | None:
    """CLOB order book {bids:[{price,size}], asks:[...]} for a token, or None."""
    try:
        b = api.get(f"{CLOB_API}/book", params={"token_id": token_id})
        return b if isinstance(b, dict) else None
    except Exception:  # noqa: BLE001
        return None


def book_metrics(book: dict) -> dict:
    """Best bid/ask, spread, and $ depth within 5c of the touch."""
    def side(rows, best_fn, within):
        rows = [(to_float(r.get("price")), to_float(r.get("size")))
                for r in (rows or []) if r.get("price") is not None]
        rows = [(p, s) for p, s in rows if p is not None and s is not None]
        if not rows:
            return None, 0.0
        best = best_fn(p for p, _ in rows)
        depth = sum(p * s for p, s in rows if within(p, best))   # $ notional within 5c
        return best, depth
    bid, bid_depth = side(book.get("bids"), max, lambda p, b: p >= b - 0.05)
    ask, ask_depth = side(book.get("asks"), min, lambda p, b: p <= b + 0.05)
    spread = (ask - bid) if (bid is not None and ask is not None) else None
    return {"bid": bid, "ask": ask, "spread": spread,
            "depth_usd": round(bid_depth + ask_depth, 2)}


# ---------------------------------------------------------------------------
# Scan + report
# ---------------------------------------------------------------------------


def run(api, target: str | None, top_n: int, min_liq: float, vlog) -> dict:
    cat, cands = resolve_category("baseball")
    cands = ["mlb"] + [c for c in cands if c != "mlb"]
    _tag, markets = discover_markets(api, cat, cands, min_volume=0.0, include_closed=False)
    on_day = [m for m in markets if (not target or game_date(m) == target)]
    vlog(f"Discovery: tag '{_tag}' -> {len(markets)} markets; {len(on_day)} dated {target or 'any'}")

    by_kind: dict[str, list] = defaultdict(list)
    by_stat: dict[str, list] = defaultdict(list)
    for m in on_day:
        kind, stat = classify(m)
        by_kind[kind].append(m)
        if kind == "player_prop":
            by_stat[stat].append(m)

    def liq(m):
        return to_float(m.get("liquidity")) or 0.0

    def vol(m):
        return to_float(m.get("volume_24h")) or 0.0

    summary = {}
    for kind, ms in by_kind.items():
        liqs = [liq(m) for m in ms]
        summary[kind] = {"n": len(ms), "vol24h_usd": round(sum(vol(m) for m in ms), 2),
                         "liq_usd": round(sum(liqs), 2),
                         "median_liq": round(stx.median(liqs), 2) if liqs else 0.0,
                         "n_liquid": sum(1 for x in liqs if x >= min_liq)}

    # Probe order-book depth for the top player props by 24h volume.
    props = sorted(by_kind.get("player_prop", []), key=vol, reverse=True)[:top_n]
    top = []
    for m in props:
        tok = (m.get("token_ids") or [None])[0]
        bm = book_metrics(fetch_book(api, tok)) if tok else {}
        top.append({"slug": m.get("event_slug") or m.get("slug"),
                    "question": (m.get("question") or "")[:80],
                    "stat": classify(m)[1], "vol24h": round(vol(m), 2),
                    "liq": round(liq(m), 2), **bm})

    return {"target": target, "tag": _tag, "discovered": len(markets), "on_day": len(on_day),
            "by_kind": summary,
            "by_stat": {s: len(ms) for s, ms in sorted(by_stat.items(), key=lambda kv: -len(kv[1]))},
            "top_props": top,
            "samples": {k: [m.get("event_slug") or m.get("slug") for m in ms[:5]]
                        for k, ms in by_kind.items()}}


def verdict(rep: dict, min_liq: float, min_props: int) -> str:
    props = rep["by_kind"].get("player_prop", {})
    n, nliq = props.get("n", 0), props.get("n_liquid", 0)
    deep = [t for t in rep["top_props"] if (t.get("depth_usd") or 0) >= min_liq]
    if n == 0:
        return "❌ NO MLB player props found on Polymarket today — a prop model is premature."
    if nliq >= min_props and deep:
        return (f"✅ VIABLE: {n} player props, {nliq} with >= ${min_liq:.0f} liquidity, "
                f"{len(deep)} with real book depth. A prop model is worth building.")
    return (f"⚠️ THIN: {n} player props but only {nliq} >= ${min_liq:.0f} liquidity / "
            f"{len(deep)} with book depth. Edge would be capped by liquidity — verify before building.")


def format_report(rep: dict, min_liq: float, min_props: int) -> str:
    L = [f"=== MLB market feasibility on Polymarket ({rep['target'] or 'any day'}) ===",
         f"Discovered {rep['discovered']} markets via tag '{rep['tag']}'; {rep['on_day']} on day.", "",
         f"{'kind':<13} {'n':>4} {'vol24h$':>10} {'liq$':>10} {'med_liq$':>9} {'#liquid':>8}"]
    for kind in ("moneyline", "game_total", "player_prop", "team_prop", "other"):
        b = rep["by_kind"].get(kind)
        if b:
            L.append(f"{kind:<13} {b['n']:>4} {b['vol24h_usd']:>10,.0f} {b['liq_usd']:>10,.0f} "
                     f"{b['median_liq']:>9,.0f} {b['n_liquid']:>8}")
    if rep["by_stat"]:
        L += ["", "Player props by stat: " + ", ".join(f"{s}={n}" for s, n in rep["by_stat"].items())]
    if rep["top_props"]:
        L += ["", f"Top player props by 24h volume (book depth = $ within 5c of touch):",
              f"  {'stat':<12} {'vol24h$':>9} {'liq$':>8} {'spread':>7} {'depth$':>8}  question"]
        for t in rep["top_props"]:
            sp = f"{t['spread']:.3f}" if t.get("spread") is not None else "  -"
            L.append(f"  {str(t.get('stat')):<12} {t.get('vol24h',0):>9,.0f} {t.get('liq',0):>8,.0f} "
                     f"{sp:>7} {t.get('depth_usd',0):>8,.0f}  {t['question']}")
    L += ["", "[diag] sample slugs per kind (confirm classification):"]
    for k, slugs in rep["samples"].items():
        if slugs:
            L.append(f"  {k}: " + "; ".join(slugs))
    L += ["", verdict(rep, min_liq, min_props)]
    return "\n".join(L)


def main() -> None:
    p = argparse.ArgumentParser(description="Feasibility scan of MLB player props on Polymarket.")
    p.add_argument("--date", default=None, help="Target day YYYY-MM-DD (default: all discovered)")
    p.add_argument("--top", type=int, default=12, help="How many top props to probe for book depth")
    p.add_argument("--min-liq", type=float, default=500.0, help="USD liquidity bar for 'liquid'")
    p.add_argument("--min-props", type=int, default=3, help="Min liquid props to call it viable")
    p.add_argument("--json", action="store_true")
    p.add_argument("--rate-limit", type=int, default=0)
    a = p.parse_args()
    api = APIClient(rate_limit_ms=a.rate_limit)
    rep = run(api, a.date, a.top, a.min_liq, lambda *x: print(*x, file=sys.stderr))
    if a.json:
        print(json.dumps(rep, indent=2, default=str))
    else:
        print(format_report(rep, a.min_liq, a.min_props))


if __name__ == "__main__":
    main()
