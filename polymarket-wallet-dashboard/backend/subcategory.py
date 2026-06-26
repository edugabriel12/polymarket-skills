#!/usr/bin/env python3
"""Sub-category (market-type) classifier for a wallet's markets.

Layered, by design: a sport-specific overlay runs first (so Futebol gets
"Ambas Marcam"/"Over/Under gols"/"Moneyline (1X2)", esports get
"Vencedor de mapa"/"Total de mapas", etc.); whatever it doesn't resolve falls
through to a UNIVERSAL market-type classifier (Moneyline / Totals / BTTS /
Handicap / Outright / Prop); anything still unmatched is "Outro".

Derivation uses only the market's own text — slug suffix + title/eventSlug —
which is all the public Data API exposes per market. Pure stdlib, offline-tested.

The category strings here MUST match analyze_wallet.classify_category's labels
("Soccer", "Tennis", "Baseball", "Basketball", "American Football", "Hockey",
"League of Legends", "Counter-Strike", "Dota 2", "Valorant", "Cricket",
"Crypto", "Politics", "Economy", ...).

Market text is untrusted user content (CLAUDE.md rule #5): it is only pattern-
matched here, never executed or interpreted as instructions.
"""

from __future__ import annotations

import re

# --- slug-suffix signals (sports markets encode the type in the slug) ----------
_SLUG_TOTALS = re.compile(r"-(?:total|totals|o-?u|over-?under)-?\d", re.I)
_SLUG_BTTS = re.compile(r"-(?:btts|both-teams-to-score|gg-ng|gg)(?:$|[-/?#])", re.I)
_SLUG_SPREAD = re.compile(r"-(?:spread|handicap|hcap|run-?line|puck-?line|asian)", re.I)
_SLUG_MAP = re.compile(r"-map-\d|-map-winner", re.I)
_SLUG_MAP_TOTAL = re.compile(r"-total-maps?|-maps?-(?:over|under)|-maps-\d", re.I)
_SLUG_SERIES = re.compile(r"-(?:series|bo[135]|best-of)", re.I)
_SLUG_OUTRIGHT = re.compile(r"-(?:winner|champion|to-win|title|mvp|outright)(?:$|[-/?#])", re.I)
_SLUG_SCORE = re.compile(r"-(?:correct-score|exact-score|cs-\d-\d)", re.I)

# --- title/eventSlug regex (free text) -----------------------------------------
_T_BTTS = re.compile(r"both teams to score|ambas (?:as )?(?:equipes |times )?marcam|\bbtts\b", re.I)
_T_TOTALS = re.compile(r"\bover\b|\bunder\b|over/under|total (?:goals|runs|points|maps|games|sets)", re.I)
_T_SPREAD = re.compile(r"\bspread\b|handicap|run line|puck line|[+-]\d+\.5\b|asian", re.I)
_T_OUTRIGHT = re.compile(
    r"to win the\b|\bchampion\b|winner of\b|\bmvp\b|\bfinals?\b|\b(?:cup|title|trophy)\b|"
    r"nominee|odds to win|to be relegated|to qualify|golden boot", re.I)
_T_MAP_WINNER = re.compile(r"\bmap \d|map winner|win map", re.I)
_T_MAP_TOTAL = re.compile(r"total maps|maps over|maps under|over \d\.5 maps|number of maps", re.I)
_T_MAP_HCAP = re.compile(r"map handicap|maps? [+-]\d", re.I)
_T_SERIES = re.compile(r"\bseries\b|best of|\bbo[135]\b|match winner|to win (?:the )?match", re.I)
_T_SET = re.compile(r"\bset betting\b|win a set|first set|total sets|\d-set|set winner", re.I)
_T_GAMES = re.compile(r"total games|games over|games under|number of games", re.I)
_T_SCORE = re.compile(r"correct score|exact score|placar (?:exato|correto)", re.I)
_T_MONEYLINE = re.compile(r"\bmoneyline\b|\bto win\b|\bwinner\b|will .* beat|win(?:s)? (?:vs|against)", re.I)
# Match-level wording — when present, "to win the …" is NOT an outright/futures market.
_T_MATCH_LEVEL = re.compile(r"\bmatch\b|\bset\b|\bmap\b|\bseries\b|\bgame\b|\bround\b|partida", re.I)
_T_PRICE = re.compile(
    r"reach \$|hit \$|above \$|below \$|\$[\d,]+|price of|\bup or down\b|higher or lower|"
    r"close (?:above|below)|>= ?\$|<= ?\$", re.I)
_T_THRESHOLD = re.compile(
    r"rate (?:hike|cut)|basis points|\bcpi\b|inflation|\bgdp\b|jobs report|unemployment|"
    r"\bfed\b|interest rate", re.I)


def _blob(title: str, slug: str, event_slug: str) -> str:
    return f"{title or ''} {slug or ''} {event_slug or ''}".lower()


