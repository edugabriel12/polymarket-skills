#!/usr/bin/env python3
"""Automatic team-strength sources for the soccer model.

Three layers, used by data_inputs (precedence: ratings CSV > xG > Elo):
  - NATIONAL_ELO: a baked-in snapshot of national-team Elo (for the World Cup /
    international games). Offline, ToS-clean. SNAPSHOT — refresh periodically.
  - Club Elo: the free clubelo.com API (CSV) for club leagues, with a Polymarket
    abbreviation -> Club Elo name alias map.
  - xG via the optional `soccerdata` library (FBref/Understat; ToS-flagged).

All network fetches are best-effort and isolated here; failures return None/{} so
the pipeline degrades to the market-implied fallback (no fabricated edge).
"""

from __future__ import annotations

CLUBELO_API = "http://api.clubelo.com"


# ---------------------------------------------------------------------------
# National-team Elo (snapshot ~2026; World Football Elo scale). Refresh as needed.
# Keyed by lowercase ISO-3 country code; aliases map common variants.
# ---------------------------------------------------------------------------

NATIONAL_ELO: dict[str, float] = {
    "arg": 2090, "fra": 2070, "esp": 2055, "bra": 2040, "eng": 2010, "por": 1995,
    "nld": 1985, "bel": 1955, "ita": 1945, "deu": 1935, "cro": 1905, "col": 1880,
    "mar": 1875, "uru": 1865, "usa": 1800, "mex": 1795, "jpn": 1835, "kor": 1785,
    "sen": 1805, "che": 1815, "dnk": 1830, "aut": 1810, "ecu": 1790, "per": 1740,
    "pol": 1760, "wal": 1745, "swe": 1770, "ukr": 1775, "tur": 1765, "nor": 1790,
    "gha": 1700, "civ": 1720, "nga": 1750, "egy": 1740, "tun": 1710, "dza": 1755,
    "aus": 1730, "irn": 1760, "sau": 1690, "qat": 1670, "can": 1760, "crc": 1700,
    "sct": 1760, "irl": 1720, "srb": 1800, "cze": 1760, "hun": 1750, "grc": 1745,
    "cmr": 1700, "rsa": 1690, "nzl": 1640, "pan": 1680, "jam": 1690,
}

_COUNTRY_ALIASES: dict[str, str] = {
    "ned": "nld", "hol": "nld", "ger": "deu", " tot": "deu", "spa": "esp",
    "eng_": "eng", "fra_": "fra", "kr": "kor", "rok": "kor", "jp": "jpn",
    "swi": "che", "sui": "che", "den": "dnk", "cze_": "cze", "rsa_": "rsa",
    "wls": "wal", "sco": "sct", "ire": "irl", "uae": "are", "ksa": "sau",
    "iri": "irn", "alg": "dza", "mor": "mar", "cmr_": "cmr", "civ_": "civ",
    "por_": "por", "bra_": "bra", "arg_": "arg",
}


def national_elo(code: str | None) -> float | None:
    c = (code or "").strip().lower()
    c = _COUNTRY_ALIASES.get(c, c)
    return NATIONAL_ELO.get(c)


# ---------------------------------------------------------------------------
# Club Elo (clubelo.com): Polymarket abbreviation -> Club Elo club name
# ---------------------------------------------------------------------------

