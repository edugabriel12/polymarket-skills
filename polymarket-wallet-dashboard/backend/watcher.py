#!/usr/bin/env python3
"""The copy-trade watcher — the heart of the wallet tracking.

For each watched wallet it reads the wallet's current positions (Data API), and for
each market computes the TOTAL position ($ invested). When that total reaches a
confidence tier's floor (learned from the wallet's CSV), it emits ONE entry per
(wallet, market) sized by that tier's Unidade Sugerida. A later poll that finds the
position has grown into a HIGHER tier re-emits the same entry (an upgrade). When the
market resolves, it emits a settlement update (status + pnl).

The FIRST poll after a wallet is added snapshots a BASELINE of the markets it had ALREADY
SETTLED before then — those bets pre-date watching and are ignored forever, so settled-before-add
never leaks into Resultados or to Sports. OPEN positions (pre-existing or opened later) ARE
tracked: they show as entries and count in Resultados only once they settle while being watched.

Pure detection (`detect_entries` / `detect_settlements`) is offline-testable; the
polling loop is best-effort and no-ops when the Data API is unreachable.
"""

from __future__ import annotations

import sys

import confidence_model as cm
import csv_parser
import entries as en
import subcategory as sc
import wallet_report as wr
import wallets_store as ws

wa = wr.wa  # analyze_wallet (fetch_positions, to_float, sanitize_text, _end_in_past, …)

_RANK = {t: i for i, t in enumerate(reversed(cm.TIERS))}  # Baixa=0, Média=1, Alta=2


def passes_filter(wallet_filters: dict | None, category: str, subcategory: str,
                  confidence: str) -> bool:
    """Whether a (category, subcategory, confidence) triple is forwarded to Sports/Telegram.

    `None` → no restriction, forward everything (legacy wallets + the user selected ALL combos,
    which the API collapses to None so live categories the CSV never had still pass). A non-null
    dict is strict: a triple passes only if its category AND subcategory are selected AND the
    confidence is listed — so an explicit empty dict `{}` forwards NOTHING.
    """
    if wallet_filters is None:
        return True
    subs = wallet_filters.get(category)
    if not subs:
        return False                                          # category not selected
    confs = subs.get(subcategory)
    if not confs:
        return False                                          # subcategory not selected
    return confidence in confs


def _total_position(pos: dict) -> float:
    return (wa.to_float(pos.get("initialValue")) or wa.to_float(pos.get("totalBought"))
            or wa.to_float(pos.get("currentValue")))


def _cond(pos: dict) -> str:
    return str(pos.get("conditionId") or pos.get("market") or "")


_GENERIC_TAGS = {"Sports (other)", "Esports (other)"}


def _category_from_tags(api, event_slug: str) -> str | None:
    """Map an event's Gamma tags to a category, preferring a specific sport over the generic
    'Sports (other)'/'Esports (other)' tag. None when no tag maps."""
    generic = None
    for tg in wa.fetch_event_tags(api, event_slug):
        cat = wa.TAG_TO_CATEGORY.get(tg)
        if not cat:
            continue
        if cat in _GENERIC_TAGS:
            generic = generic or cat
        else:
            return cat                                     # specific sport wins
    return generic


def resolve_category(api, event_slug, db_path: str = ws.DEFAULT_DB) -> str | None:
    """Polymarket's OWN category for an event via Gamma tags, cached in the DB so it's fetched
    once per event (not per poll). None when no tag maps — the caller keyword-classifies. A
    transient Gamma failure is NOT cached (retried next poll); a real 'no mapping' is."""
    if not event_slug:
        return None
    cached = ws.get_market_category(event_slug, db_path)
    if cached is not None:
        return cached or None                              # '' = known miss -> keyword fallback
    try:
        cat = _category_from_tags(api, event_slug)
    except Exception:  # noqa: BLE001
        return None
    ws.set_market_category(event_slug, cat or "", db_path)
    return cat


def _enrich_categories(api, positions: list, db_path: str = ws.DEFAULT_DB) -> None:
    """Stamp each position with pos['_category'] from Gamma tags (cached). Leaves it None on a
    miss/failure so _market_fields falls back to the keyword/structural classifier."""
    for pos in positions:
        try:
            pos["_category"] = resolve_category(api, pos.get("eventSlug"), db_path)
        except Exception:  # noqa: BLE001 — tag resolution must never break the poll
            pos["_category"] = None


