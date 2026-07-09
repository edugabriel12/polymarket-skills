#!/usr/bin/env python3
"""Verifica os mercados de temperatura/clima da Kalshi (cobertura, volume,
liquidez) para comparar com a Polymarket — insumo para decidir se vale a
pena expandir o bot de weather_edge para operar também na Kalshi.

Contexto (2026-07): deep-research confirmou a estrutura de contrato da
Kalshi via documentação pública (contract-terms PDF do NHIGH/Central Park-
NYC): tecnicamente um "Binary Contract" cujo critério de payout aceita
definições bracket-style (`greater than`/`less than`/`between`), com estação
de resolução citada explicitamente — "NWS Daily Climate Report for Central
Park, New York" — rigor comparável à seção Rules da Polymarket. Mas a
pesquisa NÃO conseguiu confirmar ao vivo: quantas séries/cidades de
temperatura existem hoje (uma alegação específica de 5 cidades foi
REFUTADA), volume real, profundidade de book, spread. Uma tentativa de
bater na API real foi bloqueada pelo proxy deste ambiente de dev (403,
domínio não liberado). Este script roda no host do operador (Kalshi
acessível) pra puxar esses dados de verdade.

READ-ONLY: só GET em endpoints públicos de mercado. Autenticação RSA-PSS só
é exigida pela Kalshi para colocar ordens / endpoints de conta privada, não
para ler séries/mercados/orderbook públicos — este script não coloca ordem,
não precisa de API key.

DESCOBERTA em vez de suposição: não assume uma lista fixa de cidades/
tickers — a pesquisa refutou explicitamente uma alegação de 5 cidades
específicas. Este script DESCOBRE as séries ativas via /trade-api/v2/series
(filtro de categoria; se vier vazio, cai num fallback client-side por
palavra-chave — mesma desconfiança do filtro da plataforma que
weather_edge_bot.fetch_weather_markets já aplica pra Gamma) e reporta o que
encontrar. Também busca a contagem atual de mercados de clima na Polymarket
(reusando fetch_weather_markets) para uma comparação lado a lado com dados
reais de ambas as plataformas.

Uso:
    python verify_kalshi_weather.py                # descobre séries + resumo
    python verify_kalshi_weather.py --markets       # + mercados abertos por série
    python verify_kalshi_weather.py --orderbook     # + profundidade de book
    python verify_kalshi_weather.py --json
    python verify_kalshi_weather.py --test          # self-test hermético (sem rede)

Env vars:
    KALSHI_API_BASE   (default https://trading-api.kalshi.com/trade-api/v2 —
                       a pesquisa não confirmou este hostname ao vivo; ajuste
                       aqui sem mudar código se a Kalshi tiver migrado)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests  # noqa: E402

KALSHI_BASE = os.environ.get("KALSHI_API_BASE",
                             "https://trading-api.kalshi.com/trade-api/v2")

# Categorias candidatas para o filtro de série da Kalshi. Tentadas em ordem;
# a primeira que devolver resultado é usada. Nomes exatos de categoria não
# foram confirmados ao vivo pela pesquisa — por isso o fallback por
# palavra-chave abaixo cobre o caso de nenhuma bater.
_WEATHER_CATEGORIES = ("Climate and Weather", "Weather", "Climate")

# Fallback client-side: mesma desconfiança do filtro da plataforma que
# weather_edge_bot.fetch_weather_markets já aplica pra Gamma ("Gamma's
# tag_slug=weather param is silently ignored, so we filter ourselves").
_WEATHER_KEYWORDS = re.compile(
    r"\b(weather|temperature|temp|climate|high|low|degrees?|"
    r"fahrenheit|celsius|°[fc])\b", re.IGNORECASE)


def _get(path: str, params: dict | None = None, timeout: int = 20):
    """GET num endpoint público da Kalshi. Fail-open: retorna None em
    qualquer falha (não-200, exceção de rede, JSON malformado). Nunca
    levanta — mesma disciplina de todo fetch_* deste projeto."""
    try:
        r = requests.get(f"{KALSHI_BASE}{path}", params=params or {},
                         timeout=timeout)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def discover_weather_series(categories: tuple = _WEATHER_CATEGORIES) -> list[dict]:
    """Descobre séries (templates recorrentes de mercado, ex. NHIGH para
    Central Park/NYC) relacionadas a clima/temperatura.

    Tenta o filtro de categoria documentado primeiro; se vier vazio de
    todas as categorias candidatas, busca sem filtro e aplica o fallback
    client-side por palavra-chave. Cada entrada retornada ganha um campo
    `_source` ('category_filter' ou 'keyword_fallback') para o relatório
    deixar claro qual caminho encontrou cada série. Retorna [] se a API
    estiver inacessível ou nada for encontrado — nunca levanta.
    """
    found: dict[str, dict] = {}
    for cat in categories:
        data = _get("/series", params={"category": cat})
        for s in (data or {}).get("series", []) if data else []:
            tk = s.get("ticker")
            if tk and tk not in found:
                found[tk] = {**s, "_source": "category_filter"}
    if found:
        return list(found.values())

    data = _get("/series", params={"limit": 200})
    for s in (data or {}).get("series", []) if data else []:
        text = f"{s.get('title', '')} {s.get('category', '')}"
        if _WEATHER_KEYWORDS.search(text):
            tk = s.get("ticker")
            if tk and tk not in found:
                found[tk] = {**s, "_source": "keyword_fallback"}
    return list(found.values())


def fetch_open_markets(series_ticker: str, limit: int = 50) -> list[dict]:
    """Mercados abertos hoje para uma série (cada um já traz volume/
    open_interest/yes_bid/yes_ask/last_price segundo a doc pública da
    Kalshi). Retorna [] em qualquer falha."""
    data = _get("/markets", params={"series_ticker": series_ticker,
                                    "status": "open", "limit": limit})
    return (data or {}).get("markets", []) if data else []


def fetch_orderbook(ticker: str) -> dict | None:
    """Profundidade de book (yes/no) de um mercado. None em qualquer falha."""
    return _get(f"/markets/{ticker}/orderbook")


def _spread(m: dict) -> float | None:
    bid, ask = m.get("yes_bid"), m.get("yes_ask")
    if bid is None or ask is None:
        return None
    try:
        return round(float(ask) - float(bid), 4)
    except (TypeError, ValueError):
        return None


def run(*, want_markets: bool = False, want_orderbook: bool = False,
        want_polymarket_count: bool = True) -> dict:
    """Descobre séries de clima na Kalshi e, opcionalmente, mercados abertos
    + orderbook. Também busca (opcional) a contagem atual de mercados de
    clima na Polymarket via fetch_weather_markets, para comparação lado a
    lado com dados reais das duas plataformas."""
    report: dict = {"series": [], "markets": [], "polymarket": None}

    series = discover_weather_series()
    report["series"] = series

    if series and (want_markets or want_orderbook):
        for s in series:
            tk = s.get("ticker")
            if not tk:
                continue
            for m in fetch_open_markets(tk):
                row = {
                    "series_ticker": tk,
                    "market_ticker": m.get("ticker"),
                    "title": m.get("title"),
                    "status": m.get("status"),
                    "volume": m.get("volume"),
                    "open_interest": m.get("open_interest"),
                    "yes_bid": m.get("yes_bid"),
                    "yes_ask": m.get("yes_ask"),
                    "spread": _spread(m),
                    "last_price": m.get("last_price"),
                    "close_time": m.get("close_time"),
                }
                if want_orderbook:
                    row["orderbook"] = fetch_orderbook(m.get("ticker"))
                report["markets"].append(row)

    if want_polymarket_count:
        try:
            from weather_edge_bot import fetch_weather_markets
            pm_markets = fetch_weather_markets(min_volume=0)
            report["polymarket"] = {
                "n_markets": len(pm_markets),
                "n_high_volume": sum(1 for m in pm_markets
                                     if float(m.get("volumeNum") or 0) >= 10000),
            }
        except Exception as e:
            report["polymarket"] = {"error": str(e)}

    return report


def _fmt_market(row: dict) -> str:
    vol = row.get("volume")
    oi = row.get("open_interest")
    spread = row.get("spread")
    return (f"    {row.get('market_ticker', '?'):<20} "
            f"vol={vol if vol is not None else '?':<8} "
            f"oi={oi if oi is not None else '?':<8} "
            f"bid={row.get('yes_bid')} ask={row.get('yes_ask')} "
            f"spread={spread if spread is not None else '?'}")


def _print_report(report: dict, want_markets: bool, want_orderbook: bool) -> int:
    print("Verificação de mercados de clima Kalshi vs. Polymarket\n")

    series = report["series"]
    if not series:
        print("● Séries de clima: NENHUMA encontrada (API inacessível, ou "
              "nem o filtro de categoria nem o fallback por palavra-chave "
              "bateram — ajuste KALSHI_API_BASE ou inspecione manualmente "
              "em https://kalshi.com/markets).\n")
    else:
        print(f"● Séries de clima descobertas: {len(series)}")
        for s in series:
            print(f"    {s.get('ticker', '?'):<16} {s.get('title', '')!r:<40} "
                  f"[{s.get('_source')}]")
        print()

    if want_markets or want_orderbook:
        markets = report["markets"]
        print(f"● Mercados abertos hoje: {len(markets)}")
        for m in markets:
            print(_fmt_market(m))
            if want_orderbook and m.get("orderbook") is not None:
                ob = m["orderbook"]
                yes_levels = len((ob.get("orderbook") or {}).get("yes") or [])
                no_levels = len((ob.get("orderbook") or {}).get("no") or [])
                print(f"        orderbook: {yes_levels} níveis YES, "
                      f"{no_levels} níveis NO")
            elif want_orderbook:
                print("        orderbook: indisponível")
        print()

    pm = report.get("polymarket")
    print("● Comparação com a Polymarket (fetch_weather_markets ao vivo):")
    if not pm:
        print("    não coletado")
    elif pm.get("error"):
        print(f"    erro ao buscar: {pm['error']}")
    else:
        print(f"    {pm['n_markets']} mercados de clima ativos "
              f"({pm['n_high_volume']} com volume ≥ $10k)")
        n_kalshi_markets = len(report["markets"]) if (want_markets or want_orderbook) else None
        if n_kalshi_markets is not None:
            print(f"    vs. Kalshi: {n_kalshi_markets} mercados abertos em "
                  f"{len(series)} séries de clima descobertas")
        else:
            print(f"    vs. Kalshi: {len(series)} séries de clima "
                  f"descobertas (rode com --markets para contar mercados "
                  f"abertos)")

    print("\n(READ-ONLY — nenhuma ordem colocada, nenhuma alteração feita. "
          "Ajuste KALSHI_API_BASE se o hostname tiver mudado.)")
    return 0 if series else 1


# ---------------------------------------------------------------------------
# Self-test hermético (sem rede) — monkeypatch requests.get
# ---------------------------------------------------------------------------
def _test() -> int:
    saved = requests.get

    class _R:
        def __init__(self, status, payload):
            self.status_code = status
            self._p = payload
        def json(self):
            return self._p

    try:
        # T1: filtro de categoria bate direto -> usa esse resultado, sem
        # cair no fallback de palavra-chave.
        def fake_category_hit(url, params=None, **kw):
            if "/series" in url and params and params.get("category") == "Climate and Weather":
                return _R(200, {"series": [
                    {"ticker": "KXHIGHNY", "title": "Highest temp in NYC",
                     "category": "Climate and Weather"}]})
            return _R(200, {"series": []})
        requests.get = fake_category_hit
        found = discover_weather_series()
        assert len(found) == 1 and found[0]["ticker"] == "KXHIGHNY", found
        assert found[0]["_source"] == "category_filter", found
        print("T1 PASS: filtro de categoria bate -> usa direto (sem fallback)")

        # T2: todas as categorias vazias -> cai no fallback por palavra-chave
        # sobre a lista sem filtro.
        def fake_keyword_fallback(url, params=None, **kw):
            if params and params.get("category"):
                return _R(200, {"series": []})
            return _R(200, {"series": [
                {"ticker": "KXHIGHNY", "title": "Highest Temperature NYC",
                 "category": "Uncategorized"},
                {"ticker": "KXNFLGAME", "title": "NFL Game Winner",
                 "category": "Sports"},
            ]})
        requests.get = fake_keyword_fallback
        found = discover_weather_series()
        tickers = {s["ticker"] for s in found}
        assert tickers == {"KXHIGHNY"}, tickers
        assert found[0]["_source"] == "keyword_fallback", found
        print("T2 PASS: categorias vazias -> fallback por palavra-chave "
              "(NFL descartado, KXHIGHNY mantido)")

        # T3: API inacessível (non-200 em tudo) -> [] gracioso, nunca levanta.
        requests.get = lambda *a, **k: _R(500, {})
        assert discover_weather_series() == []
        print("T3 PASS: API inacessível (500) -> [] gracioso")

        # T4: exceção de rede -> None do _get, sem levantar.
        def _boom(*a, **k):
            raise requests.exceptions.ConnectionError("refused")
        requests.get = _boom
        assert _get("/series") is None
        assert discover_weather_series() == []
        print("T4 PASS: exceção de rede -> None/[] (fail-open, sem raise)")

        # T5: fetch_open_markets parseia volume/oi/bid/ask e spread calculado.
        requests.get = lambda *a, **k: _R(200, {"markets": [
            {"ticker": "KXHIGHNY-26JUL10-B70", "title": "NYC high >= 70F",
             "status": "open", "volume": 1500, "open_interest": 800,
             "yes_bid": 0.42, "yes_ask": 0.47, "last_price": 0.45,
             "close_time": "2026-07-10T23:59:00Z"}]})
        markets = fetch_open_markets("KXHIGHNY")
        assert len(markets) == 1 and markets[0]["volume"] == 1500, markets
        s = _spread(markets[0])
        assert abs(s - 0.05) < 1e-9, s
        print("T5 PASS: fetch_open_markets parseia volume/oi/bid/ask; "
              "spread = ask-bid calculado corretamente (0.05)")

        # T6: _spread gracioso quando bid/ask ausente.
        assert _spread({"yes_bid": None, "yes_ask": 0.5}) is None
        assert _spread({}) is None
        print("T6 PASS: _spread -> None gracioso sem bid/ask")

        # T7: fetch_orderbook repassa o payload cru.
        requests.get = lambda *a, **k: _R(200, {"orderbook": {
            "yes": [[42, 100], [41, 50]], "no": [[58, 80]]}})
        ob = fetch_orderbook("KXHIGHNY-26JUL10-B70")
        assert ob and len(ob["orderbook"]["yes"]) == 2, ob
        print("T7 PASS: fetch_orderbook repassa book yes/no cru")

        # T8: run() agrega series + markets sem tocar Polymarket (flag off).
        state = {"n": 0}
        def fake_run(url, params=None, **kw):
            state["n"] += 1
            if "/series" in url and params and params.get("category") == "Climate and Weather":
                return _R(200, {"series": [{"ticker": "KXHIGHNY",
                                            "title": "NYC high temp"}]})
            if "/series" in url:
                return _R(200, {"series": []})
            if "/markets" in url and "orderbook" not in url:
                return _R(200, {"markets": [
                    {"ticker": "KXHIGHNY-26JUL10-B70", "volume": 100,
                     "open_interest": 50, "yes_bid": 0.3, "yes_ask": 0.35}]})
            return _R(200, {})
        requests.get = fake_run
        rep = run(want_markets=True, want_orderbook=False,
                 want_polymarket_count=False)
        assert len(rep["series"]) == 1 and rep["series"][0]["ticker"] == "KXHIGHNY", rep
        assert len(rep["markets"]) == 1 and rep["markets"][0]["volume"] == 100, rep
        assert rep["polymarket"] is None, rep
        print("T8 PASS: run() agrega series(1) + markets(1); Polymarket "
              "não tocado quando want_polymarket_count=False")

        print("\nAll verify_kalshi_weather self-tests PASS (8/8)")
        return 0
    finally:
        requests.get = saved


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Verifica mercados de clima da Kalshi vs. Polymarket "
                    "(read-only; roda onde a API da Kalshi é acessível).")
    ap.add_argument("--markets", action="store_true",
                    help="lista mercados abertos por série (volume/OI/spread)")
    ap.add_argument("--orderbook", action="store_true",
                    help="+ profundidade de book por mercado (implica --markets)")
    ap.add_argument("--no-polymarket", action="store_true",
                    help="não busca a contagem de mercados da Polymarket")
    ap.add_argument("--json", action="store_true", help="saída JSON crua")
    ap.add_argument("--test", action="store_true",
                    help="self-test hermético (sem rede)")
    args = ap.parse_args()

    if args.test:
        return _test()

    want_markets = args.markets or args.orderbook
    report = run(want_markets=want_markets, want_orderbook=args.orderbook,
                want_polymarket_count=not args.no_polymarket)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report["series"] else 1
    return _print_report(report, want_markets, args.orderbook)


if __name__ == "__main__":
    raise SystemExit(main())