# Polymarket 3-letter club code -> clubelo.com club name. Best-effort and easy to
# extend/correct: an unknown or wrong code just falls back to market-implied (no
# harm). Verify a name with `curl http://api.clubelo.com/<Name> | head`.
CLUB_ELO_ALIASES: dict[str, str] = {
    # --- Premier League ---
    "ars": "Arsenal", "mci": "ManCity", "liv": "Liverpool", "che": "Chelsea",
    "mun": "ManUnited", "tot": "Tottenham", "new": "Newcastle", "avl": "AstonVilla",
    "whu": "WestHam", "bha": "Brighton", "eve": "Everton", "ful": "Fulham",
    "cry": "CrystalPalace", "wol": "Wolves", "bre": "Brentford", "nfo": "Forest",
    "bou": "Bournemouth", "bur": "Burnley", "lee": "Leeds", "lei": "Leicester",
    "sou": "Southampton", "ips": "Ipswich", "shu": "SheffieldUnited", "lut": "Luton",
    "nor": "Norwich", "wat": "Watford", "wba": "WestBrom",
    # --- La Liga ---
    "rma": "RealMadrid", "bar": "Barcelona", "atm": "Atletico", "sev": "Sevilla",
    "rso": "RealSociedad", "vil": "Villarreal", "bet": "Betis", "ath": "Athletic",
    "val": "Valencia", "gir": "Girona", "osa": "Osasuna", "cel": "Celta",
    "ray": "Rayo", "get": "Getafe", "mll": "Mallorca", "ala": "Alaves",
    "lpa": "LasPalmas", "gra": "Granada", "cad": "Cadiz", "alm": "Almeria",
    "vll": "Valladolid", "esp": "Espanyol", "leg": "Leganes", "elc": "Elche",
    # --- Serie A ---
    "int": "Inter", "juv": "Juventus", "mil": "Milan", "nap": "Napoli",
    "rom": "Roma", "laz": "Lazio", "ata": "Atalanta", "fio": "Fiorentina",
    "bol": "Bologna", "tor": "Torino", "mnz": "Monza", "gen": "Genoa",
    "lec": "Lecce", "udi": "Udinese", "cag": "Cagliari", "ver": "Verona",
    "emp": "Empoli", "fro": "Frosinone", "sas": "Sassuolo", "sal": "Salernitana",
    "com": "Como", "par": "Parma", "ven": "Venezia",
    # --- Bundesliga ---
    "bay": "Bayern", "dor": "Dortmund", "rbl": "RBLeipzig", "b04": "Leverkusen",
    "lev": "Leverkusen", "wob": "Wolfsburg", "fra": "Frankfurt", "sge": "Frankfurt",
    "scf": "Freiburg", "tsg": "Hoffenheim", "m05": "Mainz", "bmg": "Gladbach",
    "vfb": "Stuttgart", "fca": "Augsburg", "wbr": "Bremen", "boc": "Bochum",
    "fch": "Heidenheim", "svd": "Darmstadt", "koe": "Koeln", "unb": "UnionBerlin",
    "bsc": "Hertha", "s04": "Schalke", "hol": "Kiel", "stp": "StPauli",
    # --- Ligue 1 ---
    "psg": "Paris", "mar": "Marseille", "lyo": "Lyon", "mon": "Monaco",
    "lil": "Lille", "ren": "Rennes", "nic": "Nice", "len": "Lens", "rei": "Reims",
    "str": "Strasbourg", "nan": "Nantes", "mtp": "Montpellier", "tou": "Toulouse",
    "brs": "Brest", "hav": "LeHavre", "met": "Metz", "lor": "Lorient",
    "aux": "Auxerre", "ang": "Angers", "ste": "SaintEtienne",
    # --- Eredivisie / Primeira Liga ---
    "aja": "Ajax", "psv": "PSV", "fey": "Feyenoord", "azz": "AZAlkmaar",
    "twe": "Twente", "utr": "Utrecht", "ben": "Benfica", "por": "Porto",
    "scp": "Sporting", "bra": "Braga",
}


def club_elo_name(abbr: str | None) -> str | None:
    return CLUB_ELO_ALIASES.get((abbr or "").strip().lower())


def clubelo_name_candidates(abbr: str | None, full_name: str | None) -> list[str]:
    """Club Elo endpoint names to try: the curated alias first, then a name-derived guess.

    Club Elo's per-club endpoint keys off a spaceless CamelCase name (e.g. 'Augsburg',
    'RealMadrid'). The alias map is authoritative; when it misses we derive a best-effort
    name from the FULL club name (now available from discovery) — capitalized, spaces removed —
    which resolves many single/compact European club names the map doesn't list yet. A wrong
    guess just 404s and falls back (no harm). Returns [] when neither yields a candidate.
    """
    out: list[str] = []
    alias = club_elo_name(abbr)
    if alias:
        out.append(alias)
    if full_name:
        compact = "".join(w.capitalize() for w in str(full_name).split())
        if compact and compact not in out:
            out.append(compact)
    return out


def fetch_club_elo(abbr: str | None, timeout: int = 8, name: str | None = None) -> float | None:
    """Latest Club Elo for a club (None on failure).

    Resolves via the curated abbreviation alias, then a name-derived Club Elo endpoint guess
    (so European clubs outside the alias map can still resolve from their full name).
    """
    candidates = clubelo_name_candidates(abbr, name)
    if not candidates:
        return None
    try:
        import requests  # lazy
    except Exception:  # noqa: BLE001
        return None
    for cand in candidates:
        try:
            resp = requests.get(f"{CLUBELO_API}/{cand}", timeout=timeout)
            resp.raise_for_status()
            lines = [ln for ln in resp.text.strip().splitlines() if ln]
            if len(lines) < 2:
                continue
            header = lines[0].split(",")
            idx = header.index("Elo") if "Elo" in header else 4
            return float(lines[-1].split(",")[idx])
        except Exception:  # noqa: BLE001
            continue
    return None


# ---------------------------------------------------------------------------
# xG via soccerdata (optional, ToS-flagged) — best-effort
# ---------------------------------------------------------------------------


def fetch_team_xg(home_abbr: str, away_abbr: str, league_prefix: str,
                  debug: bool = False) -> dict:
    """Rolling xG-for/against -> {total_xg, supremacy_xg}. {} if unavailable.

    Requires the optional `soccerdata` package plus a per-league team-name map
    (not bundled). Returns {} when the library or mapping is missing so the
    pipeline degrades to Elo / market-implied. Full integration is a documented
    enhancement (see references/data-sources.md).
    """
    try:
        import soccerdata  # noqa: F401
    except Exception:  # noqa: BLE001
        return {}
    return {}
