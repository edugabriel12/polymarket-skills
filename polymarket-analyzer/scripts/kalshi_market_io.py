#!/usr/bin/env python3
"""Camada de I/O de mercado da Kalshi para o bot de temperatura.

Extraído de verify_kalshi_weather.py (cliente HTTP provado em runs reais do
operador contra api.elections.kalshi.com) + funções novas para o
kalshi_edge_bot: construção de MarketSpec a partir dos campos ESTRUTURADOS
da Kalshi (floor_strike/cap_strike/strike_type/event_ticker — sem regex
frágil de texto), normalização do orderbook para o formato dos helpers,
fee taker e settlement.

Fatos da venue (ver references/kalshi-us-cities-playbook.md):
- Resolução SEMPRE pelo NWS Daily Climate Report (CLI) do WFO local; dia
  climatológico em hora padrão local (LST) o ano todo — durante o DST a
  janela real é 01:00-00:59 local; aproximamos para o dia-calendário local
  extraído do event_ticker (ex. -26JUL12 = 2026-07-12, dia local).
- Fee taker ≈ ceil(0.07 × contratos × P × (1−P)) — dependente do preço.
  Em termos de notional (size_usd = contratos × P): fee_rate = 0.07×(1−P).
  Em pontos percentuais de edge (payout $1): fee_pp = 7×P×(1−P).
- Orderbook público: {"orderbook": {"yes": [[preço, qtd], ...],
  "no": [[preço, qtd], ...]}} — só BIDS dos dois lados. Ask de YES deriva
  do bid de NO (quem dá bid de NO a 0.58 está vendendo YES a 0.42).
  Unidade dos preços (cents inteiros vs dólares) é AUTODETECTADA
  (max > 0.99 ⇒ cents) e reportada em `unit_detected` — validar contra uma
  amostra real com --sample no host do operador antes de operar.

READ-ONLY: só GET em endpoints públicos. Auth RSA-PSS só é exigida para
ordens/conta privada (ver kalshi_live_stub.py).

Uso:
    python kalshi_market_io.py --test              # self-test hermético
    python kalshi_market_io.py --sample KXHIGHNY   # spec construído vs
                                                   # rules_primary + book real
                                                   # (rodar no host do operador)

Env vars:
    KALSHI_API_BASE  (se definida, ÚNICO host tentado)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests  # noqa: E402

from weather_edge_helpers import MarketSpec  # noqa: E402

KALSHI_CITIES_PATH = (Path(__file__).resolve().parent.parent
                      / "references" / "kalshi-cities.json")

_ENV_BASE = os.environ.get("KALSHI_API_BASE")
# trading-api.kalshi.com está DESCONTINUADO (401 "API has been moved to
# https://api.elections.kalshi.com/" em run real 2026-07-09). Host confirmado
# ao vivo vai primeiro; o antigo fica como fallback só por segurança. Sem
# KALSHI_API_BASE explícita, tenta em sequência e fixa o primeiro que
# responder 200 (cache em _WORKING_BASE).
_CANDIDATE_BASES = ([_ENV_BASE] if _ENV_BASE else [
    "https://api.elections.kalshi.com/trade-api/v2",
    "https://trading-api.kalshi.com/trade-api/v2",
])
_WORKING_BASE: str | None = None  # preenchido na 1a chamada bem-sucedida
_DEBUG = False

# Categorias candidatas para o filtro de série. "Climate and Weather"
# confirmada ao vivo, mas mistura contratos não-temperatura (HURRICANENAME),
# daí o filtro _is_temperature_series aplicado sempre.
_WEATHER_CATEGORIES = ("Climate and Weather", "Weather", "Climate")

_WEATHER_KEYWORDS = re.compile(
    r"\b(weather|temperature|temp|climate|high|low|degrees?|"
    r"fahrenheit|celsius|°[fc])\b", re.IGNORECASE)

_TEMPERATURE_KEYWORDS = re.compile(
    r"\btemp\w*|\bhigh\b|\blow\b|\bheat\b|\bhot\b|\bcold\b|\bdegrees?\b|"
    r"\bfahrenheit\b|\bcelsius\b|°[fc]", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Cliente HTTP (movido de verify_kalshi_weather.py — fonte única)
# ---------------------------------------------------------------------------

def _safe_list(data: dict | None, key: str) -> list:
    """data.get(key) que NUNCA retorna None. A Kalshi devolve
    {'series': None} (não {'series': []}) pra uma categoria sem resultado —
    dict.get(key, default) só usa o default quando a CHAVE está ausente, não
    quando o valor é None (crash real do operador). Usar sempre esta função
    em vez de .get(key, []) nas respostas da Kalshi."""
    if not data:
        return []
    return data.get(key) or []


def _is_temperature_series(s: dict) -> bool:
    return bool(_TEMPERATURE_KEYWORDS.search(str(s.get("title", ""))))


def _debug_log(base: str, path: str, params: dict | None, status, body) -> None:
    if not _DEBUG:
        return
    print(f"[debug] GET {base}{path} params={params or {}}", file=sys.stderr)
    print(f"[debug]   status={status}  body[:500]={str(body)[:500]!r}",
          file=sys.stderr)


def _get(path: str, params: dict | None = None, timeout: int = 20):
    """GET num endpoint público da Kalshi. Fail-open: retorna None em
    qualquer falha (não-200, exceção de rede, JSON malformado). Nunca
    levanta. Sem KALSHI_API_BASE explícita, tenta os hosts candidatos em
    sequência e fixa em _WORKING_BASE o primeiro que responder."""
    global _WORKING_BASE
    bases = [_WORKING_BASE] if _WORKING_BASE else _CANDIDATE_BASES
    for base in bases:
        try:
            r = requests.get(f"{base}{path}", params=params or {},
                             timeout=timeout)
            body_preview = None
            try:
                body_preview = r.json()
            except Exception:
                body_preview = r.text
            _debug_log(base, path, params, r.status_code, body_preview)
            if r.status_code != 200:
                continue
            _WORKING_BASE = base
            return body_preview if isinstance(body_preview, (dict, list)) else None
        except Exception as e:
            _debug_log(base, path, params, "exception", str(e))
            continue
    return None


def discover_weather_series(categories: tuple = _WEATHER_CATEGORIES) -> list[dict]:
    """Descobre séries de TEMPERATURA. Filtro de categoria primeiro (combina
    todas que baterem); fallback client-side por palavra-chave se nenhuma
    bater; filtro final por temperatura no título sempre aplicado (a
    categoria mistura furacão etc.). Retorna [] em qualquer falha."""
    found: dict[str, dict] = {}
    for cat in categories:
        data = _get("/series", params={"category": cat})
        for s in _safe_list(data, "series"):
            tk = s.get("ticker")
            if tk and tk not in found:
                found[tk] = {**s, "_source": "category_filter"}

    if not found:
        data = _get("/series", params={"limit": 200})
        for s in _safe_list(data, "series"):
            text = f"{s.get('title', '')} {s.get('category', '')}"
            if _WEATHER_KEYWORDS.search(text):
                tk = s.get("ticker")
                if tk and tk not in found:
                    found[tk] = {**s, "_source": "keyword_fallback"}

    all_found = list(found.values())
    temp_only = [s for s in all_found if _is_temperature_series(s)]
    if _DEBUG and len(temp_only) != len(all_found):
        print(f"[debug] {len(all_found)} série(s) de clima encontrada(s), "
              f"{len(all_found) - len(temp_only)} descartada(s) por não "
              f"parecer(em) de temperatura (ex. furacão/precipitação)",
              file=sys.stderr)
    return temp_only


def fetch_open_markets(series_ticker: str, limit: int = 50) -> list[dict]:
    """Mercados abertos de uma série. Campos reais (strings): volume_fp/
    open_interest_fp (contagens) e *_dollars (preços/valores) — ver
    _to_float(). Retorna [] em qualquer falha."""
    data = _get("/markets", params={"series_ticker": series_ticker,
                                    "status": "open", "limit": limit})
    return _safe_list(data, "markets")


def fetch_orderbook(ticker: str) -> dict | None:
    """Profundidade de book (yes/no) de um mercado. None em qualquer falha."""
    return _get(f"/markets/{ticker}/orderbook")


def fetch_market(ticker: str) -> dict | None:
    """Um mercado específico (GET /markets/{ticker} → {"market": {...}}).
    É a fonte de settlement do sweep de resolução: status ("settled"/
    "finalized") + result ("yes"/"no"). None em qualquer falha."""
    data = _get(f"/markets/{ticker}")
    if isinstance(data, dict):
        m = data.get("market")
        if isinstance(m, dict):
            return m
    return None


def _to_float(v) -> float | None:
    """Converte um valor da Kalshi pra float. Campos reais vêm como STRINGS
    dolarizadas (ex. 'yes_bid_dollars': '0.0100'). None/''/inconversível ->
    None, nunca levanta."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _spread(row: dict) -> float | None:
    """Spread YES a partir do row JÁ CONVERTIDO (yes_bid/yes_ask floats)."""
    bid, ask = row.get("yes_bid"), row.get("yes_ask")
    if bid is None or ask is None:
        return None
    return round(ask - bid, 4)