def _totals(blob, slug):
    return _SLUG_TOTALS.search(slug) or _T_TOTALS.search(blob)


def _outright(blob, slug):
    # "to win the match/series/map/set" is match-level, not a futures/outright market.
    if _T_MATCH_LEVEL.search(blob):
        return None
    return _SLUG_OUTRIGHT.search(slug) or _T_OUTRIGHT.search(blob)


def _spread(blob, slug):
    return _SLUG_SPREAD.search(slug) or _T_SPREAD.search(blob)


# Universal market-type — the fallback axis, language-neutral labels.
def universal_type(blob: str, slug: str) -> str | None:
    if _SLUG_BTTS.search(slug) or _T_BTTS.search(blob):
        return "BTTS"
    if _SLUG_TOTALS.search(slug) or _T_TOTALS.search(blob):
        return "Totals"
    if _SLUG_SPREAD.search(slug) or _T_SPREAD.search(blob):
        return "Handicap"
    if _SLUG_SCORE.search(slug) or _T_SCORE.search(blob):
        return "Placar exato"
    if _outright(blob, slug):
        return "Outright"
    if _SLUG_MAP.search(slug) or _T_MAP_WINNER.search(blob):
        return "Vencedor de mapa"
    if _SLUG_SERIES.search(slug) or _T_SERIES.search(blob) or _T_MONEYLINE.search(blob):
        return "Moneyline"
    return None


# --- sport-specific overlays (return a localized sub-type, or None to fall back) ---
def _soccer(blob, slug):
    if _SLUG_BTTS.search(slug) or _T_BTTS.search(blob):
        return "Ambas Marcam"
    if _totals(blob, slug):
        return "Over/Under gols"
    if _SLUG_SCORE.search(slug) or _T_SCORE.search(blob):
        return "Placar exato"
    if _spread(blob, slug):
        return "Handicap"
    if _outright(blob, slug):
        return "Outright"
    return "Moneyline (1X2)"


def _tennis(blob, slug):
    if _T_SET.search(blob):
        return "Set betting"
    if _T_GAMES.search(blob) or _totals(blob, slug):
        return "Total de games"
    if _outright(blob, slug):
        return "Outright"
    return "Vencedor da partida"


def _baseball(blob, slug):
    if re.search(r"run line|run-?line", blob) or _SLUG_SPREAD.search(slug):
        return "Run line"
    if _totals(blob, slug):
        return "Over/Under"
    if _outright(blob, slug):
        return "Outright"
    return "Moneyline"


def _hockey(blob, slug):
    if re.search(r"puck line|puck-?line", blob) or _SLUG_SPREAD.search(slug):
        return "Puck line"
    if _totals(blob, slug):
        return "Over/Under"
    if _outright(blob, slug):
        return "Outright"
    return "Moneyline"


def _spread_sport(blob, slug):
    if _spread(blob, slug):
        return "Spread"
    if _totals(blob, slug):
        return "Over/Under"
    if _outright(blob, slug):
        return "Outright"
    return "Moneyline"


def _esports(blob, slug):
    if _SLUG_MAP_TOTAL.search(slug) or _T_MAP_TOTAL.search(blob):
        return "Total de mapas"
    if _T_MAP_HCAP.search(blob):
        return "Handicap de mapas"
    if _SLUG_MAP.search(slug) or _T_MAP_WINNER.search(blob):
        return "Vencedor de mapa"
    if _outright(blob, slug):
        return "Outright (torneio)"
    return "Vencedor (série)"


def _cricket(blob, slug):
    if _outright(blob, slug):
        return "Outright"
    if _totals(blob, slug):
        return "Totals"
    return "Vencedor"


def _crypto(blob, slug):
    if _T_PRICE.search(blob):
        return "Alvo de preço"
    if _outright(blob, slug):
        return "Outright"
    return "Sim/Não"


def _politics(blob, slug):
    if _outright(blob, slug):
        return "Outright"
    return "Sim/Não"


def _economy(blob, slug):
    if _T_THRESHOLD.search(blob):
        return "Limiar (indicador)"
    return "Sim/Não"


_SPORT = {
    "Soccer": _soccer,
    "Tennis": _tennis,
    "Baseball": _baseball,
    "Basketball": _spread_sport,
    "American Football": _spread_sport,
    "Hockey": _hockey,
    "League of Legends": _esports,
    "Counter-Strike": _esports,
    "Dota 2": _esports,
    "Valorant": _esports,
    "Cricket": _cricket,
    "Crypto": _crypto,
    "Politics": _politics,
    "Economy": _economy,
}


def classify(category: str, title: str = "", slug: str = "", event_slug: str = "") -> str:
    """Sub-category for a market: sport overlay -> universal type -> 'Outro'."""
    blob = _blob(title, slug, event_slug)
    fn = _SPORT.get(category)
    if fn:
        sub = fn(blob, slug)
        if sub:
            return sub
    return universal_type(blob, slug) or "Outro"
