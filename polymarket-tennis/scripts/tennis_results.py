#!/usr/bin/env python3
"""Auto-settlement of tennis predictions + closing-price capture (best-effort).

Settlement matches each PENDENTE prediction's player pair to a finished match in Jeff
Sackmann's season data and settles to the actual winner. Sackmann's `tourney_date` is
the tournament START date (not the match date), so matching is by the unordered player
pair within the season — best-effort, and lagged by however often Sackmann updates.
Offline/blocked it settles nothing (rows stay PENDENTE).

CLV: `capture_close_prices` snapshots each shadow row's reference-side CLOB midpoint.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import ratings_source
import tennis_predictions as tdb
from ratings import normalize

GAMMA_API = "https://gamma-api.polymarket.com"


def _pair(a: str, b: str) -> frozenset:
    return frozenset({normalize(a), normalize(b)})


def _surname(name: str) -> str:
    """Last surname token, dropping a trailing initial — converges name formats across feeds.

    'Carlos Alcaraz' (Sackmann) -> 'alcaraz'; 'Alcaraz C.' (tennis-data) -> 'alcaraz';
    'Roberto Bautista Agut' -> 'agut'. So the same match settles regardless of which feed
    supplied it and how the prediction stored the player name.
    """
    return ratings_source._surname_key(normalize(name))


def _surname_pair(a: str, b: str) -> frozenset:
    return frozenset({_surname(a), _surname(b)})


def build_winner_lookup(matches: list[dict]) -> dict:
    """{frozenset(players): winner} keyed by BOTH the full-name pair and (when unambiguous) the
    surname pair — so predictions match whether the feed used full names or 'Surname I.'."""
    out: dict[frozenset, str] = {}
    sur_count: dict[frozenset, int] = {}
    sur_win: dict[frozenset, str] = {}
    for m in matches:
        w, l = m["winner"], m["loser"]
        out[_pair(w, l)] = w
        sk = _surname_pair(w, l)
        if len(sk) == 2:                       # two distinct surnames
            sur_count[sk] = sur_count.get(sk, 0) + 1
            sur_win[sk] = w
    for sk, n in sur_count.items():
        if n == 1 and sk not in out:           # add surname alias only when unambiguous
            out[sk] = sur_win[sk]
    return out


def lookup_winner(lookup: dict, side: str, opponent: str) -> str | None:
    """Winner for a prediction's (side, opponent), trying the full-name pair then the surname pair."""
    return lookup.get(_pair(side, opponent)) or lookup.get(_surname_pair(side, opponent))


def _as_list(v):
    """Gamma returns these fields as either a JSON-encoded string or a real list."""
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v or "[]")
        except (ValueError, TypeError):
            return []
    return []


def winner_from_market(pred: dict, market: dict) -> str | None:
    """Winner label for a moneyline bet from its RESOLVED Polymarket market, else None.

    Authoritative and feed-independent: a match-winner market resolves to 1/0, so the
    bet settles even for qualifying/ITF matches that the tour-level results feeds
    (Sackmann, tennis-data.co.uk) never carry. Requires the market to be closed/resolved
    AND to carry a definitive (≈1/0) outcome — an open market (live mid prices) is left
    PENDENTE so nothing settles early.
    """
    resolved = bool(market.get("closed")) or \
        str(market.get("umaResolutionStatus", "")).lower() == "resolved"
    if not resolved:
        return None
    prices = _as_list(market.get("outcomePrices"))
    try:
        prices = [float(p) for p in prices]
    except (TypeError, ValueError):
        return None
    if not prices or max(prices) < 0.99:        # not resolved to a definitive winner (e.g. void)
        return None

    side, opp = pred.get("side") or "", pred.get("opponent") or ""
    tok = str(pred.get("token_id") or "")
    tokens = [str(t) for t in _as_list(market.get("clobTokenIds"))]
    if tok and tok in tokens and len(tokens) == len(prices):
        return side if prices[tokens.index(tok)] >= 0.5 else opp   # our token won -> our side

    # Fallback when the token isn't matchable: take the winning outcome name and map it to
    # side/opponent by surname (handles "Surname F." vs full-name storage).
    outcomes = _as_list(market.get("outcomes"))
    if outcomes and len(outcomes) == len(prices):
        win_name = outcomes[max(range(len(prices)), key=lambda i: prices[i])] or ""
        wsur = _surname(win_name)
        if wsur and wsur == _surname(side):
            return side
        if wsur and wsur == _surname(opp):
            return opp
    return None