# ---------------------------------------------------------------------------
# Config das cidades Kalshi
# ---------------------------------------------------------------------------

def load_kalshi_cities(path: Path = KALSHI_CITIES_PATH) -> dict:
    """Carrega kalshi-cities.json. Dict vazio de cidades se ausente."""
    if not path.exists():
        return {"cities": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def series_to_city(series_ticker: str, kcities: dict) -> Optional[tuple[str, str]]:
    """Mapeia um ticker de série para (cidade, temp_kind) via os campos
    series_high/series_low explícitos da config. None se desconhecido."""
    tk = (series_ticker or "").upper()
    if not tk:
        return None
    for name, c in (kcities.get("cities") or {}).items():
        if (c.get("series_high") or "").upper() == tk:
            return (name, "high")
        if (c.get("series_low") or "").upper() == tk:
            return (name, "low")
    return None


# Aliases de título por cidade (fallback p/ séries com ticker null na config,
# ex. mínimas de LA/Phoenix/Philadelphia/Miami cujo ticker exato não foi
# confirmado). Regex com \b — "\bla\b" NÃO casa com "Philadelphia".
_TITLE_ALIASES: dict[str, tuple[str, ...]] = {
    "New York": (r"\bnew york\b", r"\bnyc\b"),
    "Los Angeles": (r"\blos angeles\b", r"\bla\b"),
    "Chicago": (r"\bchicago\b",),
    "Houston": (r"\bhouston\b",),
    "Phoenix": (r"\bphoenix\b",),
    "Denver": (r"\bdenver\b",),
    "Philadelphia": (r"\bphiladelphia\b", r"\bphilly\b"),
    "Washington DC": (r"\bwashington\b", r"\bdc\b"),
    "Miami": (r"\bmiami\b",),
    "Boston": (r"\bboston\b",),
    "Austin": (r"\baustin\b",),
}


def match_series_by_title(title: str, kcities: dict) -> Optional[tuple[str, str]]:
    """Fallback de mapeamento série→(cidade, temp_kind) pelo TÍTULO da série
    (ex. "Lowest temperature in LA today?"). Só considera cidades presentes
    na config. None se cidade ou high/low não forem identificáveis."""
    t = (title or "").lower()
    if not t:
        return None
    if re.search(r"\blow(est)?\b|\bminimum\b", t):
        kind = "low"
    elif re.search(r"\bhigh(est)?\b|\bmaximum\b", t):
        kind = "high"
    else:
        return None
    configured = kcities.get("cities") or {}
    for name, patterns in _TITLE_ALIASES.items():
        if name not in configured:
            continue
        if any(re.search(p, t) for p in patterns):
            return (name, kind)
    return None


# ---------------------------------------------------------------------------
# MarketSpec estruturado (sem regex de texto)
# ---------------------------------------------------------------------------

_MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
           "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
_DATE_TOKEN_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})(?=-|$)")


