#!/usr/bin/env python3
"""Verifica a estação de resolução REAL dos mercados de temperatura europeus da
Polymarket e compara com o dict `stations` do weather-cities.json.

Contexto (2026-07): o bot quase não converte apostas na Europa. Uma das causas
é config de estação: Berlim estava com EDDT (Tegel, fechado em nov/2020), e
Madrid/Warsaw/Istanbul/Ankara nem estavam no dict `stations` (caíam no geocode
por nome, que falha → forecast_unavailable). A estação de resolução correta NÃO
pode ser adivinhada — ela está escrita na seção "Rules"/description de cada
mercado. Este utilitário LÊ essa descrição via Gamma (a mesma API que o bot já
usa) e extrai a estação com os mesmos regexes de weather_edge_helpers, para o
operador confirmar/curar o weather-cities.json com dados reais.

READ-ONLY: não escreve nada. Roda no host onde a Gamma é acessível (desktop/VPS
do operador). Requer o venv do projeto.

Uso:
    python verify_eu_stations.py                      # cidades EU padrão
    python verify_eu_stations.py --cities Berlin Madrid
    python verify_eu_stations.py --min-volume 0 --json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import weather_edge_helpers as weh  # noqa: E402
from weather_edge_bot import fetch_weather_markets  # noqa: E402

# Cidades europeias-alvo (nome canônico como aparece no slug/pergunta).
_DEFAULT_EU = [
    # já curadas (validar)
    "London", "Paris", "Berlin", "Madrid", "Warsaw", "Istanbul", "Ankara",
    # v16 — Leste/Centro EU
    "Prague", "Vienna", "Budapest", "Bratislava", "Zagreb", "Belgrade",
    "Bucharest", "Athens", "Sofia", "Kyiv", "Vilnius", "Riga", "Tallinn",
    # v16 — Escandinávia
    "Oslo", "Stockholm", "Copenhagen", "Helsinki", "Reykjavik",
    # v16 — Itália + Portugal
    "Rome", "Milan", "Naples", "Lisbon", "Porto",
    # v17 — piloto África (desérticas/subtropicais; resolução via METAR real).
    # Confirmar station/coords das Rules ANTES de operar, como as demais.
    "Cairo", "Casablanca", "Algiers", "Tunis", "Johannesburg",
    # v18 — candidatos América do Sul (deep-research 2026-07: NENHUMA cidade
    # curada ainda; nenhum modelo regional Open-Meteo na região; nem sequer
    # confirmamos se existe mercado ativo). Grafia ASCII sem acento (Sao
    # Paulo/Bogota/Montevideo/Brasilia/Asuncion) de propósito: _matches_city
    # é um substring-match sensível a acento contra o texto do mercado, que a
    # Polymarket escreve em inglês/ASCII — "São Paulo" não bateria com "Sao
    # Paulo" no texto real. Rodar isto é o único jeito de resolver o ângulo
    # 'qual estação' que a pesquisa web não consegue (ver
    # references/south-america-research-notes.md).
    "Sao Paulo", "Rio de Janeiro", "Buenos Aires", "Bogota", "Lima",
    "Santiago", "Caracas", "Quito", "Montevideo", "La Paz", "Brasilia",
    "Asuncion",
]


def _market_text(m: dict) -> str:
    """Junta os campos textuais úteis de um mercado da Gamma (as 'Rules'
    costumam estar em description; question/slug ajudam a casar a cidade)."""
    return " ".join(str(m.get(k) or "") for k in
                    ("question", "description", "slug", "title"))


def _matches_city(m: dict, city: str) -> bool:
    text = _market_text(m).lower()
    if city.lower() not in text:
        return False
    # restringe a mercados de temperatura (evita 'rain'/outros)
    return "temperature" in text or "temp" in (m.get("slug") or "").lower()


def verify(cities: list[str], min_volume: float = 0.0) -> list[dict]:
    """Para cada cidade, acha o mercado de temperatura mais recente na Gamma,
    extrai a estação da description e compara com o weather-cities.json.
    Retorna uma lista de dicts (um por cidade) — puro relatório."""
    cfg = weh.load_cities()
    stations = cfg.get("stations", {})
    try:
        markets = fetch_weather_markets(min_volume=min_volume)
    except Exception as e:
        print(f"[erro] fetch_weather_markets falhou: {e}", file=sys.stderr)
        markets = []

    out = []
    for city in cities:
        cur = stations.get(city)
        rep = {
            "city": city,
            "configured": ({"station": cur.get("station"),
                            "lat": cur.get("lat"), "lon": cur.get("lon")}
                           if cur else None),
            "market_found": False,
            "extracted": None,
            "extracted_icao": None,
            "market_slug": None,
            "rules_excerpt": None,
            "verdict": "no_market",
        }
        cands = [m for m in markets if _matches_city(m, city)]
        if cands:
            m = cands[0]
            rep["market_found"] = True
            rep["market_slug"] = m.get("slug")
            desc = str(m.get("description") or "")
            # trecho da regra que menciona 'station/recorded/reported'
            mo = re.search(r"[^.]*\b(?:recorded|reported|measured|station|"
                           r"observatory|resolve)\b[^.]*\.", desc, re.I)
            rep["rules_excerpt"] = (mo.group(0).strip()[:300] if mo
                                    else desc[:300])
            auto = weh.auto_extract_station(city, cfg, desc)
            if auto:
                rep["extracted_icao"] = auto.get("station")
                rep["extracted"] = {"station": auto.get("station"),
                                    "lat": auto.get("lat"),
                                    "lon": auto.get("lon")}
            # ICAO cru na descrição (fallback bruto, mesmo sem estar no dict)
            icaos = re.findall(r"\(([A-Z]{3,4})\)", desc)
            if icaos:
                rep["icao_in_rules"] = icaos

            # veredito
            if cur and rep["extracted_icao"] == cur.get("station"):
                rep["verdict"] = "match"
            elif rep["extracted_icao"] or icaos:
                rep["verdict"] = "mismatch_or_new"
            else:
                rep["verdict"] = "unparsed_rules"
        out.append(rep)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Verifica estações EU da Polymarket vs weather-cities.json "
                    "(read-only, roda onde a Gamma é acessível).")
    ap.add_argument("--cities", nargs="*", default=_DEFAULT_EU,
                    help="cidades a verificar (default: EU-alvo)")
    ap.add_argument("--min-volume", type=float, default=0.0)
    ap.add_argument("--json", action="store_true", help="saída JSON crua")
    args = ap.parse_args()

    reps = verify(args.cities, min_volume=args.min_volume)
    if args.json:
        print(json.dumps(reps, indent=2, ensure_ascii=False))
        return

    print("Verificação de estações EU (Polymarket Gamma vs weather-cities.json)\n")
    for r in reps:
        cfg = (f"{r['configured']['station']} "
               f"({r['configured']['lat']},{r['configured']['lon']})"
               if r["configured"] else "— AUSENTE do dict stations —")
        print(f"● {r['city']:10s} configurado: {cfg}")
        if not r["market_found"]:
            print("    sem mercado de temperatura ativo encontrado agora\n")
            continue
        print(f"    mercado: {r['market_slug']}")
        print(f"    regra:   {r['rules_excerpt']}")
        ex = (f"{r['extracted_icao']}" if r.get("extracted_icao")
              else (f"ICAO cru {r.get('icao_in_rules')}"
                    if r.get("icao_in_rules") else "não parseada"))
        flag = {"match": "✅ bate",
                "mismatch_or_new": "⚠ DIVERGE / novo — curar no JSON",
                "unparsed_rules": "❓ regra não parseada — inspecionar manual",
                "no_market": ""}.get(r["verdict"], "")
        print(f"    extraído da regra: {ex}   {flag}\n")

    print("Ação: para cada ⚠/❓, ajuste stations[<city>] em "
          "polymarket-analyzer/references/weather-cities.json com o ICAO/coords "
          "da regra, e adicione o mapeamento em station_names.")


if __name__ == "__main__":
    main()
