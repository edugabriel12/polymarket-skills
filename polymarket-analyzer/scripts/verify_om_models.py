#!/usr/bin/env python3
"""Verifica as chaves de modelo do Open-Meteo configuradas em
`weather-cities.json` (`stations[*].om_models`).

Contexto (2026-07): os PRs #165-167 generalizaram o ensemble para N modelos
regionais por-cidade (icon_d2, icon_eu, meteofrance_arpege_europe,
metno_seamless, dmi_harmonie_arome_europe, italia_meteo_arpae_icon_2i, ...). Uma
chave inválida NÃO quebra o bot — `fetch_open_meteo_ensemble` faz 400 → fallback
ao trio global (icon/gfs/ecmwf) e loga `open_meteo_model_fallback`. Mas esse
fallback é SILENCIOSO: a cidade opera com o trio achando que tem o modelo
regional. Como o proxy deste ambiente bloqueia a Open-Meteo (403), as chaves
nunca foram validadas aqui. Este utilitário roda no host do operador (onde a
Open-Meteo é acessível) e confirma, ANTES de operar, que cada chave é (a) um
identificador válido de modelo e (b) que o modelo cobre a cidade a que foi
atribuído (um modelo regional é válido globalmente mas pode não cobrir a cidade,
ex.: icon_d2 não cobre Atenas).

READ-ONLY: não escreve nada. Mesma disciplina do verify_eu_stations.py.
Espelha exatamente o endpoint/params que o bot usa
(weather_edge_helpers.fetch_open_meteo_ensemble), então "válido aqui" == "o bot
vai usar o modelo em vez de cair no trio".

v2 (2026-07-09, post-mortem "terminal parecia travado"): um --per-city varre
~145 chamadas HTTP sequenciais. Antes desta versão nada era impresso até o
final, o que numa rodada real ficou minutos em silêncio total — indistinguível
de travado. Agora cada resultado é impresso com um contador [i/N] assim que
acontece (default; `--quiet` restaura o dump-tudo-no-final). Também: uma rodada
real revelou "Connection aborted"/ConnectionResetError transitórios (rede,
não a chave/cidade) — agora há 1 retry automático nesses casos antes de marcar
como falha.

Uso:
    python verify_om_models.py                 # 1 probe por chave distinta
    python verify_om_models.py --per-city      # cada par (cidade, modelo)
    python verify_om_models.py --models icon_d2 icon_eu
    python verify_om_models.py --quiet         # sem progresso ao vivo (dump no final)
    python verify_om_models.py --json
    python verify_om_models.py --test          # self-test hermético (sem rede)

v3 (2026-07-10, expansão Kalshi): `--kalshi` carrega
references/kalshi-cities.json (20 cidades US do bot Kalshi) no lugar do
weather-cities.json; `--candidates M1 M2` pré-valida chaves AINDA NÃO
configuradas em nenhum om_models, probando cada uma em TODAS as cidades da
config carregada (implica --per-city). É o passo obrigatório ANTES de
editar om_models na config:

    python verify_om_models.py --kalshi --candidates gfs_hrrr ncep_nbm_conus
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests  # noqa: E402
import weather_edge_helpers as weh  # noqa: E402

OM_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

KALSHI_CITIES_PATH = (Path(__file__).resolve().parent.parent
                      / "references" / "kalshi-cities.json")


def _load_kalshi_stations() -> dict:
    """kalshi-cities.json adaptado ao shape {'stations': {...}} que run()
    espera — as entradas já usam os mesmos nomes de campo (lat/lon/station/
    om_models), só mudam de 'cities' para 'stations'."""
    data = json.loads(KALSHI_CITIES_PATH.read_text(encoding="utf-8"))
    return {"stations": data.get("cities") or {}}

# Classificação heurística do `reason` de um 400 da Open-Meteo. As mensagens
# variam, então sempre imprimimos o reason cru; isto só rotula para o resumo.
_INVALID_KEY_HINTS = ("not found", "cannot initialize", "does not exist",
                      "invalid model", "unknown model", "available models")
_OUT_OF_DOMAIN_HINTS = ("no data", "not available", "for this location",
                        "out of", "outside", "does not cover", "no grid")


def _classify_reason(reason: str) -> str:
    r = (reason or "").lower()
    if any(h in r for h in _OUT_OF_DOMAIN_HINTS):
        return "out_of_domain"
    if any(h in r for h in _INVALID_KEY_HINTS):
        return "invalid_key"
    return "unknown_4xx"


def _get_with_retry(url: str, params: dict, timeout: int = 20,
                    retries: int = 1, backoff: float = 0.5):
    """requests.get with a short retry on transient connection failures.

    A --per-city run fires ~100+ sequential requests at the same host; in
    practice a handful reset mid-sequence (ConnectionResetError) with nothing
    wrong with the model/city — a real-world run surfaced 4 of these out of
    145 calls. Retrying once turns a false '⚠ revisar' into the ✅ it should
    be. Deterministic HTTP responses (200/4xx) never need a retry — only
    exceptions from the transport layer do. Raises the last exception after
    exhausting retries; the caller (probe_model) catches it same as before.
    """
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return requests.get(url, params=params, timeout=timeout)
        except requests.exceptions.RequestException as e:
            last_exc = e
            if attempt < retries:
                time.sleep(backoff)
    raise last_exc


def probe_model(model_key: str, lat: float, lon: float,
                date_iso: str) -> dict:
    """Uma chamada à Open-Meteo para UM modelo em (lat, lon) no dia-alvo.

    Retorna um dict-relatório com status classificado. Nunca levanta.
    status ∈ {valid, no_data, out_of_domain, invalid_key, unknown_4xx, error}.
    Não usa fetch_open_meteo_ensemble de propósito: o fallback-ao-trio de lá
    MASCARARIA uma chave ruim (devolveria dados do trio). Aqui isolamos 1 chave.
    """
    rep = {"model": model_key, "lat": lat, "lon": lon,
           "http": None, "status": "error", "reason": None,
           "n_hours": 0, "sample_max_c": None}
    try:
        r = _get_with_retry(
            OM_FORECAST_URL,
            params={
                "latitude": lat, "longitude": lon,
                "models": model_key,
                "hourly": "temperature_2m",
                "temperature_unit": "celsius",
                "start_date": date_iso, "end_date": date_iso,
            },
            timeout=20,
        )
    except Exception as e:  # rede/DNS/timeout, após esgotar o retry
        rep["reason"] = f"request_failed: {e}"
        return rep

    rep["http"] = r.status_code
    if r.status_code == 200:
        try:
            series = (r.json().get("hourly") or {}).get("temperature_2m") or []
        except Exception as e:
            rep["status"], rep["reason"] = "error", f"bad_json: {e}"
            return rep
        clean = [v for v in series if v is not None]
        rep["n_hours"] = len(clean)
        if clean:
            rep["status"] = "valid"
            rep["sample_max_c"] = round(max(clean), 2)
        else:
            # 200 mas série vazia/toda-nula: chave aceita porém sem grade no
            # ponto → tratamos como cobertura ausente (suspeito).
            rep["status"] = "no_data"
        return rep

    # não-200: extrai o reason da Open-Meteo (JSON {"error":true,"reason":...})
    reason = None
    try:
        reason = (r.json() or {}).get("reason")
    except Exception:
        reason = (r.text or "")[:200]
    rep["reason"] = reason
    rep["status"] = _classify_reason(reason) if 400 <= r.status_code < 500 \
        else "error"
    return rep


def _distinct_keys(stations: dict) -> dict:
    """{model_key: [cidades que a usam]} preservando ordem de inserção."""
    out: dict[str, list] = {}
    for city, s in stations.items():
        if not isinstance(s, dict):
            continue
        for k in (s.get("om_models") or []):
            out.setdefault(k, []).append(city)
    return out


def _tomorrow_iso() -> str:
    return (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()


def _fmt(rep: dict) -> str:
    mark = {"valid": "✅", "no_data": "⚠", "out_of_domain": "⚠",
            "invalid_key": "❌", "unknown_4xx": "❓", "error": "‽"}
    tag = mark.get(rep["status"], "?")
    extra = ""
    if rep["status"] == "valid":
        extra = f"max={rep['sample_max_c']}°C n={rep['n_hours']}h"
    elif rep.get("reason"):
        extra = str(rep["reason"])[:80]
    return (f"    {tag} {rep['model']:<34} http={rep['http']} "
            f"{rep['status']:<13} {extra}")


def run(cities: dict, *, per_city: bool, only_models: list | None,
        date_iso: str, progress: bool = False,
        candidates: list | None = None) -> dict:
    """Probe the trio baseline, each distinct om_models key, and (if per_city)
    every (city, model) pair. Sequential — a --per-city sweep over the full
    config is ~145 HTTP calls, each up to 20s on failure.

    progress=True prints each result AS IT HAPPENS (with a running [i/N]
    counter), instead of silently collecting everything and dumping it all at
    the end. A real run against production config surfaced 145 sequential
    calls with zero feedback in between — indistinguishable from a hang, and
    the exact bug report this addresses. progress=False keeps the original
    silent behavior for callers (e.g. the self-test) that don't want stdout
    noise; --quiet on the CLI restores it for interactive use too.
    """
    stations = cities.get("stations", {})
    by_key = _distinct_keys(stations)
    if only_models:
        by_key = {k: v for k, v in by_key.items() if k in only_models}
    # Candidatos: chaves AINDA NÃO configuradas em nenhum om_models —
    # pré-validação antes de editar a config. Cada candidato ganha uma probe
    # por-chave (na primeira cidade) E entra em todos os pares por-cidade.
    if candidates:
        for k in candidates:
            by_key.setdefault(k, list(stations.keys()))

    # Baseline: o trio global DEVE ser sempre válido. Se falhar, o problema é
    # rede/API do host, não as chaves regionais.
    trio = list(weh.DEFAULT_OM_MODELS)
    # ponto de referência central-europeu para o baseline (Londres se existir)
    ref = stations.get("London") or next(
        (s for s in stations.values() if isinstance(s, dict)), None)
    ref_lat = (ref or {}).get("lat", 51.5)
    ref_lon = (ref or {}).get("lon", 0.0)

    per_city_pairs = []
    if per_city:
        for city, s in stations.items():
            if not isinstance(s, dict):
                continue
            models = s.get("om_models") or []
            if candidates:
                models = list(dict.fromkeys(list(models) + list(candidates)))
            if only_models:
                models = [m for m in models if m in only_models]
            if models:
                per_city_pairs.append((city, s, models))

    total = len(trio) + len(by_key) + sum(len(m) for _, _, m in per_city_pairs)
    done = 0

    def _stream(rep: dict, suffix: str = "") -> dict:
        nonlocal done
        done += 1
        if progress:
            print(f"[{done}/{total}] {_fmt(rep).lstrip()}{suffix}", flush=True)
        return rep

    report = {"date": date_iso, "baseline": [], "per_key": [], "per_city": []}

    if progress:
        print(f"Verificação de chaves de modelo Open-Meteo "
              f"(dia-alvo {date_iso}) — {total} chamadas HTTP no total\n")
        print("● Baseline (trio global — DEVE ser tudo ✅):")
    for mk in trio:
        report["baseline"].append(
            _stream(probe_model(mk, ref_lat, ref_lon, date_iso)))
    if progress and not all(r["status"] == "valid" for r in report["baseline"]):
        print("\n  ⚠ O trio global falhou — provável problema de rede/API no "
              "host (proxy? sem internet?), NÃO as chaves regionais. "
              "Resolva isto antes de interpretar o resto.\n")

    # Uma probe por chave distinta, na primeira cidade que a usa (coords que a
    # config afirma estarem no domínio do modelo).
    if progress:
        print("\n● Por chave distinta (1 probe na cidade representante):")
    for mk, city_list in by_key.items():
        rep_city = city_list[0]
        s = stations[rep_city]
        rep = probe_model(mk, s["lat"], s["lon"], date_iso)
        rep["representative_city"] = rep_city
        rep["n_cities"] = len(city_list)
        _stream(rep, suffix=f"   [{rep_city} · {len(city_list)} cidades]")
        report["per_key"].append(rep)

    if per_city:
        if progress:
            print("\n● Por (cidade, modelo) — cobertura de domínio:")
        for city, s, models in per_city_pairs:
            probes = [probe_model(m, s["lat"], s["lon"], date_iso)
                      for m in models]
            if progress:
                bad = [p for p in probes if p["status"] != "valid"]
                flag = "  ⚠ revisar" if bad else ""
                print(f"  {city} ({s.get('station')}){flag}")
                for p in probes:
                    _stream(p)
            else:
                done += len(probes)
            report["per_city"].append({"city": city, "station": s.get("station"),
                                        "probes": probes})
    return report


def _print_bodies(report: dict, per_city: bool) -> None:
    """Full non-streamed dump of every probe — used only with --quiet, where
    run() was called with progress=False and nothing was printed yet."""
    print(f"Verificação de chaves de modelo Open-Meteo "
          f"(dia-alvo {report['date']})\n")

    print("● Baseline (trio global — DEVE ser tudo ✅):")
    for rep in report["baseline"]:
        print(_fmt(rep))
    baseline_ok = all(r["status"] == "valid" for r in report["baseline"])
    if not baseline_ok:
        print("\n  ⚠ O trio global falhou — provável problema de rede/API no "
              "host (proxy? sem internet?), NÃO as chaves regionais. "
              "Resolva isto antes de interpretar o resto.\n")

    print("\n● Por chave distinta (1 probe na cidade representante):")
    for rep in sorted(report["per_key"], key=lambda r: r["model"]):
        line = _fmt(rep)
        print(f"{line}   [{rep.get('representative_city')} · "
              f"{rep.get('n_cities')} cidades]")

    if per_city:
        print("\n● Por (cidade, modelo) — cobertura de domínio:")
        for row in report["per_city"]:
            bad = [p for p in row["probes"] if p["status"] != "valid"]
            flag = "  ⚠ revisar" if bad else ""
            print(f"  {row['city']} ({row['station']}){flag}")
            for p in row["probes"]:
                print(_fmt(p))


def _print_summary(report: dict) -> int:
    # Resumo/veredito
    baseline_ok = all(r["status"] == "valid" for r in report["baseline"])
    invalid = [r for r in report["per_key"] if r["status"] == "invalid_key"]
    domain = [r for r in report["per_key"]
              if r["status"] in ("out_of_domain", "no_data")]
    unknown = [r for r in report["per_key"] if r["status"] == "unknown_4xx"]
    print("\nResumo:")
    print(f"  chaves distintas: {len(report['per_key'])} | "
          f"✅ válidas: {sum(1 for r in report['per_key'] if r['status']=='valid')} | "
          f"❌ inválidas: {len(invalid)} | "
          f"⚠ domínio/sem-dado: {len(domain)} | ❓ 4xx-desconhecido: {len(unknown)}")
    if invalid:
        print("\n  ❌ CHAVES INVÁLIDAS (o bot cai no trio silenciosamente — "
              "corrija o om_models em weather-cities.json):")
        for r in invalid:
            print(f"     - {r['model']}  ({r['reason']})")
    if domain:
        print("\n  ⚠ Sem cobertura na cidade representante (o modelo pode ser "
              "válido mas não cobrir a cidade — reveja a atribuição ou rode "
              "--per-city):")
        for r in domain:
            print(f"     - {r['model']} @ {r.get('representative_city')} "
                  f"({r['status']})")
    if not invalid and not domain and not unknown and baseline_ok:
        print("  ✅ Todas as chaves de modelo são válidas e cobrem as cidades "
              "representantes.")
    print("\n(READ-ONLY — nenhuma alteração feita. Ajuste stations[*].om_models "
          "em weather-cities.json para qualquer ❌.)")
    # Exit code: 1 se houver chave inválida OU baseline quebrado (acionável).
    return 1 if (invalid or not baseline_ok) else 0


# ---------------------------------------------------------------------------
# Self-test hermético (sem rede) — monkeypatch requests.get
# ---------------------------------------------------------------------------
def _test() -> int:
    saved = requests.get

    class _R:
        def __init__(self, status, payload):
            self.status_code = status
            self._p = payload
            self.text = json.dumps(payload) if isinstance(payload, dict) else str(payload)
        def json(self):
            if isinstance(self._p, Exception):
                raise self._p
            return self._p

    try:
        # T1: 200 com série → valid + sample_max
        requests.get = lambda *a, **k: _R(
            200, {"hourly": {"temperature_2m": [18.0, 24.5, 21.0, None]}})
        r = probe_model("icon_eu", 51.5, 0.0, "2026-07-10")
        assert r["status"] == "valid" and r["sample_max_c"] == 24.5 \
            and r["n_hours"] == 3, r
        print("T1 PASS: 200 com série → valid (max 24.5, n=3)")

        # T2: 200 com série toda-nula → no_data (aceito mas sem grade no ponto)
        requests.get = lambda *a, **k: _R(
            200, {"hourly": {"temperature_2m": [None, None]}})
        r = probe_model("icon_d2", 37.9, 23.9, "2026-07-10")
        assert r["status"] == "no_data" and r["sample_max_c"] is None, r
        print("T2 PASS: 200 série nula → no_data")

        # T3: 400 chave inválida → invalid_key
        requests.get = lambda *a, **k: _R(
            400, {"error": True, "reason": "Model 'foo_bar' not found. Available models: ..."})
        r = probe_model("foo_bar", 51.5, 0.0, "2026-07-10")
        assert r["status"] == "invalid_key" and r["http"] == 400, r
        print("T3 PASS: 400 'not found' → invalid_key")

        # T4: 400 fora de domínio → out_of_domain
        requests.get = lambda *a, **k: _R(
            400, {"error": True, "reason": "No data is available for this location"})
        r = probe_model("icon_d2", 37.9, 23.9, "2026-07-10")
        assert r["status"] == "out_of_domain", r
        print("T4 PASS: 400 'no data for this location' → out_of_domain")

        # T5: 400 reason desconhecido → unknown_4xx (mas reason preservado)
        requests.get = lambda *a, **k: _R(400, {"reason": "rate limited"})
        r = probe_model("icon_eu", 51.5, 0.0, "2026-07-10")
        assert r["status"] == "unknown_4xx" and r["reason"] == "rate limited", r
        print("T5 PASS: 400 reason desconhecido → unknown_4xx (reason preservado)")

        # T6: exceção de rede → error (nunca levanta)
        def _boom(*a, **k):
            raise requests.exceptions.ConnectTimeout("timeout")
        requests.get = _boom
        r = probe_model("icon_eu", 51.5, 0.0, "2026-07-10")
        assert r["status"] == "error" and "request_failed" in r["reason"], r
        print("T6 PASS: exceção de rede → error (fail-open, sem raise)")

        # T7: run() agrega baseline + per_key sem tocar rede real (mock valid)
        requests.get = lambda *a, **k: _R(
            200, {"hourly": {"temperature_2m": [20.0, 25.0]}})
        cities = {"stations": {
            "London": {"lat": 51.5, "lon": 0.0, "station": "EGLC",
                       "om_models": ["icon_eu", "ecmwf_ifs025"]},
            "Rome": {"lat": 41.8, "lon": 12.2, "station": "LIRF",
                     "om_models": ["italia_meteo_arpae_icon_2i", "icon_eu"]},
        }}
        rep = run(cities, per_city=True, only_models=None, date_iso="2026-07-10")
        assert len(rep["baseline"]) == 3, rep            # trio
        keys = {r["model"] for r in rep["per_key"]}
        assert keys == {"icon_eu", "ecmwf_ifs025", "italia_meteo_arpae_icon_2i"}, keys
        assert all(r["status"] == "valid" for r in rep["per_key"]), rep
        assert len(rep["per_city"]) == 2, rep
        # icon_eu usado por 2 cidades → n_cities == 2 na entrada representante
        icon_eu = next(r for r in rep["per_key"] if r["model"] == "icon_eu")
        assert icon_eu["n_cities"] == 2 and icon_eu["representative_city"] == "London", icon_eu
        print("T7 PASS: run() agrega baseline(3) + per_key(distintas) + per_city(2)")

        # T8: retry recupera de UM reset de conexão transitório — o modo de
        # falha real do run de 2026-07-09 (4 "Connection aborted" em 145
        # chamadas). 1a chamada levanta ConnectionError, 2a sucede.
        calls = {"n": 0}
        def _flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise requests.exceptions.ConnectionError(
                    "('Connection aborted.', ConnectionResetError(10054, ...))")
            return _R(200, {"hourly": {"temperature_2m": [22.0, 30.5]}})
        requests.get = _flaky
        r = probe_model("gfs_seamless", 52.5, 13.4, "2026-07-10")
        assert r["status"] == "valid" and r["sample_max_c"] == 30.5, r
        assert calls["n"] == 2, calls
        print("T8 PASS: reset de conexão na 1a tentativa, retry recupera -> "
              "valid (não vira falso '⚠ revisar' por blip transitório)")

        # T9 (v3): candidates — chave NÃO configurada em nenhum om_models é
        # pré-validada em TODAS as cidades (shape kalshi: om_models null).
        requests.get = lambda *a, **k: _R(
            200, {"hourly": {"temperature_2m": [20.0, 25.0]}})
        kcities = {"stations": {
            "Seattle": {"lat": 47.44, "lon": -122.31, "station": "KSEA",
                        "om_models": None},
            "Dallas": {"lat": 32.90, "lon": -97.04, "station": "KDFW",
                       "om_models": None},
        }}
        rep9 = run(kcities, per_city=True, only_models=None,
                   date_iso="2026-07-10",
                   candidates=["gfs_hrrr", "ncep_nbm_conus"])
        keys9 = {r["model"] for r in rep9["per_key"]}
        assert keys9 == {"gfs_hrrr", "ncep_nbm_conus"}, keys9
        assert len(rep9["per_city"]) == 2, rep9["per_city"]
        for entry in rep9["per_city"]:
            probed = {p["model"] for p in entry["probes"]}
            assert probed == {"gfs_hrrr", "ncep_nbm_conus"}, (entry["city"],
                                                              probed)
        # loader do kalshi-cities.json real: 20 cidades no shape stations
        ks = _load_kalshi_stations()
        assert len(ks["stations"]) == 20, len(ks["stations"])
        assert ks["stations"]["Dallas"]["station"] == "KDFW"
        print("T9 PASS: --candidates proba chaves não-configuradas em todas "
              "as cidades; _load_kalshi_stations carrega as 20 (KDFW ok)")

        print("\nAll verify_om_models self-tests PASS (9/9)")
        return 0
    finally:
        requests.get = saved


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Verifica as chaves de modelo Open-Meteo do "
                    "weather-cities.json (read-only; roda onde a Open-Meteo é "
                    "acessível).")
    ap.add_argument("--per-city", action="store_true",
                    help="probar cada par (cidade, modelo) — cobertura de domínio")
    ap.add_argument("--models", nargs="*", default=None,
                    help="restringe às chaves dadas (já configuradas)")
    ap.add_argument("--kalshi", action="store_true",
                    help="usa references/kalshi-cities.json (bot Kalshi) em "
                         "vez do weather-cities.json")
    ap.add_argument("--candidates", nargs="*", default=None,
                    help="chaves AINDA NÃO configuradas para pré-validar em "
                         "TODAS as cidades da config (implica --per-city). "
                         "Ex.: --kalshi --candidates gfs_hrrr ncep_nbm_conus")
    ap.add_argument("--date", default=None,
                    help="dia-alvo YYYY-MM-DD (default: amanhã UTC)")
    ap.add_argument("--json", action="store_true", help="saída JSON crua")
    ap.add_argument("--quiet", action="store_true",
                    help="não imprime progresso em tempo real; junta tudo no "
                        "final como antes (útil ao redirecionar para arquivo)")
    ap.add_argument("--test", action="store_true",
                    help="self-test hermético (sem rede)")
    args = ap.parse_args()

    if args.test:
        return _test()

    date_iso = args.date or _tomorrow_iso()
    cities = _load_kalshi_stations() if args.kalshi else weh.load_cities()
    stream = not args.json and not args.quiet
    report = run(cities, per_city=args.per_city or bool(args.candidates),
                 only_models=args.models, date_iso=date_iso, progress=stream,
                 candidates=args.candidates)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        # ainda retorna código de saída acionável
        invalid = any(r["status"] == "invalid_key" for r in report["per_key"])
        baseline_ok = all(r["status"] == "valid" for r in report["baseline"])
        return 1 if (invalid or not baseline_ok) else 0
    if not stream:
        _print_bodies(report, args.per_city)
    else:
        print()  # espaçamento antes do resumo, como no dump não-streamed
    return _print_summary(report)


if __name__ == "__main__":
    raise SystemExit(main())