def parse_event_date(ticker: str) -> Optional[date]:
    """Extrai a data do token -YYMONDD de um event/market ticker Kalshi
    (ex. 'KXHIGHNY-26JUL12' ou 'KXHIGHNY-26JUL12-B85' → 2026-07-12).
    A data é o DIA LOCAL do dia climatológico (LST). None se não parsear."""
    m = _DATE_TOKEN_RE.search((ticker or "").upper())
    if not m:
        return None
    yy, mon, dd = m.groups()
    month = _MONTHS.get(mon)
    if not month:
        return None
    try:
        return date(2000 + int(yy), month, int(dd))
    except ValueError:
        return None


# strike_type Kalshi → comparison do MarketSpec. Semântica exata de
# inclusividade (> vs ≥; between inclusivo?) deve ser confirmada pelo
# operador via --sample contra rules_primary — para thresholds inteiros em
# °F a diferença é material (exceed>87 vs ≥88).
_STRIKE_TYPE_MAP = {
    "greater": "exceed",
    "greater_or_equal": "exceed",
    "less": "below",
    "less_or_equal": "below",
    "between": "range",
}


def build_market_spec(market: dict, city: str, temp_kind: str) -> Optional[MarketSpec]:
    """Constrói o MarketSpec a partir dos campos ESTRUTURADOS de um mercado
    Kalshi (floor_strike/cap_strike/strike_type/event_ticker) — nada de
    regex sobre o título. Fail-open: None se faltar qualquer campo.

    Convenção Kalshi: 'greater' usa floor_strike; 'less' usa cap_strike;
    'between' usa ambos. threshold_unit é sempre F (mercados US)."""
    if not market or not city:
        return None
    comparison = _STRIKE_TYPE_MAP.get((market.get("strike_type") or "").lower())
    if comparison is None:
        return None
    floor_v = _to_float(market.get("floor_strike"))
    cap_v = _to_float(market.get("cap_strike"))
    if comparison == "range":
        if floor_v is None or cap_v is None:
            return None
        threshold, threshold_high = floor_v, cap_v
    elif comparison == "exceed":
        if floor_v is None:
            return None
        threshold, threshold_high = floor_v, None
    else:  # below
        v = cap_v if cap_v is not None else floor_v
        if v is None:
            return None
        threshold, threshold_high = v, None

    target = parse_event_date(market.get("event_ticker")
                              or market.get("ticker") or "")
    if target is None:
        return None

    title = str(market.get("title") or "")
    subtitle = str(market.get("subtitle") or "")
    raw_q = f"{title} — {subtitle}".strip(" —")
    return MarketSpec(
        city=city,
        threshold_value=threshold,
        threshold_unit="F",
        metric="temp",
        comparison=comparison,
        target_date=target,
        confidence=0.95,
        raw_question=raw_q,
        threshold_value_high=threshold_high,
        temp_kind=temp_kind or "high",
    )


