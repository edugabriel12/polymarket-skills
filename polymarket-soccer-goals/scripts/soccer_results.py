#!/usr/bin/env python3
"""Auto-settlement of soccer predictions from a results feed (football-data.org).

For each PENDENTE prediction it finds the game's final score, then settles the
TOTAL (total goals vs line) and BTTS (both teams scored?) rows. Settlement is
**order-independent** — total = sum of goals, BTTS = both > 0 — so games are
matched by the unordered team pair + date, sidestepping any home/away ambiguity.

football-data.org free tier covers the World Cup, Euros, and the big-5 leagues
(10 req/min, free API key via X-Auth-Token). Best-effort: with no key or offline,
nothing settles (rows stay PENDENTE). Lazy `requests` import.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import leagues
import soccer_predictions as spdb

FOOTBALL_DATA_API = "https://api.football-data.org/v4"

# Results-feed three-letter codes (lowercased) -> our slug team code. National-team
# TLAs differ from the ISO codes Polymarket uses (NED vs nld); clubs mostly match.
TLA_TO_CODE: dict[str, str] = {
    "ned": "nld", "ger": "deu", "sui": "che", "den": "dnk", "por": "por",
    "rsa": "rsa", "kor": "kor", "uae": "are", "ksa": "sau", "iri": "irn",
    "alg": "dza", "mar": "mar", "civ": "civ", "wal": "wal", "sco": "sct",
    "ire": "irl", "cze": "cze", "uru": "uru", "par": "pry", "chi": "chl",
}


def norm_code(code: str | None) -> str:
    c = (code or "").strip().lower()
    return TLA_TO_CODE.get(c, c)


def _pair_key(date: str, a: str, b: str) -> tuple:
    return (date, *tuple(sorted((norm_code(a), norm_code(b)))))


# ---------------------------------------------------------------------------
# Fetch finished matches (network; best-effort)
# ---------------------------------------------------------------------------


def fetch_finished(date_from: str, date_to: str, token: str | None,
                   timeout: int = 8) -> dict[tuple, tuple]:
    """Return {(date, code_a, code_b): (total_goals, btts_bool)} for FINISHED games."""
    if not token:
        return {}
    try:
        import requests  # lazy
        resp = requests.get(f"{FOOTBALL_DATA_API}/matches",
                            params={"dateFrom": date_from, "dateTo": date_to},
                            headers={"X-Auth-Token": token}, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception:  # noqa: BLE001
        return {}
    return parse_finished(data)


def parse_finished(data: dict) -> dict[tuple, tuple]:
    """Pure: normalize a football-data.org /matches payload into the pair lookup."""
    out: dict[tuple, tuple] = {}
    for m in (data or {}).get("matches", []):
        if (m.get("status") or "").upper() not in ("FINISHED", "AWARDED"):
            continue
        home, away = m.get("homeTeam", {}), m.get("awayTeam", {})
        ha = home.get("tla") or home.get("shortName") or home.get("name")
        aa = away.get("tla") or away.get("shortName") or away.get("name")
        ft = (m.get("score", {}) or {}).get("fullTime", {}) or {}
        hg, ag = ft.get("home"), ft.get("away")
        date = (m.get("utcDate") or "")[:10]
        if ha is None or aa is None or hg is None or ag is None or not date:
            continue
        total = float(hg) + float(ag)
        btts = (hg >= 1 and ag >= 1)
        out[_pair_key(date, ha, aa)] = (total, btts)
    return out


# ---------------------------------------------------------------------------
# Settle
# ---------------------------------------------------------------------------


def decide_settlements(pending_games: list[dict], lookup: dict[tuple, tuple]) -> list[dict]:
    """Pure: match pending games to finished results. Returns settle instructions.

    pending_games: [{game_slug, game_date, home, away}]; lookup from parse_finished.
    """
    out = []
    for g in pending_games:
        key = _pair_key(g["game_date"], g["home"], g["away"])
        res = lookup.get(key)
        if res is not None:
            out.append({"game_slug": g["game_slug"], "actual_total": res[0],
                        "actual_btts": res[1]})
    return out


def settle_model_log_from_feed(db_path: str = spdb.DEFAULT_DB, token: str | None = None,
                               days_back: int = 4) -> int:
    """Settle ALL shadow rows (bet or not) from the results feed, for unbiased calibration."""
    token = token or os.environ.get("FOOTBALL_DATA_TOKEN")
    rows = spdb.get_model_log(db_path)
    unsettled = [r for r in rows if r.get("ref_outcome") is None and r.get("game_date")]
    if not unsettled or not token:
        return 0
    dates = sorted({r["game_date"] for r in unsettled})
    today = datetime.now(timezone.utc).date().isoformat()
    date_from = min(dates[0], (datetime.now(timezone.utc).date()
                               - timedelta(days=days_back)).isoformat())
    lookup = fetch_finished(date_from, max(dates[-1], today), token)
    finals_total, finals_btts = {}, {}
    for r in unsettled:
        base = spdb.model_log_base(r["game_slug"])
        if base in finals_total:
            continue
        home, away = leagues.parse_teams(r["game_slug"])
        if not (home and away):
            continue
        res = lookup.get(_pair_key(r["game_date"], home, away))
        if res is not None:
            finals_total[base] = res[0]
            finals_btts[base] = 1 if res[1] else 0
    return spdb.settle_model_log(db_path, finals_total, finals_btts)


def capture_close_prices(db_path: str = spdb.DEFAULT_DB) -> int:
    """Snapshot the reference-side closing price for shadow rows missing it (CLV)."""
    from category_common import APIClient, fetch_midpoint  # lazy
    con = spdb.connect(db_path)
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
            spdb.set_close_price(db_path, r["id"], mid)
            n += 1
    return n


def settle_pending(db_path: str = spdb.DEFAULT_DB, token: str | None = None,
                   days_back: int = 4, vlog=None) -> dict:
    """Settle eligible PENDENTE soccer predictions from the results feed.

    Emits step-by-step diagnostics (returned under ``diagnostics`` and echoed via
    ``vlog``) so a stuck settlement can be debugged from the /results output: how
    many rows are pending, which games failed team/date parsing, the feed window
    queried, how many finished games came back, and — per pending game — whether
    it matched, missed because the pair isn't FINISHED yet, or missed because the
    feed dated it on a DIFFERENT day (UTC rollover) than the prediction.
    """
    diag: list[str] = []

    def note(msg: str) -> None:
        diag.append(msg)
        if vlog:
            vlog(f"[soccer-settle] {msg}")

    token = token or os.environ.get("FOOTBALL_DATA_TOKEN")
    pending = spdb.get_predictions(db_path, status="PENDENTE")
    note(f"pending PENDENTE rows: {len(pending)}")
    if not token:
        note("no FOOTBALL_DATA_TOKEN — cannot reach the results feed; rows stay PENDENTE")
        return {"checked": len(pending), "settled": [], "finals_found": 0,
                "games_matched": 0,
                "note": "set FOOTBALL_DATA_TOKEN to auto-settle soccer", "diagnostics": diag}
    if not pending:
        return {"checked": 0, "settled": [], "finals_found": 0, "games_matched": 0,
                "diagnostics": diag}

    # Distinct games + their teams (order-independent).
    games: dict[str, dict] = {}
    unparsed: list[str] = []
    for r in pending:
        slug = r["game_slug"]
        if slug in games:
            continue
        home, away = leagues.parse_teams(slug)
        if home and away and r.get("game_date"):
            games[slug] = {"game_slug": slug, "game_date": r["game_date"],
                           "home": home, "away": away}
        else:
            unparsed.append(slug)
    note(f"distinct pending games: {len(games)} parsed, {len(unparsed)} unparsed/dateless")
    if unparsed:
        note("could not parse teams+date for: " + ", ".join(sorted(set(unparsed))[:10]))

    dates = sorted({g["game_date"] for g in games.values()})
    if not dates:
        note("no parseable games with a date — nothing to query")
        return {"checked": len(pending), "settled": [], "finals_found": 0,
                "games_matched": 0, "diagnostics": diag}
    today = datetime.now(timezone.utc).date().isoformat()
    date_from = min(dates[0], (datetime.now(timezone.utc).date() - timedelta(days=days_back)).isoformat())
    date_to = max(dates[-1], today)
    note(f"querying results feed {date_from}…{date_to} for {len(games)} game(s)")
    lookup = fetch_finished(date_from, date_to, token)
    note(f"FINISHED games returned by feed: {len(lookup)}")
    if not lookup:
        note("feed returned 0 finished games — bad/expired token, rate-limit, network, "
             "or no covered competition finished in the window")

    instructions = []
    for g in games.values():
        key = _pair_key(g["game_date"], g["home"], g["away"])
        res = lookup.get(key)
        pair = tuple(sorted((norm_code(g["home"]), norm_code(g["away"]))))
        if res is not None:
            note(f"✓ {g['game_slug']}: matched {pair[0]}/{pair[1]} @ {g['game_date']} "
                 f"→ total={res[0]}, btts={res[1]}")
            instructions.append({"game_slug": g["game_slug"], "actual_total": res[0],
                                 "actual_btts": res[1]})
        else:
            alt = sorted({k[0] for k in lookup if k[1:] == pair})
            if alt:
                note(f"✗ {g['game_slug']}: pair {pair[0]}/{pair[1]} is FINISHED in the feed "
                     f"under {alt} but the prediction is dated {g['game_date']} "
                     f"(date mismatch — likely UTC rollover)")
            else:
                note(f"✗ {g['game_slug']}: {pair[0]}/{pair[1]} @ {g['game_date']} not FINISHED "
                     f"in feed (not played yet, or team code not mapped in TLA_TO_CODE)")

    settled = []
    for ins in instructions:
        rows = spdb.settle_game(ins["game_slug"], db_path,
                                actual_total=ins["actual_total"], actual_btts=ins["actual_btts"])
        note(f"settle_game({ins['game_slug']}): updated {len(rows)} row(s) "
             + (", ".join(f"{r['side']}→{r['status']}" for r in rows) if rows else
                "(0 — no PENDENTE rows under this base slug)"))
        settled.extend(rows)
    note(f"DONE: {len(instructions)} game(s) matched, {len(settled)} prediction row(s) settled")
    return {"checked": len(pending), "settled": settled, "finals_found": len(lookup),
            "games_matched": len(instructions), "diagnostics": diag}