def _fetch_markets_by_condition(api, condition_ids: list[str]) -> dict:
    """{conditionId: market_dict} from Gamma /markets?condition_ids=. Best-effort, {} offline."""
    ids = [c for c in dict.fromkeys(condition_ids) if c]
    if not ids:
        return {}
    out: dict[str, dict] = {}

    def absorb(rows):
        for m in rows if isinstance(rows, list) else []:
            cid = m.get("conditionId") or m.get("condition_id")
            if cid:
                out[cid] = m

    try:
        absorb(api.get(f"{GAMMA_API}/markets", params={"condition_ids": ids}))
    except Exception:  # noqa: BLE001 - batch failed (offline/forbidden): give up
        return out
    for cid in ids:                              # per-id fallback for any the batch missed
        if cid in out:
            continue
        try:
            absorb(api.get(f"{GAMMA_API}/markets", params={"condition_ids": cid}))
        except Exception:  # noqa: BLE001
            continue
    return out


def settle_pending_from_market(api, db_path: str = tdb.DEFAULT_DB) -> dict:
    """Settle PENDENTE rows from their Polymarket market resolution (condition_id/token_id).

    The authoritative settlement source — independent of any tour results feed, so it
    settles qualifying/ITF/Challenger matches the feeds don't cover. Best-effort; offline
    it settles nothing.
    """
    pending = tdb.get_predictions(db_path, status="PENDENTE")
    if not pending:
        return {"checked": 0, "settled": [], "games_matched": 0, "diagnostics": []}
    markets = _fetch_markets_by_condition(api, [p.get("condition_id") for p in pending])
    diags = [f"[market] {len(pending)} PENDENTE row(s); Gamma returned {len(markets)} market(s)"]
    settled, seen = [], set()
    for r in pending:
        slug = r["match_slug"]
        if slug in seen:
            continue
        m = markets.get(r.get("condition_id"))
        if not m:
            continue
        winner = winner_from_market(r, m)
        if not winner:
            continue
        seen.add(slug)
        rows = tdb.settle_match(slug, winner, db_path)
        settled.extend(rows)
        if rows:
            res = "ACERTO" if _surname(winner) == _surname(r.get("side", "")) else "ERRO"
            diags.append(f"[market] ✓ {slug}: resolved -> winner {winner} "
                         f"({res}, {len(rows)} row(s))")
    if settled:
        diags.append(f"[market] settled {len(settled)} row(s) across {len(seen)} match(es)")
    return {"checked": len(pending), "settled": settled,
            "games_matched": len(seen), "diagnostics": diags}