# ---------------------------------------------------------------------------
# Orderbook → formato dos helpers
# ---------------------------------------------------------------------------

def normalize_orderbook(raw: dict | None, side: str = "YES") -> dict:
    """Normaliza o orderbook cru da Kalshi para o formato que
    compute_max_size_for_slippage/implied_probabilities esperam:
    {"bids": [{"price", "size"}] desc, "asks": [{"price", "size"}] asc}.

    A Kalshi só publica BIDS de yes e de no; o ask de um lado deriva do bid
    do outro (bid NO a 0.58 == oferta de venda de YES a 0.42). Autodetecção
    de unidade: qualquer preço > 0.99 ⇒ cents ⇒ ÷100 (num binário o preço
    máximo em dólares é 0.99). `unit_detected` vai no retorno para log."""
    ob = (raw or {}).get("orderbook") or {}
    yes_levels = ob.get("yes") or []
    no_levels = ob.get("no") or []

    def _prices(levels):
        out = []
        for lv in levels:
            try:
                out.append(float(lv[0]))
            except (TypeError, ValueError, IndexError):
                continue
        return out

    all_prices = _prices(yes_levels) + _prices(no_levels)
    unit = "cents" if (all_prices and max(all_prices) > 0.99) else "dollars"

    def _mk(levels):
        out = []
        for lv in levels:
            try:
                p, q = float(lv[0]), float(lv[1])
            except (TypeError, ValueError, IndexError):
                continue
            if unit == "cents":
                p = p / 100.0
            out.append({"price": round(p, 4), "size": q})
        return out

    yes_bids = sorted(_mk(yes_levels), key=lambda x: -x["price"])
    no_bids = sorted(_mk(no_levels), key=lambda x: -x["price"])

    def _derived_asks(other_bids):
        return sorted(({"price": round(1.0 - l["price"], 4), "size": l["size"]}
                       for l in other_bids), key=lambda x: x["price"])

    if side.upper() == "YES":
        bids, asks = yes_bids, _derived_asks(no_bids)
    else:
        bids, asks = no_bids, _derived_asks(yes_bids)
    return {"bids": bids, "asks": asks, "unit_detected": unit}


