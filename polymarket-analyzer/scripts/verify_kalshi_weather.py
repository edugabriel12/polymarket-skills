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
    python verify_kalshi_weather.py --debug         # imprime status/corpo cru de
                                                     # cada chamada no stderr —
                                                     # rode isto se "0 séries"
                                                     # persistir, pra eu ver a
                                                     # resposta real da API
    python verify_kalshi_weather.py --test          # self-test hermético (sem rede)

Env vars:
    KALSHI_API_BASE   (se definida, ÚNICO host tentado — sem ela, o script
                       tenta em sequência os dois hosts que a pesquisa
                       encontrou mencionados, já que nenhum foi confirmado
                       ao vivo: trading-api.kalshi.com e
                       api.elections.kalshi.com. Ajuste aqui sem mudar
                       código se a Kalshi tiver migrado para outro.)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests  # noqa: E402  (usado pelos self-tests p/ monkeypatch)

# v4: cliente HTTP extraído para kalshi_market_io.py (fonte única — o
# kalshi_edge_bot usa o mesmo cliente). Este script mantém só o relatório
# de comparação Kalshi vs Polymarket. O estado mutável do cliente
# (_WORKING_BASE/_DEBUG) vive em kio; os self-tests daqui manipulam
# kio._WORKING_BASE / kio._DEBUG diretamente.
import kalshi_market_io as kio  # noqa: E402
from kalshi_market_io import (  # noqa: E402
    _CANDIDATE_BASES,
    _get,
    _safe_list,
    _spread,
    _to_float,
    discover_weather_series,
    fetch_open_markets,
    fetch_orderbook,
    raw_book_sides,
)


def _debug_market_sample(ticker: str | None) -> int:
    """Imprime o JSON CRU de um único mercado aberto — diagnóstico cirúrgico
    e não o --debug genérico (que dispararia um log por cada uma das ~88
    séries × N mercados, inviável de colar de volta).

    Motivo: um run real encontrou 88 séries + 542 mercados corretamente, mas
    volume/open_interest/yes_bid/yes_ask vieram todos None — os nomes de
    campo que assumi (documentação antiga do trading-api, agora
    descontinuado) provavelmente não batem com o schema real do
    api.elections.kalshi.com. Ver o JSON de UM mercado real revela os nomes
    corretos sem gerar um log gigante.

    Sem `ticker` explícito: percorre as séries descobertas EM ORDEM até
    achar uma com mercado aberto — algumas séries descobertas não têm
    mercado aberto agora (ex. ticker antigo/aposentado que a API de séries
    ainda lista, como KXLOWNY vs. a ativa KXLOWTNYC), então parar na
    primeira sem checar as demais falha à toa mesmo havendo outras com
    dados reais.
    """
    candidates = [ticker] if ticker else None
    tried: list[str] = []
    if candidates is None:
        series = discover_weather_series()
        if not series:
            print("Nenhuma série de temperatura encontrada — não dá pra "
                  "amostrar um mercado. Rode --debug pra diagnosticar a "
                  "descoberta de séries primeiro.")
            return 1
        candidates = [s["ticker"] for s in series]

    for tk in candidates:
        tried.append(tk)
        data = _get("/markets", params={"series_ticker": tk,
                                        "status": "open", "limit": 1})
        markets = _safe_list(data, "markets")
        if markets:
            if not ticker:
                print(f"(nenhum --ticker dado; tentativas até achar "
                      f"mercado aberto: {tried})\n")
            print(f"Mercado cru de {tk!r} (cole isto de volta):\n")
            print(json.dumps(markets[0], indent=2, ensure_ascii=False))
            return 0

    print(f"Nenhum mercado aberto encontrado em {len(tried)} série(s) "
          f"tentada(s) ({tried}). Rode --debug pra ver a resposta bruta "
          f"das chamadas, ou passe um --ticker específico.")
    return 1