def settle_pending(db_path: str = tdb.DEFAULT_DB, tour: str = "atp",
                   years: list[int] | None = None, api=None) -> dict:
    """Settle eligible PENDENTE predictions. Best-effort.

    Two settlement sources, in order of authority:
      1. The Polymarket market resolution (when `api` is supplied) — authoritative and
         feed-independent, so it settles qualifying/ITF/Challenger matches the tour-level
         results feeds never carry.
      2. The results feed (Sackmann -> tennis-data.co.uk) for whatever the market path
         couldn't resolve (e.g. the market hasn't closed yet).

    Emits a `diagnostics` list (the backend forwards it to the terminal) that explains, per
    pending bet, exactly why it did or didn't settle — feed size, name resolution, and the
    concrete reason a pair wasn't found (not played yet / opponent absent / name mismatch).
    """
    pending = tdb.get_predictions(db_path, status="PENDENTE")
    if not pending:
        return {"checked": 0, "settled": [], "diagnostics": ["0 PENDENTE rows — nothing to settle"]}
    if years is None:
        years = [datetime.now(timezone.utc).year]

    # 1) Polymarket market resolution first — authoritative, and covers matches no feed lists.
    market_settled: list = []
    market_diags: list = []
    if api is not None:
        ms = settle_pending_from_market(api, db_path)
        market_settled = ms.get("settled", [])
        market_diags = ms.get("diagnostics", [])
        pending = tdb.get_predictions(db_path, status="PENDENTE")    # drop the just-settled rows
        if not pending:                                             # all settled -> skip feed fetch
            return {"checked": len(market_settled), "settled": market_settled, "finals_found": 0,
                    "games_matched": ms.get("games_matched", 0),
                    "diagnostics": market_diags + [
                        f"DONE: {len(market_settled)} row(s) settled via market resolution; "
                        f"no PENDENTE rows left, results feed not fetched"]}

    # 2) Predictions span BOTH tours (atp-… and wta-… slugs), so settle each against ITS OWN
    # tour's feed — querying only the default tour leaves the other tour's bets stuck forever.
    def _tour_of(slug: str) -> str:
        return "wta" if (slug or "").split("-", 1)[0].lower() == "wta" else "atp"
    tours = sorted({_tour_of(r["match_slug"]) for r in pending})
    diags: list[str] = list(market_diags)
    diags.append(f"{len(pending)} PENDENTE row(s) across tour(s) {tours}; results for {years}")

    lookups: dict = {}
    feed_sur: dict = {}
    finals_found = 0
    for t in tours:
        m = ratings_source.fetch_matches(t, years)
        finals_found += len(m)
        diags.append(f"feed[{t}] returned {len(m)} finished match(es) for {years}")
        lookups[t] = build_winner_lookup(m)
        feed_sur[t] = {_surname(nm) for mm in m for nm in (mm["winner"], mm["loser"])}
    if not finals_found:
        diags.append("⚠ EMPTY feed — every source dry (offline/egress-blocked, or no source has "
                     "these matches yet). Rows stay PENDENTE. Check the [ratings_source] lines above.")
        return {"checked": len(pending) + len(market_settled), "settled": market_settled,
                "finals_found": 0, "games_matched": len(market_settled),
                "diagnostics": diags}

    settled, seen = [], set()
    for r in pending:
        slug = r["match_slug"]
        if slug in seen:
            continue
        t = _tour_of(slug)
        lookup, fsur = lookups.get(t, {}), feed_sur.get(t, set())
        side, opp = r.get("side", "") or "", r.get("opponent", "") or ""
        s_sur, o_sur = _surname(side), _surname(opp)
        winner = lookup_winner(lookup, side, opp)
        if winner:
            seen.add(slug)
            # The feed labels the winner as e.g. "Alcaraz C.", which won't string-match the
            # prediction's "Carlos Alcaraz" — settle_match's compute_status is exact, so map the
            # feed winner back to this row's own side/opponent label (by surname) or it'd ANULAR.
            w_sur = _surname(winner)
            winner_label = side if w_sur == s_sur else (opp if w_sur == o_sur else winner)
            rows = tdb.settle_match(slug, winner_label, db_path)
            settled.extend(rows)
            if rows:
                result = "ACERTO" if w_sur == s_sur else "ERRO"
                diags.append(f"✓ {slug}: {side} vs {opp} -> winner {winner} "
                             f"({result}, {len(rows)} row(s) updated)")
            else:
                diags.append(f"⚠ {slug}: found winner {winner} but 0 rows updated "
                             f"(already settled, or slug not in DB)")
        else:
            present = [(p, sur) for p, sur in ((side, s_sur), (opp, o_sur)) if sur in fsur]
            if not present:
                why = "neither player is in the feed (match not played yet, or feed lacks this event)"
            elif len(present) == 1:
                why = (f"only {present[0][0]!r} (surname {present[0][1]!r}) is in the feed — the "
                       f"opponent is absent (different event, or name didn't normalize the same)")
            else:
                why = ("both players ARE in the feed but not paired together — they played "
                       "different opponents, or a name-format mismatch broke the pairing")
            diags.append(f"✗ {slug}: {side} vs {opp} — UNSETTLED: {why} "
                         f"[looked up surnames {s_sur!r}/{o_sur!r}]")

    if not settled:                              # help compare expected vs available names
        for t in tours:                          # per tour, so each feed's names are visible
            sample = sorted(feed_sur.get(t, set()))[:25]
            diags.append(f"feed[{t}] surname sample (first 25): " + ", ".join(sample))
    all_settled = market_settled + settled
    diags.append(f"DONE: {len(all_settled)} row(s) settled "
                 f"({len(market_settled)} via market, {len(settled)} via feed) "
                 f"across {len(market_settled) + len(seen)} match(es)")
    return {"checked": len(pending) + len(market_settled), "settled": all_settled,
            "finals_found": finals_found,
            "games_matched": len(market_settled) + len(seen), "diagnostics": diags}


def capture_close_prices(db_path: str = tdb.DEFAULT_DB) -> int:
    """Snapshot the reference-side closing CLOB midpoint for shadow rows missing it (CLV)."""
    from category_common import APIClient, fetch_midpoint  # lazy
    con = tdb.connect(db_path)
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT id, ref_token FROM model_log WHERE close_price IS NULL "
            "AND ref_token IS NOT NULL")]
    finally:
        con.close()
    api = APIClient()
    n = 0
    for r in rows:
        mid = fetch_midpoint(api, r["ref_token"])
        if mid is not None:
            tdb.set_close_price(db_path, r["id"], mid)
            n += 1
    return n


def settle_model_log_from_feed(db_path: str = tdb.DEFAULT_DB, tour: str = "atp",
                               years: list[int] | None = None) -> int:
    """Settle ALL shadow rows (bet or not) from Sackmann results, for unbiased calibration."""
    rows = [r for r in tdb.get_model_log(db_path) if r.get("ref_outcome") is None]
    if not rows:
        return 0
    if years is None:
        years = [datetime.now(timezone.utc).year]
    matches = ratings_source.fetch_matches(tour, years)
    if not matches:
        return 0
    # Need each shadow row's two players; ref_side is player A, but the opponent isn't
    # stored on model_log — recover it from the recorded prediction when present.
    preds = {p["match_slug"]: p for p in tdb.get_predictions(db_path)}
    lookup = build_winner_lookup(matches)
    winners: dict[str, str] = {}
    for r in rows:
        slug = r["match_slug"]
        p = preds.get(slug)
        if not p:
            continue
        winner = lookup_winner(lookup, p.get("side", ""), p.get("opponent", ""))
        if winner:
            winners[tdb.model_log_base(slug)] = winner
    return tdb.settle_model_log(db_path, winners)
