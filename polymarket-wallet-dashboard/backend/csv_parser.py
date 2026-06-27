#!/usr/bin/env python3
"""Parse a bet-history CSV (the exported `*_historico.csv` shape) into per-bet
records the rollup understands.

CSV columns (delimiter ';', Brazilian decimals with ','):
    Data;Evento;Aposta;Conf.;Odd;Investido;ROI%;Lucro

Each row is ONE settled bet. We derive:
  - category  via team dictionaries (MLB/NBA/WNBA/NHL) + UFC + soccer signals,
    falling back to the wallet-analyzer keyword classifier.
  - subcategory via the shared market-type classifier (subcategory.py) using the
    event text + the picked side.
  - won = Lucro > 0 (settled rows only).
  - confidence = Alta / Média / Baixa (normalized).

Event/side text is untrusted (CLAUDE.md rule #5): only parsed and pattern-matched.
"""

from __future__ import annotations

import csv
import io
import os
import re
import sys

import subcategory as sc

_WALLET_ANALYZER = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                 "polymarket-wallet-analyzer", "scripts"))
if _WALLET_ANALYZER not in sys.path:
    sys.path.append(_WALLET_ANALYZER)
import analyze_wallet as wa  # noqa: E402  (reused keyword category classifier + sanitize)


def _num(v) -> float:
    """Brazilian number ('19999,96', '-100', '1,79') -> float. 0.0 on failure."""
    if v is None:
        return 0.0
    s = str(v).strip().replace("\xa0", "").replace(" ", "")
    if not s:
        return 0.0
    # comma is the decimal separator; there are no thousands separators in the export.
    s = s.replace(".", "").replace(",", ".") if s.count(",") and s.count(".") else s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _norm_conf(v: str) -> str:
    s = (v or "").strip().lower()
    if s.startswith("alt"):
        return "Alta"
    if s.startswith("m"):           # média / media
        return "Média"
    if s.startswith("baix") or s.startswith("low"):
        return "Baixa"
    return v.strip() or "—"


# --- team dictionaries (nicknames as they appear in the events) -----------------
def _words(*names) -> "re.Pattern":
    # whole-token match, accent-insensitive enough for these ASCII nicknames
    alt = "|".join(re.escape(n) for n in names)
    return re.compile(rf"(?<![a-z])(?:{alt})(?![a-z])", re.I)

_NBA = _words(
    "knicks", "spurs", "thunder", "lakers", "cavaliers", "pistons", "celtics", "76ers",
    "magic", "timberwolves", "nuggets", "rockets", "raptors", "hawks", "trail blazers",
    "pacers", "bucks", "heat", "bulls", "nets", "wizards", "hornets", "mavericks",
    "clippers", "kings", "suns", "warriors", "jazz", "grizzlies", "pelicans")
_WNBA = _words(
    "liberty", "mystics", "dream", "mercury", "aces", "wings", "valkyries", "tempo",
    "fever", "sky", "lynx", "storm", "sparks")
# NHL nicknames. Deliberately EXCLUDES "rangers"/"kings" — those collide with MLB (Texas
# Rangers) / NBA, and the NHL Rangers/Kings don't appear in these exports.
_NHL = _words(
    "stars", "wild", "oilers", "panthers", "bruins", "maple leafs", "canadiens", "avalanche",
    "golden knights", "lightning", "kraken", "flames", "jets", "senators", "hurricanes",
    "flyers", "capitals", "blue jackets", "blackhawks", "penguins", "blues", "ducks",
    "predators", "sharks", "canucks", "islanders", "devils", "sabres", "red wings", "utah")
_MLB = _words(
    "royals", "rays", "cubs", "mets", "mariners", "pirates", "red sox", "rockies",
    "phillies", "nationals", "brewers", "braves", "marlins", "cardinals", "dodgers",
    "diamondbacks", "giants", "reds", "guardians", "padres", "astros", "yankees",
    "blue jays", "tigers", "twins", "angels", "athletics", "orioles", "rangers",
    "white sox", "mariners")
# College (NCAA) basketball nicknames — only needed for the rare college game with NO O/U line
# (an O/U line already routes to Basketball by magnitude). Tokens picked to avoid pro collisions
# (so NOT "cardinals"/"tigers"/"hurricanes"/"royals", which are disambiguated by line magnitude).
_COLLEGE = _words(
    "red storm", "wildcats", "cougars", "jayhawks", "cyclones", "boilermakers", "spartans",
    "blue devils", "billikens", "wolverines", "lancers", "hawkeyes", "redhawks", "volunteers",
    "zips", "red raiders", "broncos", "longhorns", "buckeyes", "wolfpack", "razorbacks",
    "commodores", "cornhuskers", "horned frogs", "fighting illini", "aggies")

# Soccer market phrasings (with non-US-league events => national-team soccer here).
_SOCCER_SIGNAL = re.compile(
    r"\bo/u\b|both teams to score|exact score|\bspread:|will .* win on|end in a draw|"
    r"halftime|1st half|1h o/u|leading at|both teams", re.I)

# Tennis head-to-head shape: "{Tournament}: {Player} vs {Player}". Challenger/ITF/qualifying
# events often carry no tour keyword (atp/wta/wimbledon), so the keyword classifier drops them
# into "Other". This structure recovers them — applied ONLY when keyword classification already
# gave up, so soccer/esports/combat "A vs B" matches are never stolen.
_TENNIS_H2H = re.compile(r":\s*.+\bvs\b\.?\s+.+", re.I)