def implied_from_market_row(m: dict) -> dict:
    """Top-of-book implícito direto dos campos do mercado (sem buscar o book
    completo) — mesmo shape do retorno de implied_probabilities, então
    alimenta compute_edge diretamente. Para o pré-filtro de discovery; o
    book completo só é buscado para os candidatos sobreviventes."""
    return {
        "yes_bid": _to_float(m.get("yes_bid_dollars")),
        "yes_ask": _to_float(m.get("yes_ask_dollars")),
        "no_bid": _to_float(m.get("no_bid_dollars")),
        "no_ask": _to_float(m.get("no_ask_dollars")),
    }


# ---------------------------------------------------------------------------
# Fees
# ---------------------------------------------------------------------------

def kalshi_taker_fee(price: float, contracts: float) -> float:
    """Fee taker da Kalshi em dólares: ceil(0.07·C·P·(1−P)) para o centavo.
    O ceil-por-ordem é aproximação de paper (a Kalshi arredonda por ordem;
    diferenças de agregação são ruído de centavos)."""
    if not price or price <= 0 or price >= 1 or contracts <= 0:
        return 0.0
    cents = 0.07 * contracts * price * (1.0 - price) * 100.0
    # round(…, 6) antes do ceil mata ruído de float (0.07·100·0.25·100 dá
    # 175.00000000000003 em IEEE-754 → ceil viraria 176 sem isso).
    return math.ceil(round(cents, 6)) / 100.0


def kalshi_fee_rate(price: float) -> float:
    """Fee como fração do notional (size_usd = C·P):
    fee = 0.07·C·P·(1−P) = size_usd · 0.07·(1−P). Plugável direto no
    fee_rate do paper_engine."""
    if not price or price <= 0 or price >= 1:
        return 0.0
    return 0.07 * (1.0 - price)


def kalshi_fee_pp(price: float) -> float:
    """Fee em pontos percentuais de edge (payout de $1 por contrato):
    7·P·(1−P) pp. Ex.: P=0.50 → 1.75pp; P=0.85 → 0.89pp. Descontar do
    edge bruto ANTES do gate de edge mínimo (constituição regra 7)."""
    if not price or price <= 0 or price >= 1:
        return 0.0
    return 7.0 * price * (1.0 - price)


# ---------------------------------------------------------------------------
# --sample: diagnóstico no host do operador
# ---------------------------------------------------------------------------