def _market_fields(pos: dict) -> dict:
    """Common parsed fields for a position (reused by the entry + the persisted bet)."""
    title = wa.sanitize_text(pos.get("title"))
    slug = pos.get("slug", "")
    eslug = pos.get("eventSlug", "")
    side = wa.sanitize_text(pos.get("outcome") or "")
    price = wa.to_float(pos.get("avgPrice")) or wa.to_float(pos.get("curPrice"))
    # Prefer Polymarket's OWN category (Gamma tag, resolved + cached in poll_wallet). Fall back
    # to the keyword/structural classifier — live titles can be plain club matchups ("Arsenal vs
    # Chelsea") with no sport keyword, but the SLUG carries the league prefix (epl-/bra-/mlb-…).
    category = pos.get("_category") or csv_parser.classify_event(f"{title} {slug} {eslug}", side)
    return {
        "title": title, "slug": slug, "side": side.upper(), "price": price,
        "odds": (1.0 / price) if price > 0 else 0.0, "category": category,
        "subcategory": sc.classify(category, title, slug, eslug),
        "start": pos.get("gameStartTime") or pos.get("startDate"),
        "url": pos.get("url") or (f"https://polymarket.com/event/{slug}" if slug else None),
    }


def _entry_for_position(wallet: dict, pos: dict, tier: dict,
                        status: str = "OPEN", pnl: float | None = None) -> dict:
    f = _market_fields(pos)
    return en.make_entry(
        key=en.make_key(wallet["address"], _cond(pos)),       # one entry per (wallet, market)
        event=f["title"], category=f["category"], subcategory=f["subcategory"],
        side=f["side"], odds=f["odds"], entry_price=f["price"], unit=tier["unit"],
        confidence=tier["confidence"], live=en.live_flag(f["start"]),
        market_url=f["url"], game_start=f["start"],
        source=wallet.get("name", ""), status=status, pnl=pnl)


def persist_bets(wallet: dict, positions: list, db_path: str = ws.DEFAULT_DB,
                 baseline: set | None = None) -> None:
    """Upsert the latest state of every tiered market into wallet_bets (Phase 2), so the
    wallet's separated Resultados can merge live settled bets with the CSV snapshot.

    Intentionally NOT gated by the wallet's forwarding filters: the owner sees full live
    performance in Resultados; the filter only governs what is pushed to Sports/Telegram.

    `baseline` = markets the wallet already held when first polled. They pre-date watching, so
    they are skipped here too — a pre-add (often already-settled) bet must never appear in
    Resultados."""
    th = wallet.get("thresholds") or {}
    for pos in positions:
        cond = _cond(pos)
        if not cond or (baseline and cond in baseline):
            continue
        tier = cm.classify_position(_total_position(pos), th)
        if not tier:
            continue
        f = _market_fields(pos)
        resolved = _is_resolved(pos)
        pnl = wa.to_float(pos.get("cashPnl")) if resolved else None
        status = (("WON" if pnl > 0 else "LOST" if pnl < 0 else "VOID") if resolved else "OPEN")
        ws.upsert_bet(wallet["id"], cond, {
            "event": f["title"], "market_url": f["url"],
            "category": f["category"], "subcategory": f["subcategory"],
            "confidence": tier["confidence"], "side": f["side"],
            "total_position": _total_position(pos), "entry_price": f["price"],
            "odds": f["odds"], "status": status, "pnl": pnl}, db_path)


def detect_entries(wallet: dict, positions: list, seen_conf: dict,
                   baseline: set | None = None) -> tuple[list, list]:
    """New/upgraded entries. Returns (entries, [(condition_id, confidence)] to persist).

    `seen_conf` = {condition_id: highest tier already alerted}. An entry fires when a
    market is first sized into a tier, or when it climbs into a HIGHER tier.

    `baseline` = markets the wallet already held when first polled — skipped, so a position that
    pre-dates adding the wallet is never alerted as if it were a fresh entry.
    """
    out, persist = [], []
    th = wallet.get("thresholds") or {}
    for pos in positions:
        cond = _cond(pos)
        if not cond or (baseline and cond in baseline):
            continue
        tier = cm.classify_position(_total_position(pos), th)
        if not tier:
            continue
        prev = seen_conf.get(cond)
        if prev is not None and _RANK[tier["confidence"]] <= _RANK.get(prev, -1):
            continue                                          # same/lower tier — already alerted
        f = _market_fields(pos)
        if not passes_filter(wallet.get("filters"), f["category"], f["subcategory"],
                             tier["confidence"]):
            # Filtered out by this wallet's forwarding rules. Crucially we do NOT persist the
            # tier, so the market never enters seen_alerts and can never settle (orphan-free).
            print(f"[watcher] {wallet.get('name')}: filtered out "
                  f"{f['category']}/{f['subcategory']}/{tier['confidence']} ({cond})",
                  file=sys.stderr, flush=True)
            continue
        out.append(_entry_for_position(wallet, pos, tier))
        persist.append((cond, tier["confidence"]))
    return out, persist


