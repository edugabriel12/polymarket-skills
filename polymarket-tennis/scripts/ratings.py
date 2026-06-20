#!/usr/bin/env python3
"""Player rating lookup for the tennis Elo engine (pure stdlib).

Ratings are CSV-driven (analog of the soccer skill's `--ratings-csv`): one row per
player with an overall Elo and optional per-surface Elo. A player not covered by any
rating resolves to None, and the pipeline falls back to MARKET-IMPLIED probability
(edge ~ 0) — the anti-fabrication rule: never invent a rating, never manufacture edge.

CSV columns (header required), surface columns optional:
    player,elo,hard,clay,grass
    Carlos Alcaraz,2250,2240,2300,2210
    Jannik Sinner,2230,2260,2150,2180

Self-host the surface Elo from Ultimate Tennis Statistics' open-source engine
(mcekovic/tennis-crystal-ball, Apache 2.0) or scrape the Tennis Abstract Elo reports,
then export to this CSV. See references/deep-research.md.
"""

from __future__ import annotations

import csv
import re
import unicodedata

import elo as _elo


def normalize(name: str) -> str:
    """Lowercase, strip accents/punctuation, collapse spaces — for tolerant matching.

    'Č. Alcaraz Garfia' and 'alcaraz' won't collide, but 'Carlos Alcaraz' and
    'carlos  alcaraz' will. We also index by last token to catch 'C. Alcaraz'.
    """
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", str(name))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _row_to_rating(row: dict) -> dict:
    def num(key):
        v = (row.get(key) or "").strip()
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    rating = {"elo": num("elo") if num("elo") is not None else _elo.START_ELO}
    for surf in _elo.SURFACES:
        rating[surf] = num(surf)
    return rating


def load_ratings(csv_path: str) -> dict:
    """Load a ratings CSV into {normalized_name: rating, last_token: rating}.

    Full-name keys win; last-token keys are added only when unambiguous (a surname
    shared by two players is left out, so an ambiguous abbreviation resolves to None
    rather than the wrong player).
    """
    by_full: dict[str, dict] = {}
    last_counts: dict[str, int] = {}
    last_map: dict[str, dict] = {}
    with open(csv_path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            name = normalize(row.get("player", ""))
            if not name:
                continue
            rating = _row_to_rating(row)
            by_full[name] = rating
            last = name.split(" ")[-1]
            last_counts[last] = last_counts.get(last, 0) + 1
            last_map[last] = rating
    out = dict(by_full)
    for last, n in last_counts.items():
        if n == 1 and last not in out:        # unambiguous surname only
            out[last] = last_map[last]
    return out


def resolve(name: str, ratings: dict) -> dict | None:
    """Resolve a player name/abbreviation to a rating dict, or None if not covered."""
    if not ratings:
        return None
    key = normalize(name)
    if key in ratings:
        return ratings[key]
    last = key.split(" ")[-1]
    if last in ratings:
        return ratings[last]
    # 'C. Alcaraz' style: drop a leading initial token and retry on the remainder.
    toks = key.split(" ")
    if len(toks) >= 2 and len(toks[0]) == 1:
        rest = " ".join(toks[1:])
        if rest in ratings:
            return ratings[rest]
    return None