def _sample(ticker: str) -> int:
    """Imprime, para UM mercado real: campos estruturados, MarketSpec
    construído, rules_primary (verdade textual) e orderbook normalizado com
    unit_detected — o smoke que valida a semântica greater/between e a
    unidade do book antes de operar."""
    kcities = load_kalshi_cities()
    if _DATE_TOKEN_RE.search(ticker.upper()):
        market = fetch_market(ticker)
        markets = [market] if market else []
    else:
        markets = fetch_open_markets(ticker, limit=1)
    if not markets or not markets[0]:
        print(f"Nenhum mercado encontrado para {ticker!r} (série sem mercado "
              f"aberto, ticker errado, ou API inacessível — rode "
              f"verify_kalshi_weather.py --debug).")
        return 1
    m = markets[0]
    event_tk = m.get("event_ticker") or m.get("ticker") or ""
    series_tk = _DATE_TOKEN_RE.split(event_tk.upper())[0].rstrip("-")
    mapped = series_to_city(series_tk, kcities) or \
        match_series_by_title(m.get("title") or "", kcities)
    city, kind = mapped if mapped else ("?", "high")
    spec = build_market_spec(m, city, kind) if mapped else None

    print(f"● Mercado: {m.get('ticker')}  (série inferida: {series_tk})")
    print(f"  title/subtitle: {m.get('title')!r} / {m.get('subtitle')!r}")
    print(f"  strike_type={m.get('strike_type')!r} "
          f"floor_strike={m.get('floor_strike')!r} "
          f"cap_strike={m.get('cap_strike')!r} "
          f"close_time={m.get('close_time')!r}")
    print(f"  cidade mapeada: {mapped}")
    print(f"\n● MarketSpec construído:\n  {spec}")
    print(f"\n● rules_primary (verdade textual — confira > vs ≥ e "
          f"inclusividade do between):\n  {m.get('rules_primary')!r}")
    ob = fetch_orderbook(m.get("ticker"))
    norm = normalize_orderbook(ob, "YES")
    print(f"\n● Orderbook normalizado (YES): unit_detected="
          f"{norm['unit_detected']!r}")
    print(f"  bids: {norm['bids'][:5]}")
    print(f"  asks: {norm['asks'][:5]}")
    print(f"\n● Fees @ yes_ask: "
          f"{[{'price': p, 'fee_pp': round(kalshi_fee_pp(p), 3)} for p in [a['price'] for a in norm['asks'][:1]]]}")
    return 0


# ---------------------------------------------------------------------------
# Self-test hermético (sem rede)
# ---------------------------------------------------------------------------