def _is_resolved(pos: dict) -> bool:
    cur = wa.to_float(pos.get("curPrice"))
    return (bool(pos.get("redeemable")) or wa._end_in_past(pos.get("endDate"))
            or cur <= wa.RESOLVED_PRICE_EPS or cur >= 1 - wa.RESOLVED_PRICE_EPS)


def detect_settlements(wallet: dict, positions: list, settled: set,
                       seen_conf: dict) -> tuple[list, list]:
    """Settlement updates for markets we ALERTED and that have now resolved.

    Returns (entries with status WON/LOST/VOID + pnl, [condition_id] to mark settled).

    No forwarding filter is applied here on purpose: it gates on `cond in seen_conf`, and
    detect_entries only records seen_conf for markets that PASSED the filter. So a filtered-out
    market never settles (no orphan card on Sports), while a market that WAS forwarded always
    gets its terminal settle — even if the user later narrows the filter.
    """
    out, persist = [], []
    for pos in positions:
        cond = _cond(pos)
        if not cond or cond in settled or cond not in seen_conf:
            continue
        if not _is_resolved(pos):
            continue
        pnl = wa.to_float(pos.get("cashPnl"))
        status = "WON" if pnl > 0 else ("LOST" if pnl < 0 else "VOID")
        conf = seen_conf[cond]
        tier = {"confidence": conf, "unit": cm.UNIT.get(conf, 0.0)}
        out.append(_entry_for_position(wallet, pos, tier, status=status, pnl=pnl))
        persist.append(cond)
    return out, persist


def poll_wallet(api, wallet: dict, db_path: str = ws.DEFAULT_DB) -> list:
    """Fetch a wallet's positions, detect new entries + settlements, persist dedup
    state, and return the entries to push. Best-effort: [] on a fetch failure."""
    try:
        positions = wa.fetch_positions(api, wallet["address"])
    except Exception as e:  # noqa: BLE001
        print(f"[watcher] {wallet.get('name')}: fetch failed — {e}", file=sys.stderr, flush=True)
        return []
    _enrich_categories(api, positions, db_path)       # Polymarket tags -> category (cached)
    wid = wallet["id"]

    # First poll after the wallet was added: snapshot ONLY the markets it had ALREADY SETTLED
    # before now — those bets pre-date watching and are ignored forever (settled-before-add never
    # leaks into Resultados or Sports). OPEN positions (pre-existing or new) are NOT baselined:
    # they're processed below as entries on this very poll and settle normally while watched.
    # (`reset_tracking` nulls baseline_at to re-snapshot from now.)
    if not ws.baseline_established(wid, db_path):
        settled_pre = [_cond(p) for p in positions if _cond(p) and _is_resolved(p)]
        ws.set_baseline(wid, settled_pre, db_path)
        print(f"[watcher] {wallet.get('name')}: baseline set — "
              f"{len(settled_pre)} already-settled market(s) will be ignored",
              file=sys.stderr, flush=True)

    base = ws.baseline_markets(wid, db_path)
    persist_bets(wallet, positions, db_path, baseline=base)  # Phase 2: keep live bet state per wallet
    seen_conf = ws.seen_confidences(wid, db_path)
    settled = ws.settled_keys(wid, db_path)

    new_entries, persist = detect_entries(wallet, positions, seen_conf, baseline=base)
    for cond, conf in persist:
        ws.set_seen_confidence(wid, cond, conf, db_path)
        seen_conf[cond] = conf

    settle_entries, settle_conds = detect_settlements(wallet, positions, settled, seen_conf)
    for cond in settle_conds:
        ws.mark_settled(wid, cond, db_path)

    if new_entries or settle_entries:
        print(f"[watcher] {wallet.get('name')}: {len(new_entries)} new, "
              f"{len(settle_entries)} settled", file=sys.stderr, flush=True)
    return new_entries + settle_entries


def run_once(api, push_fn, db_path: str = ws.DEFAULT_DB) -> int:
    """Poll every watched wallet once and push the resulting entries. Returns count."""
    total = 0
    for summary in ws.list_wallets(db_path):
        wallet = ws.get_wallet(summary["id"], db_path)
        entries = poll_wallet(api, wallet, db_path)
        if entries:
            push_fn(entries)
            total += len(entries)
    return total
