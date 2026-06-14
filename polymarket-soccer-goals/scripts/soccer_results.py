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


def settle_pending(db_path: str = spdb.DEFAULT_DB, token: str | None = None,
                   days_back: int = 4) -> dict:
    """Settle eligible PENDENTE soccer predictions from the results feed."""
    token = token or os.environ.get("FOOTBALL_DATA_TOKEN")
    pending = spdb.get_predictions(db_path, status="PENDENTE")
    if not pending or not token:
        return {"checked": len(pending), "settled": [],
                "note": None if token else "set FOOTBALL_DATA_TOKEN to auto-settle soccer"}

    # Distinct games + their teams (order-independent).
    games: dict[str, dict] = {}
    for r in pending:
        slug = r["game_slug"]
        if slug in games:
            continue
        home, away = leagues.parse_teams(slug)
        if home and away and r.get("game_date"):
            games[slug] = {"game_slug": slug, "game_date": r["game_date"],
                           "home": home, "away": away}

    dates = sorted({g["game_date"] for g in games.values()})
    if not dates:
        return {"checked": len(pending), "settled": []}
    today = datetime.now(timezone.utc).date().isoformat()
    date_from = min(dates[0], (datetime.now(timezone.utc).date() - timedelta(days=days_back)).isoformat())
    lookup = fetch_finished(date_from, max(dates[-1], today), token)

    settled = []
    for ins in decide_settlements(list(games.values()), lookup):
        rows = spdb.settle_game(ins["game_slug"], db_path,
                                actual_total=ins["actual_total"], actual_btts=ins["actual_btts"])
        settled.extend(rows)
    return {"checked": len(pending), "settled": settled, "games_matched": len(set(
        i["game_slug"] for i in decide_settlements(list(games.values()), lookup)))}