def _test() -> int:
    global _WORKING_BASE, _DEBUG
    saved = requests.get
    saved_working_base, saved_debug = _WORKING_BASE, _DEBUG
    _WORKING_BASE = None

    class _R:
        def __init__(self, status, payload):
            self.status_code = status
            self._p = payload
        def json(self):
            return self._p

    try:
        # T1: normalize_orderbook em CENTS (valores > 0.99): converte /100,
        # ordena bids desc, deriva asks de bids NO (1 - preço) asc.
        raw = {"orderbook": {"yes": [[41, 50], [42, 100]],
                             "no": [[57, 80], [55, 30]]}}
        nb = normalize_orderbook(raw, "YES")
        assert nb["unit_detected"] == "cents", nb
        assert nb["bids"][0] == {"price": 0.42, "size": 100}, nb["bids"]
        assert nb["bids"][1] == {"price": 0.41, "size": 50}, nb["bids"]
        assert nb["asks"][0] == {"price": 0.43, "size": 80}, nb["asks"]
        assert nb["asks"][1] == {"price": 0.45, "size": 30}, nb["asks"]
        print("T1 PASS: normalize cents→dollars, bids desc, asks derivados "
              "de bids NO asc")

        # T2: DOLLARS (todos ≤ 0.99) sem conversão; lado NO espelhado; book
        # vazio/parcial gracioso.
        raw2 = {"orderbook": {"yes": [[0.42, 100]], "no": [[0.57, 80]]}}
        nb2 = normalize_orderbook(raw2, "NO")
        assert nb2["unit_detected"] == "dollars", nb2
        assert nb2["bids"][0] == {"price": 0.57, "size": 80}, nb2["bids"]
        assert nb2["asks"][0] == {"price": 0.58, "size": 100}, nb2["asks"]
        empty = normalize_orderbook(None, "YES")
        assert empty["bids"] == [] and empty["asks"] == [], empty
        only_yes = normalize_orderbook(
            {"orderbook": {"yes": [[0.42, 10]], "no": None}}, "YES")
        assert only_yes["bids"] and only_yes["asks"] == [], only_yes
        print("T2 PASS: dollars sem conversão, lado NO espelhado, book "
              "vazio/só-yes gracioso")

        # T3: build_market_spec greater/less/between + fail-open.
        base_m = {"event_ticker": "KXHIGHNY-26JUL12", "ticker":
                  "KXHIGHNY-26JUL12-T87", "title": "NYC high temp",
                  "subtitle": "88° or above", "strike_type": "greater",
                  "floor_strike": "87.5"}
        sp = build_market_spec(base_m, "New York", "high")
        assert sp and sp.comparison == "exceed" and sp.threshold_value == 87.5, sp
        assert sp.target_date == date(2026, 7, 12) and sp.temp_kind == "high"
        assert sp.threshold_unit == "F" and sp.metric == "temp"
        m_less = {**base_m, "strike_type": "less", "floor_strike": None,
                  "cap_strike": "60.5"}
        sp2 = build_market_spec(m_less, "New York", "low")
        assert sp2 and sp2.comparison == "below" and sp2.threshold_value == 60.5, sp2
        m_range = {**base_m, "strike_type": "between", "floor_strike": "82.5",
                   "cap_strike": "84.5"}
        sp3 = build_market_spec(m_range, "New York", "high")
        assert sp3 and sp3.comparison == "range", sp3
        assert sp3.threshold_value == 82.5 and sp3.threshold_value_high == 84.5
        assert build_market_spec({**base_m, "floor_strike": None},
                                 "New York", "high") is None
        assert build_market_spec({**base_m, "strike_type": "weird"},
                                 "New York", "high") is None
        assert build_market_spec({**base_m, "event_ticker": "KXHIGHNY",
                                  "ticker": "KXHIGHNY"},
                                 "New York", "high") is None
        print("T3 PASS: specs greater/less/between; fail-open sem strike/"
              "strike_type desconhecido/sem data")

        # T4: parse_event_date incluindo virada de ano e formatos.
        assert parse_event_date("KXHIGHNY-26JUL12-B85") == date(2026, 7, 12)
        assert parse_event_date("KXLOWTNYC-26DEC31") == date(2026, 12, 31)
        assert parse_event_date("KXHIGHTPHX-27JAN01") == date(2027, 1, 1)
        assert parse_event_date("KXHIGHNY") is None
        assert parse_event_date("KXHIGHNY-26XXX12") is None
        assert parse_event_date("") is None
        print("T4 PASS: parse_event_date (-26JUL12/-26DEC31/-27JAN01; "
              "inválidos → None)")

        # T5: fees com ceil-por-centavo + rate/pp coerentes.
        assert kalshi_taker_fee(0.5, 100) == 1.75, kalshi_taker_fee(0.5, 100)
        # 0.07*7*0.33*0.67 = 0.10820… → ceil p/ 0.11
        assert kalshi_taker_fee(0.33, 7) == 0.11, kalshi_taker_fee(0.33, 7)
        assert kalshi_taker_fee(0.0, 100) == 0.0
        assert abs(kalshi_fee_rate(0.5) - 0.035) < 1e-9
        assert abs(kalshi_fee_pp(0.5) - 1.75) < 1e-9
        assert abs(kalshi_fee_pp(0.85) - 0.8925) < 1e-9
        print("T5 PASS: taker fee ceil p/ centavo (0.5×100→$1.75; "
              "0.33×7→$0.11); fee_rate=0.035; fee_pp(0.5)=1.75")

        # T6: mapeamento série→cidade exato + fallback por título.
        kc = {"cities": {
            "New York": {"series_high": "KXHIGHNY", "series_low": "KXLOWTNYC"},
            "Los Angeles": {"series_high": "KXHIGHLAX", "series_low": None},
            "Philadelphia": {"series_high": "KXHIGHPHIL", "series_low": None},
        }}
        assert series_to_city("KXHIGHNY", kc) == ("New York", "high")
        assert series_to_city("kxlowtnyc", kc) == ("New York", "low")
        assert series_to_city("KXHIGHXXX", kc) is None
        assert match_series_by_title("Lowest temperature in LA today?", kc) \
            == ("Los Angeles", "low")
        assert match_series_by_title("Highest temperature in NYC", kc) \
            == ("New York", "high")
        # "\bla\b" NÃO pode casar com Philadelphia
        assert match_series_by_title("Highest temperature in Philadelphia",
                                     kc) == ("Philadelphia", "high")
        assert match_series_by_title("Named Atlantic Hurricane", kc) is None
        print("T6 PASS: série→cidade exato + fallback por título "
              "(LA≠Philadelphia, furacão → None)")

        # T7: config real válida — 11 cidades, chaves obrigatórias, pilot,
        # estações batendo com o playbook.
        cfg = load_kalshi_cities()
        cities = cfg.get("cities") or {}
        assert len(cities) == 11, len(cities)
        req = {"series_high", "series_low", "station", "wfo", "lat", "lon",
               "timezone", "pilot", "om_models", "risk_notes", "confirmation"}
        for name, c in cities.items():
            assert req <= set(c), (name, req - set(c))
            assert c["pilot"] is True, name
        expected = {"New York": "KNYC", "Chicago": "KMDW", "Houston": "KHOU",
                    "Austin": "KAUS", "Denver": "KDEN"}
        for city, icao in expected.items():
            assert cities[city]["station"] == icao, (city, cities[city]["station"])
        print("T7 PASS: kalshi-cities.json válido (11 cidades, chaves ok, "
              "pilot=true, KNYC/KMDW/KHOU/KAUS/KDEN corretos)")

        # T8: implied_from_market_row → shape do implied_probabilities.
        imp = implied_from_market_row({
            "yes_bid_dollars": "0.4200", "yes_ask_dollars": "0.4700",
            "no_bid_dollars": "0.5300", "no_ask_dollars": "0.5800"})
        assert imp == {"yes_bid": 0.42, "yes_ask": 0.47,
                       "no_bid": 0.53, "no_ask": 0.58}, imp
        from weather_edge_helpers import compute_edge
        edge = compute_edge(0.60, imp)
        assert edge["best_side"] == "YES", edge
        assert abs(edge["edge_pp_at_best"] - 13.0) < 0.01, edge
        print("T8 PASS: implied_from_market_row alimenta compute_edge "
              "(P=0.60 vs ask 0.47 → YES +13pp)")

        # T9: fetch_market desembrulha {"market": {...}}; falha → None.
        requests.get = lambda *a, **k: _R(200, {"market": {
            "ticker": "KXHIGHNY-26JUL12-T87", "status": "settled",
            "result": "yes"}})
        mkt = fetch_market("KXHIGHNY-26JUL12-T87")
        assert mkt and mkt["result"] == "yes", mkt
        requests.get = lambda *a, **k: _R(500, {})
        _WORKING_BASE = None
        assert fetch_market("KXHIGHNY-26JUL12-T87") is None
        requests.get = lambda *a, **k: _R(200, {"market": None})
        _WORKING_BASE = None
        assert fetch_market("KXHIGHNY-26JUL12-T87") is None
        print("T9 PASS: fetch_market desembrulha market; 500/None → None")

        print("\nAll kalshi_market_io self-tests PASS (9/9)")
        return 0
    finally:
        requests.get = saved
        _WORKING_BASE, _DEBUG = saved_working_base, saved_debug


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Camada de I/O de mercado da Kalshi (read-only). "
                    "--sample roda no host do operador; --test é hermético.")
    ap.add_argument("--sample", metavar="TICKER",
                    help="imprime spec construído vs rules_primary + book "
                         "normalizado de um mercado real (série ou ticker "
                         "de mercado)")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--test", action="store_true",
                    help="self-test hermético (sem rede)")
    args = ap.parse_args()

    if args.test:
        return _test()
    if args.debug:
        global _DEBUG
        _DEBUG = True
    if args.sample:
        return _sample(args.sample)
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