def _to_float(v) -> float | None:
    """Converte um valor da Kalshi pra float. Os campos reais vêm como
    STRINGS dolarizadas (ex. 'yes_bid_dollars': '0.0100'), não floats/centavos
    inteiros como a doc antiga do trading-api (descontinuado) sugeria —
    confirmado via --debug-market-sample contra api.elections.kalshi.com.
    None/''/inconversível -> None, nunca levanta."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _spread(row: dict) -> float | None:
    """Spread YES a partir do row JÁ CONVERTIDO (yes_bid/yes_ask floats),
    não do dict cru da Kalshi (que usa yes_bid_dollars/yes_ask_dollars)."""
    bid, ask = row.get("yes_bid"), row.get("yes_ask")
    if bid is None or ask is None:
        return None
    return round(ask - bid, 4)


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
                    "volume": _to_float(m.get("volume_fp")),
                    "open_interest": _to_float(m.get("open_interest_fp")),
                    # liquidity_dollars sumiu do payload real (jul/2026) —
                    # tenta as variantes; "liquidity" legado vem em cents.
                    "liquidity": (_to_float(m.get("liquidity_dollars"))
                                  or _to_float(m.get("liquidity_fp"))
                                  or ((_to_float(m.get("liquidity")) or 0) / 100.0
                                      or None)),
                    "yes_bid": _to_float(m.get("yes_bid_dollars")),
                    "yes_ask": _to_float(m.get("yes_ask_dollars")),
                    "no_bid": _to_float(m.get("no_bid_dollars")),
                    "no_ask": _to_float(m.get("no_ask_dollars")),
                    "last_price": _to_float(m.get("last_price_dollars")),
                    "close_time": m.get("close_time"),
                }
                row["spread"] = _spread(row)
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
    liq = row.get("liquidity")
    spread = row.get("spread")
    return (f"    {row.get('market_ticker', '?'):<24} "
            f"vol={vol if vol is not None else '?':<8} "
            f"oi={oi if oi is not None else '?':<8} "
            f"liq=${liq if liq is not None else '?':<8} "
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
                # raw_book_sides entende os dois formatos da API
                # (orderbook_fp/yes_dollars novo e orderbook/yes legado).
                yes_raw, no_raw = raw_book_sides(m["orderbook"])
                print(f"        orderbook: {len(yes_raw)} níveis YES, "
                      f"{len(no_raw)} níveis NO")
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
    saved_working_base, saved_debug = kio._WORKING_BASE, kio._DEBUG
    kio._WORKING_BASE = None  # cada teste começa sem host cacheado

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

        # T5: fetch_open_markets repassa os campos REAIS da Kalshi cru
        # (volume_fp/open_interest_fp contagem-string, yes_bid_dollars/
        # yes_ask_dollars string dolarizada tipo '0.4200' -- confirmado via
        # --debug-market-sample contra api.elections.kalshi.com; a doc
        # antiga do trading-api descontinuado sugeria volume/yes_bid
        # numéricos diretos, o que causava tudo None no run real). _to_float
        # converte; _spread computa em cima do já convertido.
        requests.get = lambda *a, **k: _R(200, {"markets": [
            {"ticker": "KXHIGHNY-26JUL10-B70", "title": "NYC high >= 70F",
             "status": "active", "volume_fp": "1500.00",
             "open_interest_fp": "800.00", "yes_bid_dollars": "0.4200",
             "yes_ask_dollars": "0.4700", "last_price_dollars": "0.4500",
             "close_time": "2026-07-10T23:59:00Z"}]})
        markets = fetch_open_markets("KXHIGHNY")
        assert len(markets) == 1 and markets[0]["volume_fp"] == "1500.00", markets
        assert _to_float(markets[0]["volume_fp"]) == 1500.0
        row = {"yes_bid": _to_float(markets[0]["yes_bid_dollars"]),
              "yes_ask": _to_float(markets[0]["yes_ask_dollars"])}
        s = _spread(row)
        assert abs(s - 0.05) < 1e-9, s
        print("T5 PASS: fetch_open_markets repassa campos reais "
              "(volume_fp/*_dollars); _to_float + _spread = ask-bid "
              "calculado corretamente (0.05)")

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
                    {"ticker": "KXHIGHNY-26JUL10-B70", "volume_fp": "100.00",
                     "open_interest_fp": "50.00", "yes_bid_dollars": "0.3000",
                     "yes_ask_dollars": "0.3500"}]})
            return _R(200, {})
        requests.get = fake_run
        rep = run(want_markets=True, want_orderbook=False,
                 want_polymarket_count=False)
        assert len(rep["series"]) == 1 and rep["series"][0]["ticker"] == "KXHIGHNY", rep
        assert len(rep["markets"]) == 1 and rep["markets"][0]["volume"] == 100.0, rep
        assert abs(rep["markets"][0]["spread"] - 0.05) < 1e-9, rep
        assert rep["polymarket"] is None, rep
        print("T8 PASS: run() agrega series(1) + markets(1) com campos "
              "reais convertidos (volume=100.0, spread=0.05); Polymarket "
              "não tocado quando want_polymarket_count=False")

        # T9: 1o host candidato falha (conexão recusada), 2o responde 200 ->
        # _get tenta em sequência e fixa o 2o em _WORKING_BASE; chamada
        # seguinte vai direto nele (sem retentar o 1o).
        kio._WORKING_BASE = None
        calls = {"first_base_tries": 0, "second_base_tries": 0}
        def fake_multi_base(url, params=None, **kw):
            if url.startswith(_CANDIDATE_BASES[0]):
                calls["first_base_tries"] += 1
                raise requests.exceptions.ConnectionError("refused")
            calls["second_base_tries"] += 1
            return _R(200, {"series": [{"ticker": "KXHIGHNY", "title": "x"}]})
        requests.get = fake_multi_base
        r1 = _get("/series", params={"category": "Climate and Weather"})
        assert r1 == {"series": [{"ticker": "KXHIGHNY", "title": "x"}]}, r1
        assert kio._WORKING_BASE == _CANDIDATE_BASES[1], kio._WORKING_BASE
        assert calls["first_base_tries"] == 1 and calls["second_base_tries"] == 1, calls
        # 2a chamada: so o host cacheado e tentado (1o candidato nao sobe de novo).
        r2 = _get("/series", params={"category": "Weather"})
        assert r2 is not None and calls["first_base_tries"] == 1, calls
        assert calls["second_base_tries"] == 2, calls
        print("T9 PASS: 1o host falha, 2o responde -> fixado em _WORKING_BASE "
              "(chamada seguinte não retenta o 1o)")

        # T10: _safe_list nunca quebra em {'series': None} -- o bug real do
        # run em producao (Kalshi devolve None, nao [], p/ categoria sem
        # resultado; TypeError: 'NoneType' object is not iterable).
        assert _safe_list({"series": None}, "series") == []
        assert _safe_list({}, "series") == []
        assert _safe_list(None, "series") == []
        assert _safe_list({"series": [1, 2]}, "series") == [1, 2]
        print("T10 PASS: _safe_list -> [] gracioso pra {'series': None} "
              "(reproduz o crash real do operador)")

        # T11: filtro de temperatura descarta contrato de clima que NAO e
        # de temperatura (ex. real: 'Climate and Weather' incluindo
        # HURRICANENAME), mesmo vindo do filtro de categoria.
        def fake_mixed_category(url, params=None, **kw):
            if "/series" in url and params and params.get("category") == "Climate and Weather":
                return _R(200, {"series": [
                    {"ticker": "HURRICANENAME", "title": "Named Atlantic Hurricane",
                     "category": "Climate and Weather"},
                    {"ticker": "KXHIGHNY", "title": "Highest Temperature NYC",
                     "category": "Climate and Weather"},
                ]})
            return _R(200, {"series": []})
        requests.get = fake_mixed_category
        found = discover_weather_series()
        tickers = {s["ticker"] for s in found}
        assert tickers == {"KXHIGHNY"}, tickers  # HURRICANENAME descartado
        assert all(s["_source"] == "category_filter" for s in found), found
        print("T11 PASS: filtro de temperatura descarta HURRICANENAME "
              "mesmo vindo do filtro de categoria (mantém só KXHIGHNY)")

        # T12: _debug_market_sample imprime o mercado cru (achando série
        # sozinho quando ticker=None) e retorna 0; [] gracioso -> 1.
        def fake_sample(url, params=None, **kw):
            if "/series" in url:
                return _R(200, {"series": [{"ticker": "KXHIGHNY",
                                            "title": "Highest Temperature NYC"}]})
            if "/markets" in url:
                return _R(200, {"markets": [
                    {"ticker": "KXHIGHNY-26JUL10-T94", "campo_estranho": 42}]})
            return _R(200, {})
        requests.get = fake_sample
        rc = _debug_market_sample(None)
        assert rc == 0, rc
        requests.get = lambda *a, **k: _R(200, {"markets": []})
        rc2 = _debug_market_sample("KXHIGHNY")
        assert rc2 == 1, rc2
        print("T12 PASS: _debug_market_sample acha série sozinho + imprime "
              "mercado cru (0); [] -> 1 gracioso")

        # T13: 1a série descoberta (KXLOWNY) NÃO tem mercado aberto -- o
        # bug real que o operador bateu (dict iterou uma série aposentada
        # primeiro). Deve pular pra 2a série (KXHIGHNY) automaticamente em
        # vez de desistir na 1a.
        def fake_stale_first_series(url, params=None, **kw):
            if "/series" in url:
                return _R(200, {"series": [
                    {"ticker": "KXLOWNY", "title": "Lowest Temperature in NYC"},
                    {"ticker": "KXHIGHNY", "title": "Highest Temperature NYC"},
                ]})
            if "/markets" in url:
                tk = (params or {}).get("series_ticker")
                if tk == "KXLOWNY":
                    return _R(200, {"markets": []})  # série sem mercado aberto
                return _R(200, {"markets": [
                    {"ticker": "KXHIGHNY-26JUL10-T94", "campo_real": True}]})
            return _R(200, {})
        requests.get = fake_stale_first_series
        rc3 = _debug_market_sample(None)
        assert rc3 == 0, rc3
        print("T13 PASS: 1a série descoberta sem mercado aberto -> pula "
              "automaticamente pra 2a com dados (reproduz o KXLOWNY real)")

        print("\nAll verify_kalshi_weather self-tests PASS (13/13)")
        return 0
    finally:
        requests.get = saved
        kio._WORKING_BASE, kio._DEBUG = saved_working_base, saved_debug


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
    ap.add_argument("--debug", action="store_true",
                    help="imprime status/corpo cru de cada chamada HTTP no "
                        "stderr (rode isto se '0 séries' persistir)")
    ap.add_argument("--debug-market-sample", nargs="?", const="",
                    metavar="TICKER",
                    help="imprime o JSON cru de UM mercado aberto (ticker "
                        "de série opcional; sem ele usa a 1a série de "
                        "temperatura descoberta) e sai — rode isto se "
                        "volume/oi/bid/ask vierem vazios, pra eu ver os "
                        "nomes reais dos campos sem um log de 500+ linhas")
    ap.add_argument("--test", action="store_true",
                    help="self-test hermético (sem rede)")
    args = ap.parse_args()

    if args.test:
        return _test()

    if args.debug_market_sample is not None:
        return _debug_market_sample(args.debug_market_sample or None)

    if args.debug:
        kio._DEBUG = True

    want_markets = args.markets or args.orderbook
    report = run(want_markets=want_markets, want_orderbook=args.orderbook,
                want_polymarket_count=not args.no_polymarket)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report["series"] else 1
    return _print_report(report, want_markets, args.orderbook)


if __name__ == "__main__":
    raise SystemExit(main())