# Over/Under line magnitude — the most reliable cross-sport signal, immune to team-nickname
# collisions (college "Utah State"/"Cardinals"/"Tigers" vs NHL/MLB nicknames).
_OU_LINE = re.compile(r"o/u\s*(\d+(?:\.\d+)?)", re.I)


def _ou_line(blob: str) -> float | None:
    m = _OU_LINE.search(blob)
    return float(m.group(1)) if m else None


def classify_event(event: str, side: str) -> str:
    """Category for one market from its text. Layered so specific market-type and line-magnitude
    signals win before the broad team-nickname / keyword fallbacks (which over-grabbed before:
    the soccer `o/u` signal stole tennis/MMA/basketball totals; college nicknames collided with
    NHL/MLB). Used by the CSV analysis AND by the live watcher (fallback when no Gamma tag)."""
    blob = f"{event} {side}".lower()
    # 1. Crypto
    if "bitcoin" in blob or "up or down" in blob:
        return "Crypto"
    # 2. Combat (UFC/MMA): explicit, round totals, or method-of-victory
    if ("ufc" in blob or re.search(r"\bmma\b", blob) or "by ko or tko" in blob
            or ("rounds" in blob and "o/u" in blob)):
        return "Combat Sports"
    # 3. Tennis market types with no tour keyword: total games & set handicap
    #    (tournament-winner "{Tournament}: A vs B" is recovered by _TENNIS_H2H at the end).
    if "match o/u" in blob or "set handicap" in blob:
        return "Tennis"
    # 4. Soccer goalscorer props ("X: 1+ goals", "X: Anytime Goalscorer")
    if "1+ goals" in blob or "goalscorer" in blob:
        return "Soccer"
    # 5. Basketball player props ("X: Points/Rebounds/Assists O/U")
    if re.search(r"\b(?:points|rebounds|assists)\b\s*o/u", blob):
        return "Basketball"
    # 6. World Baseball Classic knockout ("Final Stage: <country> vs <country>") — un-classifiable
    #    from text alone, so a narrow special-case (the live path resolves it via Gamma tags).
    if "final stage" in blob and "dominican" in blob:
        return "Baseball"
    # 7. Soccer corners
    if "corners" in blob:
        return "Soccer"
    # 8. Over/Under markets — line magnitude decides, and it beats nickname collisions.
    line = _ou_line(blob)
    if line is not None:
        if line >= 100:
            return "Basketball"     # NBA ~200+, NCAA ~130-170, WNBA ~155-180, 1st-half ~105-115
        if _NHL.search(blob):
            return "Hockey"         # NHL totals ~5.5-7.5
        if line >= 6.5:
            return "Baseball"       # MLB totals ~6.5-19.5
        return "Soccer"             # soccer goals ~1.5-5.5
    # 9. No O/U line: wording, then team nickname, then keyword/structural fallback.
    if "both teams to score" in blob or "end in a draw" in blob or "exact score" in blob:
        return "Soccer"
    if _NHL.search(blob):
        return "Hockey"
    if _WNBA.search(blob) or _NBA.search(blob) or _COLLEGE.search(blob):
        return "Basketball"
    if _MLB.search(blob):
        return "Baseball"
    if _SOCCER_SIGNAL.search(blob):
        return "Soccer"
    cat = wa.classify_category(blob)
    if cat == "Other" and _TENNIS_H2H.search(event or ""):
        return "Tennis"
    return cat


def _record(date, event, side, conf, odd, invested, profit) -> dict:
    cat = classify_event(event, side)
    title = f"{event} {side}"
    sub = sc.classify(cat, title, "", "")
    won = True if profit > 0 else (False if profit < 0 else None)
    return {
        "date": date, "title": wa.sanitize_text(event), "side": wa.sanitize_text(side),
        "category": cat, "subcategory": sub, "confidence": conf, "odd": odd,
        "invested": invested, "total_pnl": profit, "realized_pnl": profit,
        "unrealized_pnl": 0.0, "current_value": 0.0, "resolved": True, "won": won,
        "n_trades": 1,
    }


def parse_csv(data) -> list[dict]:
    """Parse the CSV bytes/str into per-bet records. Tolerant of header casing/order."""
    text = data.decode("utf-8-sig") if isinstance(data, (bytes, bytearray)) else str(data)
    reader = csv.reader(io.StringIO(text), delimiter=";")
    rows = [r for r in reader if any((c or "").strip() for c in r)]
    if not rows:
        return []
    header = [h.strip().lower() for h in rows[0]]

    def idx(*names, default=None):
        for n in names:
            for i, h in enumerate(header):
                if h.startswith(n):
                    return i
        return default

    # Header present? (first cell 'data'/'date'). Otherwise assume positional.
    has_header = header and (header[0].startswith("data") or header[0].startswith("date"))
    i_date = idx("data", "date", default=0)
    i_event = idx("evento", "event", default=1)
    i_side = idx("aposta", "bet", "side", default=2)
    i_conf = idx("conf", default=3)
    i_odd = idx("odd", default=4)
    i_inv = idx("investido", "invested", "stake", default=5)
    i_profit = idx("lucro", "profit", "pnl", default=7)

    out: list[dict] = []
    for r in rows[1:] if has_header else rows:
        if len(r) <= max(i_event, i_side, i_inv, i_profit):
            continue
        out.append(_record(
            (r[i_date] or "").strip(),
            (r[i_event] or "").strip(),
            (r[i_side] or "").strip(),
            _norm_conf(r[i_conf] if i_conf is not None and i_conf < len(r) else ""),
            _num(r[i_odd] if i_odd is not None and i_odd < len(r) else 0),
            _num(r[i_inv]),
            _num(r[i_profit]),
        ))
    return out
