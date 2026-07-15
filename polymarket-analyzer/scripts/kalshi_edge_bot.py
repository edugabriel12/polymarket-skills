#!/usr/bin/env python3
"""Kalshi weather edge bot — paper trading nos mercados de temperatura da
Kalshi para as 11 cidades dos EUA com estação de resolução confirmada
(references/kalshi-cities.json; pesquisa em kalshi-us-cities-playbook.md).

Daemon análogo ao weather_edge_bot (Polymarket), reutilizando o pipeline
venue-agnóstico existente — forecast/ensemble (_compute_mae_for_market),
probabilidade (forecast_probability), edge (compute_edge), triggers de
cashout (evaluate_cashout_triggers), risk gate (_risk_block_reason) e o
schema do weather_edge_db — trocando APENAS a camada de I/O de mercado
(kalshi_market_io) e a fonte de settlement (a própria Kalshi).

Diferenças estruturais vs o bot Polymarket:
  - DB PRÓPRIO: ~/.polymarket-paper/kalshi_edge.db (mesmo schema), via
    WEATHER_EDGE_DB_PATH setado ANTES de importar weather_edge_db. O judge
    roda numa segunda instância apontada pro mesmo env (ver
    agent/kalshi-edge-judge.service).
  - BANCA PRÓPRIA: PaperEngine(portfolio="kalshi") — criar antes com
    `paper_engine.py --action init --name kalshi --balance 1000`.
  - MarketSpec vem dos campos ESTRUTURADOS da Kalshi (floor/cap_strike,
    strike_type, data do event_ticker) — parser_confidence 0.95, sem regex
    de título. O spec é embutido em discovery_meta_json p/ o judge.
  - FEE: taker 0.07·P·(1−P) por contrato — descontada do edge ANTES do
    gate de edge mínimo (constituição regra 7) e aplicada no paper fill
    via fee_rate = 0.07·(1−P).
  - CONTRATOS INTEIROS: floor() no sizing; <1 contrato = skip.
  - pilot=True em TODAS as entries (semântica do piloto África): o judge
    força review LLM completo enquanto a venue é nova.
  - RESOLUÇÃO: decide APENAS pelo settlement da própria Kalshi
    (GET /markets/{ticker} → status settled/finalized + result yes/no) —
    nunca por preço. O CLI do NWS pode atrasar (~11h ET em inconsistência
    com METAR): mercado não settled fica para o próximo sweep.
    observed_value via METAR da estação é SÓ log/counterfactual (METAR≈CLI
    por arredondamento; dia UTC≈dia LST — aproximações documentadas).
  - Sem ladder no v1 (brackets da Kalshi são candidatos naturais, mas o
    laddering é a maior máquina de complexidade do bot Polymarket — fica
    para uma fase futura, depois de calibrar o básico).

Paper-only: não há caminho de execução live neste daemon (ver
kalshi_live_stub.py para o esqueleto de auth RSA-PSS, não ativado).

Uso:
    python kalshi_edge_bot.py --once --dry-run   # 1 ciclo, sem executar
    python kalshi_edge_bot.py --once             # 1 ciclo com paper real
    python kalshi_edge_bot.py --daemon           # loop (systemd)
    python kalshi_edge_bot.py --test-discovery | --test-execute |
        --test-monitor | --test-resolution | --test-lst-date |
        --test-caution-cost | --test-logging

Env:
    WEATHER_EDGE_DB_PATH  (default ~/.polymarket-paper/kalshi_edge.db —
                           setado por este módulo ANTES dos imports
                           compartilhados; exporte para apontar outro DB)
    KALSHI_API_BASE       (repassada ao kalshi_market_io)
    OPENWEATHER_API_KEY   (forecast, via get_weather.py)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import signal
import sys
import time
from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# Windows: stdout pipado cai em cp1252 (sem ●/→/≥) — força UTF-8, mesma
# proteção do weather_edge_judge.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

# ---------------------------------------------------------------------------
# FENCE: o override do DB precisa acontecer ANTES de qualquer import de
# weather_edge_db (direto ou via weather_edge_bot). weather_edge_db lê
# WEATHER_EDGE_DB_PATH na importação; importar antes desta linha faria o
# bot Kalshi escrever no weather_edge.db do bot Polymarket.
# ---------------------------------------------------------------------------
DEFAULT_KALSHI_DB = Path.home() / ".polymarket-paper" / "kalshi_edge.db"
os.environ.setdefault("WEATHER_EDGE_DB_PATH", str(DEFAULT_KALSHI_DB))

import requests  # noqa: E402

import weather_edge_db as db  # noqa: E402
import weather_edge_bot as web  # noqa: E402
import kalshi_market_io as kio  # noqa: E402
from weather_edge_helpers import (  # noqa: E402
    MarketSpec,
    compute_edge,
    compute_max_size_for_slippage,
    evaluate_cashout_triggers,
    fetch_metar_daily_extremes,
    forecast_probability,
    forecast_ref_value,
    prob_yes_for_sizing,
)

# Log JSONL próprio (o log_event do weather_edge_bot lê web.LOG_FILE em
# tempo de chamada — mesmo mecanismo do --log-file de lá).
KALSHI_LOG_FILE = Path.home() / ".polymarket-paper" / "kalshi_edge.jsonl"
web.LOG_FILE = KALSHI_LOG_FILE
log_event = web.log_event
_now_iso = web._now_iso


# log_event já espelha cada evento no JSONL E no terminal. O que escapava do
# arquivo era um crash FORA do loop principal (startup, args, import): o
# traceback ia só para o stderr — num terminal do Windows que fecha, a
# evidência morre. O excepthook injeta o traceback no JSONL antes do print
# padrão. Ctrl+C fica de fora (encerramento normal, não é crash).
def _log_uncaught(exc_type, exc, tb):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    import traceback as _tb
    try:
        log_event("error", {
            "where": "kalshi_uncaught",
            "err": f"{exc_type.__name__}: {exc}",
            "traceback": "".join(
                _tb.format_exception(exc_type, exc, tb))[-4000:],
        }, level="ERROR")
    except Exception:
        pass  # logging nunca pode mascarar o traceback real
    sys.__excepthook__(exc_type, exc, tb)


sys.excepthook = _log_uncaught

STRATEGY = "kalshi_weather"

_shutdown = False


# ---------------------------------------------------------------------------
# Helpers de config/spec/meta
# ---------------------------------------------------------------------------

def _station_from_cfg(city_cfg: dict) -> dict:
    """Dict de estação no shape que _compute_mae_for_market espera (mesmas
    chaves do stations do weather-cities.json)."""
    return {
        "lat": city_cfg.get("lat"),
        "lon": city_cfg.get("lon"),
        "station": city_cfg.get("station"),
        "temp_bias_f": float(city_cfg.get("temp_bias_f") or 0.0),
        "om_models": city_cfg.get("om_models"),
        "pilot": True,
    }


def _spec_to_meta(spec: MarketSpec) -> dict:
    d = asdict(spec)
    d["target_date"] = (spec.target_date.isoformat()
                        if spec.target_date else None)
    return d


def _spec_from_meta(meta: dict) -> Optional[MarketSpec]:
    """Reconstrói o MarketSpec estruturado gravado no discovery — a mesma
    função que o judge usa via _entry_spec. Fail-open None."""
    d = (meta or {}).get("spec")
    if not d:
        return None
    try:
        d = dict(d)
        if d.get("target_date"):
            d["target_date"] = date.fromisoformat(d["target_date"])
        return MarketSpec(**d)
    except Exception:
        return None


def _meta_load(row) -> dict:
    try:
        raw = row["discovery_meta_json"]
        return json.loads(raw) if raw else {}
    except (KeyError, IndexError, TypeError, ValueError):
        return {}


def _make_engine(portfolio: str):
    """Lazy import do paper_engine (hook monkeypatchável nos testes)."""
    from paper_engine import PaperEngine
    return PaperEngine(portfolio=portfolio)


def _paper_close(args, token_id: str, side: str, reasoning: str,
                 force_exit_price: float, fee_rate: float = 0.0):
    """Fecha posição paper SEMPRE via force_exit_price — o caminho normal do
    close_position busca o book na CLOB da Polymarket, que não conhece
    tickers Kalshi. Hook monkeypatchável nos testes."""
    import paper_engine
    return paper_engine.close_position(
        token_id=token_id, side=side, portfolio_name=args.portfolio,
        reasoning=reasoning, force_exit_price=force_exit_price,
        fee_rate=fee_rate)


def _set_position_price(args, token_id: str, price: float,
                        side: Optional[str] = None) -> int:
    """Hook monkeypatchável: paper_engine.set_position_price."""
    import paper_engine
    return paper_engine.set_position_price(
        token_id, price, portfolio_name=args.portfolio, side=side)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _series_targets(kcities: dict) -> list[tuple[str, str, str]]:
    """[(series_ticker, city, temp_kind)] — séries explícitas da config +
    séries descobertas via /series mapeáveis a cidades configuradas (cobre
    as mínimas cujo ticker exato não foi confirmado na pesquisa)."""
    targets: dict[str, tuple[str, str, str]] = {}
    for name, c in (kcities.get("cities") or {}).items():
        for key, kind in (("series_high", "high"), ("series_low", "low")):
            tk = c.get(key)
            if tk:
                targets[tk.upper()] = (tk.upper(), name, kind)
    try:
        discovered = kio.discover_weather_series()
    except Exception as e:
        log_event("warn", {"where": "kalshi_series_discovery",
                           "err": str(e)}, level="WARN")
        discovered = []
    for s in discovered:
        tk = (s.get("ticker") or "").upper()
        if not tk or tk in targets:
            continue
        mapped = (kio.series_to_city(tk, kcities)
                  or kio.match_series_by_title(s.get("title") or "", kcities))
        if mapped:
            targets[tk] = (tk, mapped[0], mapped[1])
            log_event("kalshi_series_discovered", {
                "series": tk, "city": mapped[0], "kind": mapped[1],
                "title": s.get("title")})
    return list(targets.values())


def run_discovery_kalshi(args, kcities: dict) -> int:
    """Varre as séries das 11 cidades, constrói specs estruturados, calcula
    edge LÍQUIDO de fee e propõe entries (status=PROPOSED) para o judge."""
    proposed = 0
    skipped: dict[str, int] = defaultdict(int)
    targets = _series_targets(kcities)
    log_event("kalshi_discovery_start", {"n_series": len(targets)})

    with db.connect() as conn:
        pending = {(r["market_slug"], r["side"]) for r in conn.execute(
            "SELECT market_slug, side FROM entries "
            "WHERE status IN ('PROPOSED','APPROVED','ADJUSTED')")}
        live_tokens = db.query_live_tokens(conn)
        # Cooldown de re-proposta: REJECTED/SKIPPED saía do dedup e o mesmo
        # (ticker, side) voltava a ser proposto no discovery seguinte — o
        # judge re-julgava o MESMO mercado a cada hora (~$0.10/review) até
        # estourar o budget diário (visto no smoke: 196 entries numa noite,
        # $21 queimados nos mesmos ~10 mercados reciclados).
        cooldown_h = float(getattr(args, "reproposal_cooldown_hours", 6))
        cutoff = (datetime.now(timezone.utc) -
                  timedelta(hours=cooldown_h)).isoformat()
        cooled = {(r["market_slug"], r["side"]) for r in conn.execute(
            "SELECT market_slug, side FROM entries "
            "WHERE status IN ('REJECTED','SKIPPED') AND ts >= ?", (cutoff,))}

    forecast_cache: dict[str, dict] = {}

    for series_tk, city, kind in targets:
        ccfg = (kcities.get("cities") or {}).get(city)
        if not ccfg:
            continue
        markets = kio.fetch_open_markets(series_tk, limit=args.max_markets_per_series)
        for m in markets:
            ticker = m.get("ticker")
            if not ticker:
                continue
            end_date_str = str(m.get("close_time") or "")
            ttr = web._ttr_hours(end_date_str)
            if ttr <= args.min_ttr_hours:
                skipped["ttr_below_min"] += 1
                continue
            if ttr > args.window_hours:
                skipped["outside_window"] += 1
                continue
            vol = kio._to_float(m.get("volume_fp")) or 0.0
            if vol < args.min_volume:
                skipped["low_volume"] += 1
                continue
            spec = kio.build_market_spec(m, city, kind)
            if spec is None:
                skipped["spec_unparseable"] += 1
                log_event("kalshi_spec_unparseable", {
                    "ticker": ticker, "strike_type": m.get("strike_type"),
                    "event_ticker": m.get("event_ticker")}, level="WARN")
                continue
            implied = kio.implied_from_market_row(m)
            if implied["yes_ask"] is None and implied["no_ask"] is None:
                skipped["no_quotes"] += 1
                continue

            fc = forecast_cache.get(city)
            if fc is None:
                fc = web.fetch_forecast(city, lat=ccfg["lat"],
                                        lon=ccfg["lon"]) or {}
                forecast_cache[city] = fc
            if not fc:
                skipped["forecast_unavailable"] += 1
                continue

            station = _station_from_cfg(ccfg)
            mae_dyn, bias, mu_over, mae_meta = web._compute_mae_for_market(
                spec, fc, args, station=station)
            p_yes = forecast_probability(spec, fc, mae_override=mae_dyn,
                                         bias_override=bias,
                                         mu_override=mu_over)
            if p_yes is None:
                skipped["no_probability"] += 1
                continue

            edge = compute_edge(p_yes, implied)
            side = edge["best_side"]
            if side is None:
                skipped["low_edge"] += 1
                continue
            entry_price = (implied["yes_ask"] if side == "YES"
                           else implied["no_ask"])
            if entry_price is None:
                skipped["no_quotes"] += 1
                continue
            if entry_price < args.min_entry_price:
                skipped["entry_too_cheap"] += 1
                continue
            if entry_price > args.max_entry_price:
                skipped["entry_too_expensive"] += 1
                continue

            # Constituição regra 7: fee come edge. Gate sobre o LÍQUIDO.
            fee_pp = kio.kalshi_fee_pp(entry_price)
            edge_net = edge["edge_pp_at_best"] - fee_pp
            if edge_net < args.min_edge_pp:
                skipped["low_edge_after_fee"] += 1
                try:
                    with db.connect() as conn:
                        db.insert_discovery_skip(
                            conn, ts=_now_iso(), slug=ticker, city=city,
                            reason="low_edge_after_fee",
                            meta_json=json.dumps({
                                "edge_pp_gross": edge["edge_pp_at_best"],
                                "fee_pp": round(fee_pp, 3),
                                "edge_pp_net": round(edge_net, 3),
                                "min_edge_pp": args.min_edge_pp,
                                "side": side}))
                except Exception:
                    pass
                continue

            if (ticker, side) in pending:
                skipped["duplicate_pending"] += 1
                continue
            if (ticker, side) in cooled:
                skipped["reproposal_cooldown"] += 1
                continue
            if ticker in live_tokens:
                skipped["duplicate_token_open"] += 1
                continue

            p_side = p_yes if side == "YES" else 1.0 - p_yes
            meta = {
                **(mae_meta or {}),
                "venue": "kalshi",
                "pilot": True,
                "risk_notes": ccfg.get("risk_notes"),
                "lat": ccfg.get("lat"),
                "lon": ccfg.get("lon"),
                "station_icao": ccfg.get("station"),
                "wfo": ccfg.get("wfo"),
                "timezone": ccfg.get("timezone"),
                "series_ticker": series_tk,
                "event_ticker": m.get("event_ticker"),
                "fee_pp": round(fee_pp, 4),
                "edge_pp_gross": edge["edge_pp_at_best"],
                "spec": _spec_to_meta(spec),
            }
            with db.connect() as conn:
                entry_id = db.insert_entry(
                    conn,
                    ts=_now_iso(),
                    market_slug=ticker,
                    market_question=spec.raw_question[:300],
                    condition_id=m.get("event_ticker"),
                    # Kalshi tem UM ticker por mercado binário; o lado vive
                    # na posição do paper engine (keyed token+side).
                    token_id_yes=ticker,
                    token_id_no=ticker,
                    end_date=end_date_str,
                    side=side,
                    entry_price=entry_price,
                    forecast_prob_at_entry=p_side,   # P(side), convenção do DB
                    implied_prob_at_entry=entry_price,
                    edge_pp_at_entry=round(edge_net, 4),  # LÍQUIDO de fee
                    forecast_snapshot_json=fc,
                    parser_confidence=spec.confidence,
                    city_resolved=city,
                    threshold_value=spec.threshold_value,
                    threshold_unit=spec.threshold_unit,
                    comparison=spec.comparison,
                    ttr_hours_at_entry=ttr,
                    status="PROPOSED",
                    strategy=STRATEGY,
                    # event_ticker no slot genérico de evento: alimenta o cap
                    # de exposição por evento no execute.
                    ladder_event_slug=m.get("event_ticker"),
                    discovery_meta_json=meta,
                )
            pending.add((ticker, side))
            proposed += 1
            log_event("kalshi_entry_proposed", {
                "entry_id": entry_id, "ticker": ticker, "side": side,
                "entry_price": entry_price, "edge_pp_net": round(edge_net, 2),
                "edge_pp_gross": edge["edge_pp_at_best"],
                "fee_pp": round(fee_pp, 2), "city": city,
                "ttr_h": round(ttr, 1)})

    log_event("kalshi_discovery_end", {"proposed": proposed,
                                       "skipped_breakdown": dict(skipped)})
    return proposed


# ---------------------------------------------------------------------------
# Execute
# ---------------------------------------------------------------------------

def _event_exposure_usd(conn, event_ticker: str) -> float:
    """Exposição aberta somada em TODOS os mercados de um mesmo evento
    Kalshi (ex. todas as brackets de KXHIGHNY-26JUL12). Espelha
    current_market_exposure_usd, agregando por ladder_event_slug."""
    if not event_ticker:
        return 0.0
    row = conn.execute(
        "SELECT COALESCE(SUM(e.size_usd), 0) FROM entries e "
        "LEFT JOIN cashouts    c ON c.entry_id = e.entry_id "
        "LEFT JOIN resolutions r ON r.entry_id = e.entry_id "
        "WHERE e.ladder_event_slug = ? "
        "  AND e.status IN ('EXECUTED','FAST_PATH') "
        "  AND c.cashout_id IS NULL AND r.resolution_id IS NULL",
        (event_ticker,)).fetchone()
    return float(row[0] or 0.0)


def run_execute_kalshi(args) -> int:
    """Executa entries APPROVED/ADJUSTED pelo judge via paper engine
    (portfolio kalshi), com re-check de edge líquido de fee, sizing por
    slippage do book Kalshi e CONTRATOS INTEIROS."""
    executed = 0
    with db.connect() as conn:
        rows = db.query_approved_unexecuted(conn)
    rows = [r for r in rows
            if (r["strategy"] if "strategy" in r.keys() else None) == STRATEGY]
    if not rows:
        return 0

    try:
        engine = _make_engine(args.portfolio)
    except Exception as e:
        log_event("error", {"where": "kalshi_execute",
                            "err": f"paper_engine: {e}"}, level="ERROR")
        return 0

    block_reason = web._risk_block_reason(engine, args)
    if block_reason:
        log_event("kalshi_risk_halt_block",
                  {"reason": block_reason, "n_pending": len(rows)},
                  level="WARN")
        return 0

    for row in rows:
        entry_id = row["entry_id"]
        status = row["status"]

        # Expiry guard (mesma lição do v15.2 do bot Polymarket).
        end_date_str = row["end_date"] if "end_date" in row.keys() else None
        if end_date_str:
            try:
                _end = datetime.fromisoformat(
                    str(end_date_str).replace("Z", "+00:00"))
                _expired = _end < datetime.now(timezone.utc)
            except (ValueError, TypeError):
                _expired = False
            if _expired:
                log_event("kalshi_execute_skipped", {
                    "entry_id": entry_id,
                    "reason": "expired_before_execute",
                    "end_date": end_date_str})
                with db.connect() as conn2:
                    db.update_entry_status(conn2, entry_id, "SKIPPED",
                                           skip_reason="expired_before_execute")
                continue

        adjusted_side = (row["judge_adjusted_side"]
                         if status == "ADJUSTED" else None)
        side = adjusted_side or row["side"]
        ticker = row["token_id_yes"]  # Kalshi: mesmo ticker p/ os dois lados

        raw_book = kio.fetch_orderbook(ticker)
        book = kio.normalize_orderbook(raw_book, side)
        if not book.get("asks"):
            log_event("kalshi_execute_skipped", {"entry_id": entry_id,
                                                 "reason": "no_orderbook"})
            continue

        sizing = compute_max_size_for_slippage(book, "BUY",
                                               max_slippage=args.max_slippage)
        if sizing["max_shares"] <= 0:
            log_event("kalshi_execute_skipped", {"entry_id": entry_id,
                                                 "reason": "zero_max_size"})
            continue

        fill_price = float(sizing["avg_fill"])

        # Banda de preço re-aplicada na EXECUÇÃO: a banda do discovery valeu
        # para o preço de então. Se o mercado desabou até aqui (ex.: fill a
        # $0.01 no dia da liquidação), o re-check de edge até MELHORA — mas o
        # colapso significa que o mercado tem informação intraday que o
        # ensemble não tem; comprar nesse momento é seleção adversa (smoke:
        # entries 62/71 executaram a $0.01 e a 62 já liquidou em -100%).
        # Skip PERMANENTE — com o cooldown de re-proposta, não ressuscita.
        if not (float(args.min_entry_price) <= fill_price
                <= float(args.max_entry_price)):
            log_event("kalshi_execute_skipped", {
                "entry_id": entry_id, "reason": "price_out_of_band",
                "fill_price": round(fill_price, 4), "side": side,
                "band": [args.min_entry_price, args.max_entry_price]})
            with db.connect() as conn2:
                db.update_entry_status(conn2, entry_id, "SKIPPED",
                                       skip_reason="price_out_of_band")
            continue

        forecast_prob = row["forecast_prob_at_entry"]
        if forecast_prob is not None and side != row["side"]:
            forecast_prob = 1.0 - float(forecast_prob)

        # Re-check de edge LÍQUIDO de fee ao preço de fill atual.
        if forecast_prob is None:
            current_edge_pp = None
        else:
            current_edge_pp = ((float(forecast_prob) - fill_price) * 100.0
                               - kio.kalshi_fee_pp(fill_price))
        if (current_edge_pp is not None
                and current_edge_pp < args.execute_min_edge_pp):
            log_event("kalshi_execute_skipped", {
                "entry_id": entry_id, "reason": "edge_stale",
                "current_edge_pp_net": round(current_edge_pp, 2),
                "fill_price": round(fill_price, 4), "side": side})
            with db.connect() as conn2:
                db.update_entry_status(conn2, entry_id, "SKIPPED",
                                       skip_reason="edge_stale")
            continue

        target_usd = float(sizing["max_usd"])

        judge_size_cap = (row["judge_adjusted_size_usd"]
                          if status == "ADJUSTED" else None)
        if judge_size_cap is not None and judge_size_cap > 0:
            target_usd = min(target_usd, float(judge_size_cap))

        # Caps de exposição: por mercado (ticker) e por evento (todas as
        # brackets do mesmo dia/cidade compartilham o risco do mesmo
        # desfecho meteorológico).
        with db.connect() as conn2:
            mkt_exposure = db.current_market_exposure_usd(conn2, ticker)
            evt_ticker = (row["ladder_event_slug"]
                          if "ladder_event_slug" in row.keys() else None)
            evt_exposure = _event_exposure_usd(conn2, evt_ticker)
        remaining_mkt = float(args.max_market_exposure_usd) - mkt_exposure
        remaining_evt = float(args.max_event_exposure_usd) - evt_exposure
        remaining_cap = min(remaining_mkt, remaining_evt)
        if remaining_cap <= 0:
            log_event("kalshi_execute_skipped", {
                "entry_id": entry_id, "reason": "exposure_cap_reached",
                "market_exposure_usd": round(mkt_exposure, 2),
                "event_exposure_usd": round(evt_exposure, 2)})
            continue
        target_usd = min(target_usd, remaining_cap)

        # CONTRATOS INTEIROS: Kalshi negocia contratos de 1¢ a 99¢; nada de
        # shares fracionários irrealistas no paper.
        contracts = math.floor(min(float(sizing["max_shares"]),
                                   target_usd / fill_price))
        if contracts < 1:
            log_event("kalshi_execute_skipped", {
                "entry_id": entry_id, "reason": "below_one_contract",
                "depth_shares": sizing["max_shares"],
                "target_usd": round(target_usd, 2)})
            continue
        size_usd = round(contracts * fill_price, 4)
        if size_usd < args.min_trade_usd:
            # SKIPPED permanente, não retry: a entry ficava APPROVED/ADJUSTED
            # e era re-tentada a CADA loop (~1/min) — 5.9k eventos numa noite.
            # Pior: cap de ADJUST $10 + mínimo $10 + floor de contratos é
            # estruturalmente inexecutável (fills caem em $9.2-9.9), e o
            # único "escape" era o preço desabar até caber — exatamente
            # quando não se deve comprar (seleção adversa).
            log_event("kalshi_execute_skipped", {
                "entry_id": entry_id, "reason": "size_below_min",
                "size_usd": size_usd, "min_trade_usd": args.min_trade_usd})
            with db.connect() as conn2:
                db.update_entry_status(conn2, entry_id, "SKIPPED",
                                       skip_reason="size_below_min")
            continue

        fee_rate = kio.kalshi_fee_rate(fill_price)

        if args.dry_run:
            log_event("kalshi_execute_dry_run", {
                "entry_id": entry_id, "side": side, "contracts": contracts,
                "size_usd": size_usd, "avg_fill": fill_price,
                "fee_rate": round(fee_rate, 4)})
            with db.connect() as conn2:
                db.update_entry_status(conn2, entry_id, "EXECUTED",
                                       size_usd=size_usd,
                                       size_shares=contracts,
                                       entry_price=fill_price,
                                       **web._side_flip_extras(row, side))
            executed += 1
            continue

        try:
            result = engine.open_position(
                token_id=ticker,
                side=side,
                size_usd=size_usd,
                market_question=str(row["market_question"] or "")[:200],
                fee_rate=fee_rate,
                confidence=0.65,
                reasoning=f"kalshi_edge_bot entry_id={entry_id}",
                price=fill_price,
            )
            with db.connect() as conn2:
                if result.get("status") == "executed":
                    db.update_entry_status(conn2, entry_id, "EXECUTED",
                                           size_usd=result.get("cost_usd"),
                                           size_shares=result.get("shares_filled"),
                                           entry_price=result.get("avg_price"),
                                           **web._side_flip_extras(row, side))
                    log_event("kalshi_entry_executed", {
                        "entry_id": entry_id, "ticker": ticker, "side": side,
                        "contracts": contracts,
                        "avg_price": result.get("avg_price"),
                        "fee": result.get("fee")})
                    executed += 1
                else:
                    log_event("kalshi_execute_rejected", {
                        "entry_id": entry_id,
                        "reason": result.get("reason")})
                    db.update_entry_status(
                        conn2, entry_id, "SKIPPED",
                        skip_reason=str(result.get("reason"))[:200])
        except Exception as e:
            log_event("error", {"where": "kalshi_open_position",
                                "entry_id": entry_id, "err": str(e)})

    return executed


# ---------------------------------------------------------------------------
# Monitor / cashout
# ---------------------------------------------------------------------------

_last_monitor_per_entry: dict[int, float] = {}


def run_monitor_tick_kalshi(args, kcities: dict) -> None:
    """Checa posições abertas (cadência adaptativa por TTR) e dispara
    cashout quando um trigger fecha. NÃO reusa _do_monitor_check do bot
    Polymarket (ele re-parseia o título e busca book na CLOB): o spec vem
    estruturado de discovery_meta_json e o book da Kalshi."""
    now_mono = time.monotonic()
    with db.connect() as conn:
        rows = [dict(r) for r in db.query_open_positions(conn,
                                                         strategy=STRATEGY)]
    for row in rows:
        entry_id = row["entry_id"]
        ttr_h = web._ttr_hours(row["end_date"] or "")
        interval = web._monitor_interval_for_ttr(ttr_h)
        # Sentinela None = nunca checada → checa JÁ. O default 0 antigo
        # conflitava com time.monotonic() (segundos desde o BOOT): numa
        # máquina recém-reiniciada monotonic < interval e todas as posições
        # eram puladas até o uptime alcançar o intervalo (30-60 min).
        last = _last_monitor_per_entry.get(entry_id)
        if last is not None and now_mono - last < interval:
            continue
        _last_monitor_per_entry[entry_id] = now_mono
        try:
            _do_monitor_check_kalshi(row, kcities, args)
        except Exception as e:
            log_event("error", {"where": "kalshi_monitor_row",
                                "entry_id": entry_id, "err": str(e)},
                      level="ERROR")


def _do_monitor_check_kalshi(row, kcities: dict, args) -> None:
    entry_id = row["entry_id"]
    side = row["side"]
    meta = _meta_load(row)
    spec = _spec_from_meta(meta)
    if spec is None:
        log_event("kalshi_monitor_check", {"entry_id": entry_id,
                                           "decision": "HOLD",
                                           "reason": "no_spec_in_meta"})
        return

    city = row["city_resolved"] or spec.city
    ccfg = (kcities.get("cities") or {}).get(city) or {}
    lat = meta.get("lat", ccfg.get("lat"))
    lon = meta.get("lon", ccfg.get("lon"))

    forecast = web.fetch_forecast(city, lat=lat, lon=lon)
    if not forecast:
        log_event("kalshi_monitor_check", {"entry_id": entry_id,
                                           "decision": "HOLD",
                                           "reason": "no_forecast"})
        return

    station = _station_from_cfg({**ccfg, "lat": lat, "lon": lon})
    mae_dyn, bias, mu_over, mae_meta = web._compute_mae_for_market(
        spec, forecast, args, station=station)
    ens_cal = bool(mae_meta.get("ensemble_calibrated"))
    p_yes = forecast_probability(spec, forecast, mae_override=mae_dyn,
                                 bias_override=bias, mu_override=mu_over)
    if p_yes is None:
        return
    p_yes = prob_yes_for_sizing(p_yes, side, spec.comparison,
                                ensemble_calibrated=ens_cal)

    ticker = row["token_id_yes"]
    book = kio.normalize_orderbook(kio.fetch_orderbook(ticker), side)
    if not book.get("bids") and not book.get("asks"):
        return
    bid = book["bids"][0]["price"] if book.get("bids") else 0.0
    ask = book["asks"][0]["price"] if book.get("asks") else 0.0
    entry_price = float(row["entry_price"])

    prev_peak = float(row["peak_bid_seen"] or 0.0)
    peak = max(prev_peak, bid)

    forecast_ref = forecast_ref_value(spec, forecast)
    verdict = evaluate_cashout_triggers(
        side=side,
        entry_price=entry_price,
        current_bid=bid,
        peak_bid_seen=peak,
        forecast_prob_yes=p_yes,
        profit_lock_pp=args.profit_lock_pp,
        trailing_drawdown_pct=args.trailing_drawdown_pct,
        convergence_pp=args.convergence_pp,
        comparison=spec.comparison,
        forecast_value=forecast_ref,
        range_low=spec.threshold_value,
        range_high=spec.threshold_value_high,
    )
    decision = verdict["decision"]
    reason = f"{verdict['trigger']}: {verdict['reason']}"
    forecast_prob_now = p_yes if side == "YES" else 1.0 - p_yes

    with db.connect() as conn2:
        if bid > prev_peak:
            conn2.execute(
                "UPDATE entries SET peak_bid_seen = ?, peak_bid_seen_at = ? "
                "WHERE entry_id = ?", (bid, _now_iso(), entry_id))
        db.insert_monitor_check(
            conn2, entry_id=entry_id, ts=_now_iso(),
            forecast_prob_now=forecast_prob_now,
            forecast_snapshot_json=forecast,
            market_best_bid=bid, market_best_ask=ask,
            decision=decision, decision_reason=reason)

    # Mantém current_price/drawdown honestos no portfolio kalshi (o refresh
    # automático via CLOB não conhece tickers Kalshi).
    try:
        _set_position_price(args, ticker, bid, side=side)
    except Exception as e:
        log_event("warn", {"where": "kalshi_set_position_price",
                           "entry_id": entry_id, "err": str(e)}, level="WARN")

    log_event("kalshi_monitor_check", {
        "entry_id": entry_id, "decision": decision,
        "trigger": verdict["trigger"], "bid": bid, "peak": peak,
        "forecast_prob_now": forecast_prob_now})

    if decision == "CASHOUT":
        _do_cashout_kalshi(row, book, forecast, forecast_prob_now, args,
                           reason)


def _do_cashout_kalshi(row, book: dict, forecast: dict,
                       forecast_prob_now: float, args, reason: str) -> None:
    entry_id = row["entry_id"]
    side = row["side"]
    ticker = row["token_id_yes"]

    # Liquidez de saída: walk dos bids da Kalshi. Book fino → segura (não
    # despeja num book vazio).
    sell = compute_max_size_for_slippage(book, "SELL", args.max_slippage)
    held_shares = float(row["size_shares"] or 0.0)
    if sell["max_shares"] <= 0 or (held_shares > 0
                                   and sell["max_shares"] < held_shares):
        log_event("kalshi_cashout_blocked", {
            "entry_id": entry_id, "held_shares": held_shares,
            "sell_max_shares": sell["max_shares"]}, level="WARN")
        return
    exit_price = float(sell["avg_fill"])

    if args.dry_run:
        log_event("kalshi_cashout_dry_run", {"entry_id": entry_id,
                                             "exit_price": exit_price})
        return

    try:
        from paper_engine import NoOpenPositionError
    except ImportError:
        NoOpenPositionError = RuntimeError  # type: ignore

    try:
        result = _paper_close(args, ticker, side,
                              f"kalshi_edge_bot: {reason}"[:200],
                              force_exit_price=exit_price,
                              fee_rate=kio.kalshi_fee_rate(exit_price))
        result = web._normalize_close_result(result)
        if result.get("status") == "closed":
            with db.connect() as conn2:
                db.insert_cashout(
                    conn2, entry_id=entry_id, ts=_now_iso(),
                    exit_price=result.get("avg_sell_price") or exit_price,
                    exit_shares=result.get("shares_sold"),
                    realized_pnl_usd=result.get("realized_pnl"),
                    forecast_prob_at_exit=forecast_prob_now,
                    forecast_snapshot_json=forecast,
                    reason=reason[:200])
            log_event("kalshi_cashout_executed", {
                "entry_id": entry_id, "exit_price": exit_price,
                "pnl": result.get("realized_pnl")})
        else:
            log_event("kalshi_cashout_rejected", {
                "entry_id": entry_id, "reason": result.get("reason")})
    except NoOpenPositionError as e:
        # Posição já fechada por outro caminho → marcador terminal para
        # esta entry parar de tentar (self-heal do bot Polymarket).
        with db.connect() as conn2:
            web._insert_phantom_cashout(conn2, entry_id, forecast,
                                        forecast_prob_now, str(e))
        log_event("kalshi_cashout_phantom", {"entry_id": entry_id,
                                             "reason": str(e)}, level="WARN")
    except Exception as e:
        log_event("error", {"where": "kalshi_cashout",
                            "entry_id": entry_id, "err": str(e)})


# ---------------------------------------------------------------------------
# Resolution sweep — settlement pela própria Kalshi
# ---------------------------------------------------------------------------

_SETTLED_STATUSES = {"settled", "finalized"}


# Arquivo do IEM (Iowa Environmental Mesonet) para o produto CLI — a MESMA
# fonte que liquida o mercado (NWS Daily Climate Report), com o dia
# climatológico LST correto. Dois endpoints: o serviço /api/1 e o cli.py
# legado como fallback (shapes de resposta ligeiramente diferentes).
IEM_CLI_API = "https://mesonet.agron.iastate.edu/api/1/cli.json"
IEM_CLI_LEGACY = "https://mesonet.agron.iastate.edu/json/cli.py"


def fetch_iem_cli(icao: str, target_date: date) -> Optional[dict]:
    """Valores do CLI arquivados pelo IEM para (estação, dia LST).

    Retorna {"high_f", "low_f", "source"} ou None (fail-open — a decisão de
    settlement NUNCA depende disto; é verdade-terra para log/counterfactual
    e calibração). Parser defensivo: aceita lista em "data" (api/1) ou
    "results" (cli.py), campos "M"/None tratados como ausentes."""
    d_iso = target_date.isoformat()

    def _pick(rows) -> Optional[dict]:
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            if not str(r.get("valid") or "").startswith(d_iso):
                continue
            hi = kio._to_float(r.get("high"))
            lo = kio._to_float(r.get("low"))
            if hi is not None or lo is not None:
                return {"high_f": hi, "low_f": lo}
        return None

    try:
        r = requests.get(IEM_CLI_API,
                         params={"station": icao, "date": d_iso}, timeout=15)
        if r.status_code == 200:
            body = r.json() or {}
            got = _pick(body.get("data") or body.get("results"))
            if got:
                got["source"] = "iem_cli_api"
                return got
    except Exception:
        pass
    try:
        r = requests.get(IEM_CLI_LEGACY,
                         params={"station": icao,
                                 "year": target_date.year}, timeout=20)
        if r.status_code == 200:
            body = r.json() or {}
            got = _pick(body.get("results") or body.get("data"))
            if got:
                got["source"] = "iem_cli_legacy"
                return got
    except Exception:
        pass
    return None


def _observed_value_for_kalshi(row) -> tuple[Optional[float], Optional[str]]:
    """(observed_value, fonte) — APENAS log/counterfactual/calibração; a
    decisão de settlement NUNCA usa isto.

    Preferência: IEM CLI (arquivo do MESMO produto que liquida o mercado,
    dia climatológico LST exato) → METAR como fallback (aproximações
    documentadas: METAR≈CLI no arredondamento/QC e dia UTC ≈ dia LST)."""
    meta = _meta_load(row)
    icao = meta.get("station_icao")
    spec_d = meta.get("spec") or {}
    tgt = spec_d.get("target_date")
    kind = spec_d.get("temp_kind") or "high"
    if not icao or not tgt:
        return None, None
    tgt_d = date.fromisoformat(tgt)
    try:
        cli = fetch_iem_cli(icao, tgt_d)
    except Exception:
        cli = None
    if cli:
        v = cli.get("low_f" if kind == "low" else "high_f")
        if v is not None:
            return float(v), cli.get("source", "iem_cli")
    try:
        obs = fetch_metar_daily_extremes(icao, tgt_d)
    except Exception:
        obs = None
    if not obs:
        return None, None
    v = obs.get("observed_min_f" if kind == "low" else "observed_max_f")
    if v is None:
        return None, None
    return float(v), "metar"


def run_resolution_sweep_kalshi(args) -> int:
    """Resolve entries passadas do end_date pelo settlement REAL da Kalshi:
    GET /markets/{ticker} → status settled/finalized + result yes/no.
    Nunca decide por preço; não-settled fica para o próximo sweep (o CLI
    do NWS pode atrasar a determinação em ~horas)."""
    resolved = 0
    with db.connect() as conn:
        rows = [r for r in db.query_unresolved_past_end(conn, _now_iso())
                if (r["strategy"] if "strategy" in r.keys() else None)
                == STRATEGY]
    log_event("kalshi_resolution_sweep_started",
              {"unresolved_past_end": len(rows)})
    for row in rows:
        ticker = row["market_slug"]
        try:
            m = kio.fetch_market(ticker)
            if m is None:
                log_event("kalshi_resolution_skipped", {
                    "entry_id": row["entry_id"], "ticker": ticker,
                    "reason": "market_not_found"})
                continue
            status = str(m.get("status") or "").lower()
            result = str(m.get("result") or "").lower()
            if status not in _SETTLED_STATUSES:
                log_event("kalshi_resolution_skipped", {
                    "entry_id": row["entry_id"], "ticker": ticker,
                    "reason": "not_yet_settled", "status": status})
                continue
            if result == "yes":
                final_outcome = "YES"
            elif result == "no":
                final_outcome = "NO"
            else:
                # settled sem result válido — convenção VOID (payout neutro
                # no preço de entrada, como no bot Polymarket).
                final_outcome = "VOID"
            payout = 1.0 if final_outcome == row["side"] else 0.0
            if final_outcome == "VOID":
                payout = float(row["entry_price"] or 0)

            observed_value, observed_src = _observed_value_for_kalshi(row)
            with db.connect() as conn2:
                db.insert_resolution(
                    conn2, entry_id=row["entry_id"], ts_resolved=_now_iso(),
                    final_outcome=final_outcome, payout_per_share=payout,
                    observed_value=observed_value)
            resolved += 1
            log_event("kalshi_resolution_observed", {
                "entry_id": row["entry_id"], "ticker": ticker,
                "outcome": final_outcome, "payout": payout,
                "observed_value_f": observed_value,
                "observed_source": observed_src})

            try:
                close_result = _paper_close(
                    args, ticker, row["side"],
                    f"kalshi_settlement:{final_outcome}",
                    force_exit_price=payout, fee_rate=0.0)
                close_result = web._normalize_close_result(close_result)
                log_event("kalshi_resolution_closed", {
                    "entry_id": row["entry_id"], "ticker": ticker,
                    "payout": payout,
                    "realized_pnl": close_result.get("realized_pnl")})
            except RuntimeError as ce:
                # Já fechada via cashout antes da resolução — não é erro.
                log_event("kalshi_resolution_close_skipped", {
                    "entry_id": row["entry_id"], "reason": str(ce)})
            except Exception as ce:
                log_event("error", {"where": "kalshi_resolution_close",
                                    "entry_id": row["entry_id"],
                                    "err": str(ce)}, level="WARN")
        except Exception as e:
            log_event("error", {"where": "kalshi_resolution_sweep",
                                "ticker": ticker, "err": str(e)},
                      level="WARN")
    return resolved


def run_rejection_counterfactual_kalshi(args, limit: int = 40) -> int:
    """"Custo da cautela": para entries REJECTED/SKIPPED cujo mercado já
    encerrou, busca o settlement REAL da Kalshi (barato: 1 GET por ticker)
    e grava em counterfactuals quanto o trade recusado TERIA rendido —
    nocional padronizado de $100, líquido da fee taker.

    Motivação (operador, 2026-07-11): com muitas fontes de evidência, o
    judge pode ficar conservador demais e rejeitar trades lucrativos. Este
    sweep transforma essa hipótese em telemetria: rejeições de trades
    mortos aparecem com pnl<=0 (cautela correta); pnl>0 recorrente = lucro
    deixado na mesa, base objetiva para afrouxar a régua.

    Não altera status nem posições; counterfactuals ganha realized_pnl=0.0
    (não operamos), hypothetical_hold_pnl=pnl hipotético e notes JSON.
    `limit` por ciclo limita chamadas HTTP em backlogs grandes; o resto
    fica para o próximo sweep (idempotente via LEFT JOIN)."""
    now = _now_iso()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT e.* FROM entries e "
            "LEFT JOIN counterfactuals cf ON cf.entry_id = e.entry_id "
            "WHERE e.status IN ('REJECTED','SKIPPED') "
            "AND COALESCE(e.strategy,'') = ? "
            "AND e.end_date IS NOT NULL AND e.end_date < ? "
            "AND cf.counterfactual_id IS NULL "
            "ORDER BY e.end_date ASC LIMIT ?",
            (STRATEGY, now, limit)).fetchall()
    if not rows:
        return 0
    done, n_win, total_pnl = 0, 0, 0.0
    for row in rows:
        ticker = row["token_id_yes"]
        try:
            m = kio.fetch_market(ticker)
        except Exception:
            m = None
        if not m or (m.get("status") or "").lower() not in _SETTLED_STATUSES:
            continue  # ainda não liquidado — próximo sweep tenta de novo
        result = (m.get("result") or "").lower()
        side = (row["side"] or "YES").upper()
        entry_price = float(row["entry_price"] or 0)
        if entry_price <= 0:
            continue
        if result in ("yes", "no"):
            payout = 1.0 if result == side.lower() else 0.0
        else:
            payout = entry_price  # VOID: stake devolvido, pnl 0 antes da fee
        contracts = 100.0 / entry_price
        fee = kio.kalshi_taker_fee(entry_price, contracts)
        hypo = round(contracts * (payout - entry_price) - fee, 2)
        with db.connect() as conn:
            db.upsert_counterfactual(
                conn, row["entry_id"], realized_pnl=0.0,
                hypothetical_hold_pnl=hypo, delta=hypo, computed_at=now,
                notes=json.dumps({
                    "kind": "rejection_counterfactual", "per_usd": 100,
                    "status_at_skip": row["status"],
                    "outcome": result or "void", "payout": payout,
                    "entry_price": entry_price, "side": side,
                    "fee_usd": fee}))
        done += 1
        total_pnl += hypo
        n_win += 1 if hypo > 0 else 0
        log_event("kalshi_rejection_counterfactual", {
            "entry_id": row["entry_id"], "ticker": ticker, "side": side,
            "status_at_skip": row["status"], "outcome": result or "void",
            "hypo_pnl_per_100usd": hypo})
    if done:
        log_event("kalshi_caution_cost", {
            "n_computed": done, "n_would_have_won": n_win,
            "total_hypo_pnl_per_100usd": round(total_pnl, 2)})
    return done


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

EXECUTE_INTERVAL = 60
MONITOR_TICK = 60
RESOLUTION_SWEEP_INTERVAL = 3600
HEARTBEAT_INTERVAL = 300


def _handle_sig(signum, frame):
    global _shutdown
    _shutdown = True


def _write_pid_file():
    pid_file = Path.home() / ".polymarket-paper" / "kalshi-bot.pid.json"
    try:
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = pid_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "pid": os.getpid(),
            "argv": [sys.executable] + sys.argv,
            "cwd": str(Path.cwd()),
            "started_at": _now_iso(),
        }), encoding="utf-8")
        tmp.replace(pid_file)
    except OSError as e:
        log_event("warn", {"where": "kalshi_pidfile_write", "err": str(e)},
                  level="WARN")
    return pid_file


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Kalshi weather edge bot (paper trading, 11 cidades US)")
    ap.add_argument("--once", action="store_true",
                    help="roda cada fase uma vez e sai")
    ap.add_argument("--daemon", action="store_true", help="loop contínuo")
    ap.add_argument("--dry-run", action="store_true",
                    help="não toca o paper portfolio (marca EXECUTED no DB)")
    ap.add_argument("--portfolio", default="kalshi",
                    help="nome do paper portfolio (default: kalshi — criar "
                         "com paper_engine.py --action init --name kalshi)")
    ap.add_argument("--log-file", default=None,
                    help="JSONL de eventos (default kalshi_edge.jsonl)")
    # Discovery
    ap.add_argument("--discovery-interval-min", type=float, default=60)
    ap.add_argument("--min-edge-pp", type=float, default=20,
                    help="edge mínimo LÍQUIDO de fee no discovery (pp)")
    ap.add_argument("--min-ttr-hours", type=float, default=2)
    ap.add_argument("--window-hours", type=float, default=48,
                    help="TTR máximo (mercados Kalshi são diários)")
    ap.add_argument("--min-volume", type=float, default=100,
                    help="volume mínimo em CONTRATOS (não USD)")
    ap.add_argument("--min-entry-price", type=float, default=0.30)
    ap.add_argument("--max-entry-price", type=float, default=0.85)
    ap.add_argument("--max-markets-per-series", type=int, default=50)
    ap.add_argument("--reproposal-cooldown-hours", type=float, default=6,
                    help="não re-propõe (ticker, side) REJECTED/SKIPPED há "
                         "menos de N horas — sem isso o judge re-julga o "
                         "mesmo mercado a cada ciclo de discovery")
    # Execute
    ap.add_argument("--execute-min-edge-pp", type=float, default=8,
                    help="edge mínimo LÍQUIDO de fee na execução (pp)")
    ap.add_argument("--max-slippage", type=float, default=0.20)
    ap.add_argument("--max-market-exposure-usd", type=float, default=50)
    ap.add_argument("--max-event-exposure-usd", type=float, default=100,
                    help="cap somado sobre todas as brackets de um mesmo "
                         "evento (mesma cidade+dia)")
    ap.add_argument("--min-trade-usd", type=float, default=10,
                    help="tamanho mínimo por trade (constituição §2)")
    # Monitor
    ap.add_argument("--profit-lock-pp", type=float, default=50)
    ap.add_argument("--trailing-drawdown-pct", type=float, default=30)
    ap.add_argument("--convergence-pp", type=float, default=5)
    # Risk
    ap.add_argument("--max-drawdown-halt-pct", type=float, default=20)
    ap.add_argument("--daily-loss-limit-pct", type=float, default=5)
    # Forecast (consumidos por _compute_mae_for_market via getattr)
    ap.add_argument("--open-meteo", dest="open_meteo", action="store_true",
                    default=True)
    ap.add_argument("--no-open-meteo", dest="open_meteo",
                    action="store_false")
    ap.add_argument("--multi-source", dest="multi_source",
                    action="store_true", default=True)
    ap.add_argument("--no-multi-source", dest="multi_source",
                    action="store_false")
    ap.add_argument("--max-disagreement-pp", type=float, default=25)
    ap.add_argument("--probe-cli", nargs="+", metavar="STATION [DATE]",
                    help="smoke do operador: busca o CLI no arquivo do IEM e "
                         "compara com o METAR (ex.: --probe-cli KSEA "
                         "2026-07-09; DATE default = ontem UTC)")
    ap.add_argument("--status", action="store_true",
                    help="raio-X operacional: caminho do DB, entries por "
                         "status, posições abertas (mesma query do "
                         "dashboard) e banca paper — e sai")
    return ap


def main() -> int:
    ap = build_arg_parser()
    args = ap.parse_args()

    if args.log_file:
        web.LOG_FILE = Path(args.log_file)

    if args.probe_cli:
        from datetime import timedelta
        icao = args.probe_cli[0].upper()
        d = (date.fromisoformat(args.probe_cli[1]) if len(args.probe_cli) > 1
             else datetime.now(timezone.utc).date() - timedelta(days=1))
        cli = fetch_iem_cli(icao, d)
        print(f"IEM CLI  {icao} {d}: {cli}")
        try:
            metar = fetch_metar_daily_extremes(icao, d)
        except Exception as e:
            metar = f"erro: {e}"
        print(f"METAR    {icao} {d}: {metar}")
        if cli:
            print("OK — verdade-terra do observado virá do CLI (fonte de "
                  "settlement); METAR fica como fallback.")
        else:
            print("IEM indisponível/sem dado — o bot cai no METAR "
                  "(aproximação documentada). Se persistir, cheque o "
                  "endpoint no navegador: "
                  f"{IEM_CLI_API}?station={icao}&date={d}")
        return 0

    if args.status:
        # Diagnóstico do descasamento clássico: "aberta" pode significar
        # coisas diferentes em cada camada. O dashboard lista entries do
        # kalshi_edge.db (EXECUTED sem cashout/resolução); a banca paper
        # vive no portfolio.db. Este comando mostra os DOIS lados com as
        # MESMAS queries que cada um usa.
        db_path = Path(str(db.DB_PATH))
        print(f"DB do bot: {db_path}  (existe: {db_path.exists()})")
        db.init_db()
        with db.connect() as conn:
            by_status = conn.execute(
                "SELECT COALESCE(status, '?'), COUNT(*) FROM entries "
                "GROUP BY status ORDER BY 2 DESC").fetchall()
            print("entries por status:",
                  {r[0]: r[1] for r in by_status} or "(nenhuma)")
            open_rows = db.query_open_positions(conn)
            print(f"abertas segundo bot/dashboard (EXECUTED sem "
                  f"cashout/resolução): {len(open_rows)}")
            for r in open_rows:
                print(f"  #{r['entry_id']} {r['market_slug']} {r['side']} "
                      f"{(r['size_shares'] or 0):.0f}x @ "
                      f"{r['entry_price']}")
        try:
            pf = _make_engine(args.portfolio).get_portfolio(
                refresh_prices=False)
            poss = pf.get("positions") or []
            print(f"banca paper {args.portfolio!r}: "
                  f"cash ${pf.get('cash_balance', 0):.2f}, "
                  f"{len(poss)} posição(ões) aberta(s)")
            for p in poss:
                print(f"  {p['token_id']} {p['side']} "
                      f"{(p['shares'] or 0):.0f}x @ {p['avg_entry']}")
        except Exception as e:
            print(f"banca paper {args.portfolio!r} indisponível: {e}")
        return 0

    kcities = kio.load_kalshi_cities()
    if not kcities.get("cities"):
        print("kalshi-cities.json ausente ou vazio — nada a fazer",
              file=sys.stderr)
        return 1

    db.init_db()
    log_event("kalshi_bot_startup", {
        "db_path": str(db.DB_PATH),
        "portfolio": args.portfolio,
        "n_cities": len(kcities["cities"]),
        "dry_run": args.dry_run,
        "min_edge_pp_net": args.min_edge_pp,
    })

    # Fail-fast com mensagem clara se o portfolio kalshi não existe ainda.
    if not args.dry_run:
        try:
            engine = _make_engine(args.portfolio)
            engine.get_portfolio(refresh_prices=False)
        except Exception as e:
            log_event("error", {
                "where": "kalshi_startup",
                "err": f"portfolio {args.portfolio!r} indisponível: {e}",
                "fix": ("crie com: python polymarket-paper-trader/scripts/"
                        f"paper_engine.py --action init --name "
                        f"{args.portfolio} --balance 1000")}, level="ERROR")
            return 1

    signal.signal(signal.SIGTERM, _handle_sig)
    signal.signal(signal.SIGINT, _handle_sig)

    if args.once:
        run_discovery_kalshi(args, kcities)
        run_execute_kalshi(args)
        run_monitor_tick_kalshi(args, kcities)
        run_resolution_sweep_kalshi(args)
        run_rejection_counterfactual_kalshi(args)
        return 0

    if not args.daemon:
        ap.print_help()
        return 0

    pid_file = _write_pid_file()
    # -inf, não 0.0: monotonic() conta desde o boot, e 0.0 atrasaria o
    # primeiro ciclo numa máquina recém-reiniciada (uptime < intervalo).
    last_discovery = float("-inf")
    last_resolution = float("-inf")
    last_heartbeat = float("-inf")
    try:
        while not _shutdown:
            now = time.monotonic()
            try:
                if now - last_discovery >= args.discovery_interval_min * 60:
                    last_discovery = now
                    run_discovery_kalshi(args, kcities)
                run_execute_kalshi(args)
                run_monitor_tick_kalshi(args, kcities)
                if now - last_resolution >= RESOLUTION_SWEEP_INTERVAL:
                    last_resolution = now
                    run_resolution_sweep_kalshi(args)
                    run_rejection_counterfactual_kalshi(args)
                if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                    last_heartbeat = now
                    log_event("kalshi_heartbeat", {})
            except Exception as e:
                log_event("error", {"where": "kalshi_main_loop",
                                    "err": str(e)}, level="ERROR")
            for _ in range(MONITOR_TICK):
                if _shutdown:
                    break
                time.sleep(1)
    finally:
        pid_file.unlink(missing_ok=True)
        log_event("kalshi_bot_shutdown", {})
    return 0


# ---------------------------------------------------------------------------
# Testes herméticos (sem rede) — monkeypatch de kio/web/paper hooks
# ---------------------------------------------------------------------------

def _mk_test_args(**overrides):
    args = build_arg_parser().parse_args([])
    for k, v in overrides.items():
        setattr(args, k, v)
    return args


def _swap_test_db():
    """Aponta db.connect para um DB temporário (o connect real usa default
    bound no def; monkeypatch da função cobre todos os call sites)."""
    import tempfile
    tmp = Path(tempfile.mkdtemp()) / "kalshi_test.db"
    orig_connect = db.connect

    def _tmp_connect(path=None):
        return orig_connect(tmp)
    db.connect = _tmp_connect

    def restore():
        db.connect = orig_connect
    return tmp, restore


_TEST_KCITIES = {"cities": {
    "New York": {
        "series_high": "KXHIGHNY", "series_low": "KXLOWTNYC",
        "station": "KNYC", "wfo": "OKX", "lat": 40.7789, "lon": -73.9692,
        "timezone": "America/New_York", "pilot": True, "om_models": None,
        "risk_notes": "brisa marítima; UHI", "confirmation": "NHIGH.pdf",
    },
}}


def _mk_market(ticker="KXHIGHNY-26JUL12-T87", *, yes_ask="0.4000",
               yes_bid="0.3500", no_ask="0.6500", no_bid="0.6000",
               volume="500.00", strike_type="greater", floor_strike="87.5",
               close_time="2026-07-12T14:00:00Z"):
    return {"ticker": ticker, "event_ticker": "KXHIGHNY-26JUL12",
            "title": "Highest temperature in NYC today?",
            "subtitle": "88° or above", "status": "active",
            "strike_type": strike_type, "floor_strike": floor_strike,
            "close_time": close_time, "volume_fp": volume,
            "yes_ask_dollars": yes_ask, "yes_bid_dollars": yes_bid,
            "no_ask_dollars": no_ask, "no_bid_dollars": no_bid}


def _test_lst_date():
    """target_date vem da data LOCAL do event_ticker, não do close_time UTC
    (que pode cair no dia seguinte em UTC)."""
    m = _mk_market(close_time="2026-07-13T03:59:00Z")  # madrugada UTC do dia 13
    spec = kio.build_market_spec(m, "New York", "high")
    assert spec is not None
    assert spec.target_date == date(2026, 7, 12), spec.target_date
    assert spec.temp_kind == "high" and spec.threshold_unit == "F"
    print("Test PASS: target_date = dia local do event_ticker (2026-07-12), "
          "não o dia UTC do close_time (13)")
    print("\nAll --test-lst-date PASS")


def _test_discovery():
    """Discovery: spec estruturado → entry PROPOSED com meta venue/pilot/
    spec; gate de edge LÍQUIDO de fee mata edge bruto que sobreviveria."""
    import weather_edge_bot as _web
    tmp, restore_db = _swap_test_db()
    saved = (kio.fetch_open_markets, kio.discover_weather_series,
             _web.fetch_forecast, _web._compute_mae_for_market)
    g = globals()
    saved_fp = g["forecast_probability"]
    try:
        kio.discover_weather_series = lambda *a, **k: []
        kio.fetch_open_markets = lambda tk, limit=50: (
            [_mk_market()] if tk == "KXHIGHNY" else [])
        _web.fetch_forecast = lambda city, days=5, lat=None, lon=None: {
            "city": city, "fake": True}
        _web._compute_mae_for_market = lambda spec, fc, args, station=None: (
            None, None, None, {"ensemble_calibrated": True})
        g["forecast_probability"] = lambda *a, **k: 0.80

        args = _mk_test_args(min_edge_pp=20, min_ttr_hours=0.1)
        # freeze "now": mercado fecha 2026-07-12; se o teste rodar depois
        # disso o TTR real seria 0. Usa um close_time futuro dinâmico.
        future = (datetime.now(timezone.utc)
                  .replace(microsecond=0)).timestamp() + 6 * 3600
        close_iso = datetime.fromtimestamp(
            future, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        kio.fetch_open_markets = lambda tk, limit=50: (
            [_mk_market(close_time=close_iso)] if tk == "KXHIGHNY" else [])

        n = run_discovery_kalshi(args, _TEST_KCITIES)
        assert n == 1, n
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM entries").fetchone()
        assert row["strategy"] == "kalshi_weather", row["strategy"]
        assert row["side"] == "YES", row["side"]
        assert row["market_slug"] == "KXHIGHNY-26JUL12-T87"
        assert row["token_id_yes"] == row["token_id_no"] == row["market_slug"]
        assert row["ladder_event_slug"] == "KXHIGHNY-26JUL12"
        meta = json.loads(row["discovery_meta_json"])
        assert meta["venue"] == "kalshi" and meta["pilot"] is True, meta
        assert meta["station_icao"] == "KNYC"
        assert meta["spec"]["comparison"] == "exceed"
        assert meta["spec"]["target_date"] == "2026-07-12"
        # edge: P(YES)=0.80 vs ask 0.40 → 40pp bruto; fee_pp(0.40)=1.68 →
        # net 38.32 armazenado
        assert abs(row["edge_pp_at_entry"] - 38.32) < 0.01, row["edge_pp_at_entry"]
        assert abs(meta["fee_pp"] - 1.68) < 0.01, meta["fee_pp"]
        print("Test 1 PASS: entry PROPOSED com strategy/meta/spec/edge "
              "líquido (38.32pp) corretos")

        # Gate de fee: P=0.50 vs ask 0.42 → bruto 8pp; fee_pp(0.42)=1.7 →
        # net 6.3 < min_edge_pp 7 → NÃO propõe (sem fee teria passado).
        with db.connect() as conn:
            _wipe_entries(conn)
        g["forecast_probability"] = lambda *a, **k: 0.50
        kio.fetch_open_markets = lambda tk, limit=50: (
            [_mk_market(yes_ask="0.4200", close_time=close_iso)]
            if tk == "KXHIGHNY" else [])
        args2 = _mk_test_args(min_edge_pp=7, min_ttr_hours=0.1)
        n2 = run_discovery_kalshi(args2, _TEST_KCITIES)
        assert n2 == 0, n2
        with db.connect() as conn:
            skips = conn.execute(
                "SELECT reason FROM discovery_skips").fetchall()
        assert any(s["reason"] == "low_edge_after_fee" for s in skips), skips
        print("Test 2 PASS: edge bruto 8pp ≥ floor 7pp, mas líquido de fee "
              "6.3pp < 7pp → skip low_edge_after_fee registrado")

        # Dedup: entry pendente na mesma (ticker, side) não duplica.
        g["forecast_probability"] = lambda *a, **k: 0.80
        kio.fetch_open_markets = lambda tk, limit=50: (
            [_mk_market(close_time=close_iso)] if tk == "KXHIGHNY" else [])
        n3 = run_discovery_kalshi(args, _TEST_KCITIES)
        assert n3 == 1, n3
        n4 = run_discovery_kalshi(args, _TEST_KCITIES)
        assert n4 == 0, n4
        print("Test 3 PASS: dedup (ticker, side) pendente")

        # Cooldown de re-proposta: REJECTED recente NÃO volta; REJECTED
        # antigo (fora da janela) volta a ser proposto.
        with db.connect() as conn:
            conn.execute(
                "UPDATE entries SET status = 'REJECTED'")
        n5 = run_discovery_kalshi(args, _TEST_KCITIES)
        assert n5 == 0, n5
        old_ts = (datetime.now(timezone.utc) -
                  timedelta(hours=7)).isoformat()
        with db.connect() as conn:
            conn.execute("UPDATE entries SET ts = ?", (old_ts,))
        n6 = run_discovery_kalshi(args, _TEST_KCITIES)
        assert n6 == 1, n6
        print("Test 4 PASS: cooldown de re-proposta — REJECTED recente "
              "bloqueia, REJECTED de 7h atrás (janela 6h) libera")

        print("\nAll --test-discovery PASS (4/4)")
    finally:
        (kio.fetch_open_markets, kio.discover_weather_series,
         _web.fetch_forecast, _web._compute_mae_for_market) = saved
        g["forecast_probability"] = saved_fp
        restore_db()


def _wipe_entries(conn):
    """Limpa entries e todas as tabelas-filhas (ordem respeita as FKs)."""
    for t in ("monitor_checks", "cashouts", "resolutions",
              "counterfactuals", "judge_reviews", "entries"):
        conn.execute(f"DELETE FROM {t}")


def _seed_entry(conn, *, status="APPROVED", side="YES", entry_price=0.40,
                ticker="KXHIGHNY-26JUL12-T87", p_side=0.80,
                end_date=None, size_shares=None, size_usd=None,
                peak_bid=None):
    end_date = end_date or (datetime.now(timezone.utc)
                            .isoformat().replace("+00:00", "Z"))
    meta = {"venue": "kalshi", "pilot": True, "station_icao": "KNYC",
            "lat": 40.7789, "lon": -73.9692,
            "event_ticker": "KXHIGHNY-26JUL12",
            "spec": {"city": "New York", "threshold_value": 87.5,
                     "threshold_unit": "F", "metric": "temp",
                     "comparison": "exceed", "target_date": "2026-07-12",
                     "confidence": 0.95, "raw_question": "q",
                     "threshold_value_high": None, "temp_kind": "high"}}
    return db.insert_entry(
        conn, ts=_now_iso(), market_slug=ticker, market_question="q",
        condition_id="KXHIGHNY-26JUL12", token_id_yes=ticker,
        token_id_no=ticker, end_date=end_date, side=side,
        entry_price=entry_price, forecast_prob_at_entry=p_side,
        implied_prob_at_entry=entry_price, edge_pp_at_entry=30.0,
        parser_confidence=0.95, city_resolved="New York",
        threshold_value=87.5, threshold_unit="F", comparison="exceed",
        ttr_hours_at_entry=6.0, status=status, strategy=STRATEGY,
        ladder_event_slug="KXHIGHNY-26JUL12",
        discovery_meta_json=meta, size_shares=size_shares,
        size_usd=size_usd, peak_bid_seen=peak_bid)


def _test_execute():
    """Execute: contratos inteiros (floor, skip <1), price/fee propagados
    ao engine, cap do judge honrado."""
    g = globals()
    tmp, restore_db = _swap_test_db()
    saved_fetch_ob = kio.fetch_orderbook
    saved_risk = web._risk_block_reason
    saved_engine = g["_make_engine"]
    calls = []

    class FakeEngine:
        def open_position(self, **kw):
            calls.append(kw)
            return {"status": "executed", "cost_usd": kw["size_usd"],
                    "shares_filled": kw["size_usd"] / kw["price"],
                    "avg_price": kw["price"], "fee": 0.0}
        def get_portfolio(self, refresh_prices=True):
            return {"total_value": 1000.0, "drawdown_pct": 0.0,
                    "starting_balance": 1000.0}

    try:
        web._risk_block_reason = lambda engine, args: None
        g["_make_engine"] = lambda portfolio: FakeEngine()
        future = (datetime.now(timezone.utc).timestamp()) + 6 * 3600
        end_iso = datetime.fromtimestamp(
            future, tz=timezone.utc).isoformat().replace("+00:00", "Z")

        # Book: 30 contratos @0.40 no ask (derivado de bid NO 0.60).
        kio.fetch_orderbook = lambda tk: {
            "orderbook": {"yes": [[0.35, 100]], "no": [[0.60, 30]]}}

        with db.connect() as conn:
            _seed_entry(conn, status="APPROVED", end_date=end_iso)
        args = _mk_test_args(min_trade_usd=1.0, execute_min_edge_pp=8)
        n = run_execute_kalshi(args)
        assert n == 1, n
        assert len(calls) == 1, calls
        kw = calls[0]
        assert kw["token_id"] == "KXHIGHNY-26JUL12-T87"
        assert kw["price"] == 0.40, kw
        assert abs(kw["fee_rate"] - kio.kalshi_fee_rate(0.40)) < 1e-9, kw
        assert kw["market_question"] == "q"
        # depth 30 @0.40 → cap exposição market $50 → 125 contratos > depth
        # → 30 contratos inteiros → $12.00
        assert abs(kw["size_usd"] - 12.0) < 1e-6, kw["size_usd"]
        with db.connect() as conn:
            row = conn.execute("SELECT status, size_shares FROM entries"
                               ).fetchone()
        assert row["status"] == "EXECUTED", row["status"]
        print("Test 1 PASS: 30 contratos inteiros @0.40 → $12; price/fee/"
              "question propagados; EXECUTED")

        # <1 contrato: depth 0.6 → skip below_one_contract.
        calls.clear()
        with db.connect() as conn:
            _wipe_entries(conn)
            _seed_entry(conn, status="APPROVED", end_date=end_iso)
        kio.fetch_orderbook = lambda tk: {
            "orderbook": {"yes": [[0.35, 100]], "no": [[0.60, 0.6]]}}
        n2 = run_execute_kalshi(args)
        assert n2 == 0 and not calls, (n2, calls)
        print("Test 2 PASS: depth 0.6 contrato → skip below_one_contract")

        # Judge ADJUSTED cap: depth 30 mas judge capa em $4 → floor(10)=10
        # contratos → $4.00.
        calls.clear()
        with db.connect() as conn:
            _wipe_entries(conn)
            eid = _seed_entry(conn, status="ADJUSTED", end_date=end_iso)
            conn.execute(
                "INSERT INTO judge_reviews (entry_id, ts, verdict, "
                "confidence, adjusted_size_usd) VALUES (?, ?, 'ADJUST', "
                "0.6, 4.0)", (eid, _now_iso()))
        kio.fetch_orderbook = lambda tk: {
            "orderbook": {"yes": [[0.35, 100]], "no": [[0.60, 30]]}}
        n3 = run_execute_kalshi(args)
        assert n3 == 1 and len(calls) == 1, (n3, calls)
        assert abs(calls[0]["size_usd"] - 4.0) < 1e-6, calls[0]["size_usd"]
        print("Test 3 PASS: cap do judge ($4) → 10 contratos → $4.00")

        # Banda de preço na EXECUÇÃO: mercado desabou até fill 0.01 (caso
        # real: entries 62/71 — seleção adversa no dia da liquidação).
        # Edge re-check "melhora" (P 0.80 vs 0.01) mas a banda barra ANTES,
        # e o skip é permanente (SKIPPED).
        calls.clear()
        with db.connect() as conn:
            _wipe_entries(conn)
            _seed_entry(conn, status="APPROVED", end_date=end_iso)
        kio.fetch_orderbook = lambda tk: {
            "orderbook": {"yes": [[0.005, 2000]], "no": [[0.99, 2000]]}}
        n4 = run_execute_kalshi(args)
        assert n4 == 0 and not calls, (n4, calls)
        with db.connect() as conn:
            row = conn.execute(
                "SELECT status, skip_reason FROM entries").fetchone()
        assert row["status"] == "SKIPPED", row["status"]
        assert row["skip_reason"] == "price_out_of_band", row["skip_reason"]
        print("Test 4 PASS: fill 0.01 fora da banda [0.30, 0.85] → SKIPPED "
              "permanente (sem seleção adversa)")

        # size_below_min vira SKIPPED (não retry infinito): depth 30 @0.40 →
        # $12 < min $13 → skip; segunda rodada não re-tenta.
        calls.clear()
        with db.connect() as conn:
            _wipe_entries(conn)
            _seed_entry(conn, status="APPROVED", end_date=end_iso)
        kio.fetch_orderbook = lambda tk: {
            "orderbook": {"yes": [[0.35, 100]], "no": [[0.60, 30]]}}
        args5 = _mk_test_args(min_trade_usd=13.0, execute_min_edge_pp=8)
        n5 = run_execute_kalshi(args5)
        assert n5 == 0 and not calls, (n5, calls)
        with db.connect() as conn:
            row = conn.execute(
                "SELECT status, skip_reason FROM entries").fetchone()
        assert row["status"] == "SKIPPED", row["status"]
        assert row["skip_reason"] == "size_below_min", row["skip_reason"]
        n5b = run_execute_kalshi(args5)
        assert n5b == 0 and not calls, "SKIPPED não pode ser re-tentado"
        print("Test 5 PASS: size $12 < min $13 → SKIPPED permanente, sem "
              "loop de retry")

        print("\nAll --test-execute PASS (5/5)")
    finally:
        kio.fetch_orderbook = saved_fetch_ob
        web._risk_block_reason = saved_risk
        g["_make_engine"] = saved_engine
        restore_db()


def _test_monitor():
    """Monitor: trigger profit_lock com book Kalshi; cashout SEMPRE via
    force_exit_price; set_position_price chamado; phantom self-heal."""
    import weather_edge_bot as _web
    g = globals()
    tmp, restore_db = _swap_test_db()
    saved = (kio.fetch_orderbook, _web.fetch_forecast,
             _web._compute_mae_for_market)
    saved_fp = g["forecast_probability"]
    saved_close = g["_paper_close"]
    saved_setp = g["_set_position_price"]
    closes, price_sets = [], []
    try:
        _web.fetch_forecast = lambda city, days=5, lat=None, lon=None: {
            "fake": True}
        _web._compute_mae_for_market = lambda spec, fc, args, station=None: (
            None, None, None, {"ensemble_calibrated": True})
        g["forecast_probability"] = lambda *a, **k: 0.97
        # bid 0.95 ≥ entry 0.40 + 0.50 → profit_lock. Book fundo p/ sell.
        kio.fetch_orderbook = lambda tk: {
            "orderbook": {"yes": [[0.95, 100]], "no": [[0.03, 100]]}}

        def fake_close(args, token_id, side, reasoning,
                       force_exit_price, fee_rate=0.0):
            closes.append({"token_id": token_id, "side": side,
                           "force_exit_price": force_exit_price,
                           "fee_rate": fee_rate})
            return {"status": "closed", "avg_sell_price": force_exit_price,
                    "shares_sold": 30.0, "realized_pnl": 16.0}
        g["_paper_close"] = fake_close
        g["_set_position_price"] = lambda args, tk, price, side=None: \
            price_sets.append((tk, price, side)) or 1

        future = (datetime.now(timezone.utc).timestamp()) + 6 * 3600
        end_iso = datetime.fromtimestamp(
            future, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        with db.connect() as conn:
            _seed_entry(conn, status="EXECUTED", end_date=end_iso,
                        size_shares=30.0, size_usd=12.0)
        args = _mk_test_args()
        _last_monitor_per_entry.clear()
        run_monitor_tick_kalshi(args, _TEST_KCITIES)

        assert len(closes) == 1, closes
        c = closes[0]
        assert c["force_exit_price"] == 0.95, c
        assert abs(c["fee_rate"] - kio.kalshi_fee_rate(0.95)) < 1e-9, c
        assert price_sets and price_sets[0][1] == 0.95, price_sets
        with db.connect() as conn:
            n_cash = conn.execute(
                "SELECT COUNT(*) FROM cashouts").fetchone()[0]
            mc = conn.execute(
                "SELECT decision FROM monitor_checks").fetchone()
        assert n_cash == 1, n_cash
        assert mc["decision"] == "CASHOUT", mc["decision"]
        print("Test 1 PASS: profit_lock → cashout via force_exit_price 0.95 "
              "+ fee_rate kalshi; set_position_price(0.95); cashout gravado")

        # Phantom: close levanta NoOpenPositionError → marcador terminal.
        from paper_engine import NoOpenPositionError

        def phantom_close(args, token_id, side, reasoning,
                          force_exit_price, fee_rate=0.0):
            raise NoOpenPositionError("no open position")
        g["_paper_close"] = phantom_close
        with db.connect() as conn:
            _wipe_entries(conn)
            conn.execute("DELETE FROM cashouts")
            conn.execute("DELETE FROM monitor_checks")
            _seed_entry(conn, status="EXECUTED", end_date=end_iso,
                        size_shares=30.0, size_usd=12.0)
        _last_monitor_per_entry.clear()
        run_monitor_tick_kalshi(args, _TEST_KCITIES)
        with db.connect() as conn:
            row = conn.execute(
                "SELECT reason FROM cashouts").fetchone()
        assert row is not None and "phantom" in (row["reason"] or "").lower(), row
        print("Test 2 PASS: NoOpenPositionError → phantom cashout terminal")

        # Liquidez de saída insuficiente: bids rasos → segura (sem close).
        g["_paper_close"] = fake_close
        closes.clear()
        kio.fetch_orderbook = lambda tk: {
            "orderbook": {"yes": [[0.95, 5]], "no": [[0.03, 100]]}}
        with db.connect() as conn:
            _wipe_entries(conn)
            conn.execute("DELETE FROM cashouts")
            _seed_entry(conn, status="EXECUTED", end_date=end_iso,
                        size_shares=30.0, size_usd=12.0)
        _last_monitor_per_entry.clear()
        run_monitor_tick_kalshi(args, _TEST_KCITIES)
        assert not closes, closes
        print("Test 3 PASS: bid depth 5 < held 30 → cashout bloqueado "
              "(não despeja em book raso)")

        print("\nAll --test-monitor PASS (3/3)")
    finally:
        (kio.fetch_orderbook, _web.fetch_forecast,
         _web._compute_mae_for_market) = saved
        g["forecast_probability"] = saved_fp
        g["_paper_close"] = saved_close
        g["_set_position_price"] = saved_setp
        restore_db()


def _test_resolution():
    """Resolution: decide SÓ por status+result da Kalshi; yes/no/void/
    not-settled; close com portfolio kalshi e payout; METAR só log."""
    g = globals()
    tmp, restore_db = _swap_test_db()
    saved_fetch_m = kio.fetch_market
    saved_close = g["_paper_close"]
    saved_metar = g["fetch_metar_daily_extremes"]
    saved_cli = g["fetch_iem_cli"]
    closes = []
    try:
        def fake_close(args, token_id, side, reasoning,
                       force_exit_price, fee_rate=0.0):
            closes.append({"token_id": token_id, "side": side,
                           "portfolio": args.portfolio,
                           "force_exit_price": force_exit_price,
                           "reasoning": reasoning})
            return {"status": "closed", "realized_pnl": 1.0}
        g["_paper_close"] = fake_close
        g["fetch_metar_daily_extremes"] = lambda icao, tgt, hours=72: {
            "observed_max_f": 91.0, "observed_min_f": 72.0}
        # IEM indisponível nos testes 1-4 → fallback METAR (91.0)
        g["fetch_iem_cli"] = lambda icao, d: None

        past = (datetime.now(timezone.utc).timestamp()) - 3600
        past_iso = datetime.fromtimestamp(
            past, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        args = _mk_test_args()

        # YES vencedor (side YES → payout 1.0)
        with db.connect() as conn:
            _seed_entry(conn, status="EXECUTED", end_date=past_iso,
                        ticker="KXHIGHNY-26JUL12-T87")
        kio.fetch_market = lambda tk: {"ticker": tk, "status": "settled",
                                       "result": "yes"}
        n = run_resolution_sweep_kalshi(args)
        assert n == 1, n
        with db.connect() as conn:
            r = conn.execute("SELECT * FROM resolutions").fetchone()
        assert r["final_outcome"] == "YES" and r["payout_per_share"] == 1.0, dict(r)
        assert r["observed_value"] == 91.0, r["observed_value"]
        assert closes[0]["portfolio"] == "kalshi", closes[0]
        assert closes[0]["force_exit_price"] == 1.0, closes[0]
        print("Test 1 PASS: settled result=yes → YES payout 1.0, close no "
              "portfolio kalshi, METAR 91.0F logado")

        # NO vencedor com side YES → payout 0.0
        closes.clear()
        with db.connect() as conn:
            _wipe_entries(conn)
            conn.execute("DELETE FROM resolutions")
            _seed_entry(conn, status="EXECUTED", end_date=past_iso)
        kio.fetch_market = lambda tk: {"ticker": tk, "status": "finalized",
                                       "result": "no"}
        n2 = run_resolution_sweep_kalshi(args)
        assert n2 == 1
        with db.connect() as conn:
            r = conn.execute("SELECT * FROM resolutions").fetchone()
        assert r["final_outcome"] == "NO" and r["payout_per_share"] == 0.0
        assert closes[0]["force_exit_price"] == 0.0, closes[0]
        print("Test 2 PASS: finalized result=no vs side YES → payout 0.0")

        # Não settled (atraso do CLI) → NÃO resolve, fica pro próximo sweep.
        closes.clear()
        with db.connect() as conn:
            _wipe_entries(conn)
            conn.execute("DELETE FROM resolutions")
            _seed_entry(conn, status="EXECUTED", end_date=past_iso)
        kio.fetch_market = lambda tk: {"ticker": tk, "status": "active",
                                       "result": "", "yes_bid_dollars":
                                       "0.9900"}
        n3 = run_resolution_sweep_kalshi(args)
        assert n3 == 0 and not closes, (n3, closes)
        with db.connect() as conn:
            n_res = conn.execute(
                "SELECT COUNT(*) FROM resolutions").fetchone()[0]
        assert n_res == 0, n_res
        print("Test 3 PASS: não-settled (preço 0.99 ignorado) → retry no "
              "próximo sweep, nunca decide por preço")

        # Settled sem result válido → VOID, payout = entry_price.
        with db.connect() as conn:
            _wipe_entries(conn)
            _seed_entry(conn, status="EXECUTED", end_date=past_iso,
                        entry_price=0.40)
        kio.fetch_market = lambda tk: {"ticker": tk, "status": "settled",
                                       "result": ""}
        n4 = run_resolution_sweep_kalshi(args)
        assert n4 == 1
        with db.connect() as conn:
            r = conn.execute("SELECT * FROM resolutions").fetchone()
        assert r["final_outcome"] == "VOID", r["final_outcome"]
        assert abs(r["payout_per_share"] - 0.40) < 1e-9, dict(r)
        print("Test 4 PASS: settled sem result → VOID payout=entry_price")

        # Test 5: IEM CLI tem PRECEDÊNCIA sobre METAR no observed_value
        # (CLI 90.0 vs METAR 91.0 → grava 90.0), e o parser aceita os dois
        # shapes do IEM + campos "M" ausentes.
        with db.connect() as conn:
            _wipe_entries(conn)
            _seed_entry(conn, status="EXECUTED", end_date=past_iso,
                        ticker="KXHIGHNY-26JUL12-T87")
        g["fetch_iem_cli"] = lambda icao, d: {
            "high_f": 90.0, "low_f": 71.0, "source": "iem_cli_api"}
        kio.fetch_market = lambda tk: {"ticker": tk, "status": "settled",
                                       "result": "yes"}
        n5 = run_resolution_sweep_kalshi(args)
        assert n5 == 1, n5
        with db.connect() as conn:
            r5 = conn.execute("SELECT observed_value FROM resolutions "
                              "ORDER BY resolution_id DESC LIMIT 1").fetchone()
        assert r5["observed_value"] == 90.0, r5["observed_value"]
        # parser hermético do fetch_iem_cli (os dois shapes + "M")
        g["fetch_iem_cli"] = saved_cli  # restaura a função real
        saved_get = requests.get

        class _R:
            def __init__(self, status, payload):
                self.status_code = status
                self._p = payload
            def json(self):
                return self._p

        try:
            requests.get = lambda url, params=None, timeout=None: _R(
                200, {"data": [{"valid": "2026-07-09", "station": "KSEA",
                                "high": 78, "low": "M"}]})
            got = fetch_iem_cli("KSEA", date(2026, 7, 9))
            assert got == {"high_f": 78.0, "low_f": None,
                           "source": "iem_cli_api"}, got
            # api/1 falha → cai no legado (shape "results")
            def _legacy_only(url, params=None, timeout=None):
                if "api/1" in url:
                    return _R(500, {})
                return _R(200, {"results": [
                    {"valid": "2026-07-08", "high": 70, "low": 50},
                    {"valid": "2026-07-09", "high": 79, "low": 55}]})
            requests.get = _legacy_only
            got2 = fetch_iem_cli("KSEA", date(2026, 7, 9))
            assert got2 == {"high_f": 79.0, "low_f": 55.0,
                            "source": "iem_cli_legacy"}, got2
            # tudo falha → None (fail-open)
            def _boom(url, params=None, timeout=None):
                raise RuntimeError("net down")
            requests.get = _boom
            assert fetch_iem_cli("KSEA", date(2026, 7, 9)) is None
        finally:
            requests.get = saved_get
        print("Test 5 PASS: CLI do IEM precede METAR (90.0 gravado); parser "
              "aceita data/results, 'M'→None, fail-open")

        print("\nAll --test-resolution PASS (5/5)")
    finally:
        kio.fetch_market = saved_fetch_m
        g["_paper_close"] = saved_close
        g["fetch_metar_daily_extremes"] = saved_metar
        g["fetch_iem_cli"] = saved_cli
        restore_db()


def _test_caution_cost():
    """Custo da cautela: counterfactual de REJECTED/SKIPPED — pnl hipotético
    por $100 líquido de fee, idempotência, not-settled adiado, VOID=0−fee,
    e entries EXECUTED intocadas."""
    tmp, restore_db = _swap_test_db()
    saved_fetch_m = kio.fetch_market
    try:
        past = (datetime.now(timezone.utc).timestamp()) - 3600
        past_iso = datetime.fromtimestamp(
            past, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        args = _mk_test_args()

        # Rejeitado que TERIA vencido: YES @0.40 settled yes →
        # 250 contratos × 0.60 − fee(ceil(0.07·250·0.4·0.6·100)/100=4.20)
        # = 150 − 4.20 = 145.80 por $100.
        with db.connect() as conn:
            e1 = _seed_entry(conn, status="REJECTED", end_date=past_iso,
                             ticker="KXHIGHNY-26JUL12-T87")
        kio.fetch_market = lambda tk: {"ticker": tk, "status": "settled",
                                       "result": "yes"}
        n = run_rejection_counterfactual_kalshi(args)
        assert n == 1, n
        with db.connect() as conn:
            cf = conn.execute("SELECT * FROM counterfactuals").fetchone()
        assert cf["realized_pnl"] == 0.0, dict(cf)
        assert abs(cf["hypothetical_hold_pnl"] - 145.80) < 0.01, \
            cf["hypothetical_hold_pnl"]
        meta = json.loads(cf["notes"])
        assert meta["kind"] == "rejection_counterfactual", meta
        assert meta["outcome"] == "yes" and meta["per_usd"] == 100, meta
        print("Test 1 PASS: REJECTED que teria vencido → +$145.80/$100 "
              "(líquido de fee) gravado em counterfactuals")

        # Idempotente: segundo sweep não recomputa.
        assert run_rejection_counterfactual_kalshi(args) == 0
        print("Test 2 PASS: idempotente (LEFT JOIN counterfactuals)")

        # SKIPPED que teria perdido (settled no vs side YES) → −100 − fee?
        # payout 0: 250×(0−0.40) = −100; fee ainda paga → −104.20.
        with db.connect() as conn:
            _seed_entry(conn, status="SKIPPED", end_date=past_iso,
                        ticker="KXHIGHCHI-26JUL12-T80")
        kio.fetch_market = lambda tk: {"ticker": tk, "status": "finalized",
                                       "result": "no"}
        assert run_rejection_counterfactual_kalshi(args) == 1
        with db.connect() as conn:
            cf2 = conn.execute(
                "SELECT cf.* FROM counterfactuals cf JOIN entries e "
                "ON e.entry_id = cf.entry_id WHERE e.status='SKIPPED'"
            ).fetchone()
        assert abs(cf2["hypothetical_hold_pnl"] - (-104.20)) < 0.01, \
            cf2["hypothetical_hold_pnl"]
        print("Test 3 PASS: SKIPPED que teria perdido → −$104.20/$100 "
              "(cautela correta aparece como pnl negativo)")

        # Não-settled: adia (não grava), tenta no próximo sweep.
        with db.connect() as conn:
            _seed_entry(conn, status="REJECTED", end_date=past_iso,
                        ticker="KXHIGHMIA-26JUL12-T95")
        kio.fetch_market = lambda tk: {"ticker": tk, "status": "active",
                                       "result": ""}
        assert run_rejection_counterfactual_kalshi(args) == 0
        # VOID (settled sem result): payout=entry → pnl = −fee só.
        kio.fetch_market = lambda tk: {"ticker": tk, "status": "settled",
                                       "result": ""}
        assert run_rejection_counterfactual_kalshi(args) == 1
        with db.connect() as conn:
            n_cf = conn.execute(
                "SELECT COUNT(*) FROM counterfactuals").fetchone()[0]
            ex = conn.execute(
                "SELECT COUNT(*) FROM entries WHERE status IN "
                "('EXECUTED','FAST_PATH')").fetchone()[0]
        assert n_cf == 3, n_cf
        assert ex == 0  # nenhuma entry executada foi criada/tocada
        print("Test 4 PASS: not-settled adiado; VOID = só a fee; entries "
              "executadas fora do escopo")

        print("\nAll --test-caution-cost PASS (4/4)")
    finally:
        kio.fetch_market = saved_fetch_m
        restore_db()


def _test_logging():
    """Terminal ↔ JSONL: mkdir do --log-file em subdiretório novo +
    excepthook gravando crash no arquivo. Hermético (tmpdir, sem rede).
    Run: python kalshi_edge_bot.py --test-logging
    """
    import tempfile
    saved_logfile = web.LOG_FILE
    try:
        with tempfile.TemporaryDirectory() as td:
            # Test 1: log_event cria o subdiretório do LOG_FILE redirecionado
            # (mesmo bug de [log-error] que o judge tinha com o override).
            web.LOG_FILE = Path(td) / "logs" / "novo" / "kalshi.jsonl"
            log_event("test_log_dir", {"n": 1})
            assert web.LOG_FILE.exists(), web.LOG_FILE
            rec = json.loads(
                web.LOG_FILE.read_text(encoding="utf-8").splitlines()[0])
            assert rec["event_type"] == "test_log_dir", rec
            print("Test 1 PASS: log_event cria subdiretório novo do "
                  "--log-file (sem [log-error])")

            # Test 2: excepthook injeta o traceback do crash no JSONL.
            try:
                raise RuntimeError("boom sintético do teste")
            except RuntimeError:
                et, ev, tb = sys.exc_info()
            print("(traceback sintético abaixo é esperado)", file=sys.stderr)
            _log_uncaught(et, ev, tb)
            rec = json.loads(
                web.LOG_FILE.read_text(encoding="utf-8").splitlines()[-1])
            assert rec["event_type"] == "error", rec
            assert rec["payload"]["where"] == "kalshi_uncaught", rec
            assert "boom sintético do teste" in rec["payload"]["traceback"]
            assert rec["level"] == "ERROR", rec
            print("Test 2 PASS: crash não-tratado vira evento error com "
                  "traceback no JSONL")

            # Test 3: KeyboardInterrupt (Ctrl+C) NÃO vira evento — é
            # encerramento normal, não crash.
            n_before = len(
                web.LOG_FILE.read_text(encoding="utf-8").splitlines())
            try:
                raise KeyboardInterrupt()
            except KeyboardInterrupt:
                et, ev, tb = sys.exc_info()
            _log_uncaught(et, ev, tb)
            n_after = len(
                web.LOG_FILE.read_text(encoding="utf-8").splitlines())
            assert n_after == n_before, (n_before, n_after)
            print("Test 3 PASS: Ctrl+C não polui o JSONL")

        print("\nAll --test-logging PASS (3/3)")
    finally:
        web.LOG_FILE = saved_logfile


if __name__ == "__main__":
    if "--test-lst-date" in sys.argv:
        _test_lst_date()
        sys.exit(0)
    if "--test-discovery" in sys.argv:
        _test_discovery()
        sys.exit(0)
    if "--test-execute" in sys.argv:
        _test_execute()
        sys.exit(0)
    if "--test-monitor" in sys.argv:
        _test_monitor()
        sys.exit(0)
    if "--test-resolution" in sys.argv:
        _test_resolution()
        sys.exit(0)
    if "--test-caution-cost" in sys.argv:
        _test_caution_cost()
        sys.exit(0)
    if "--test-logging" in sys.argv:
        _test_logging()
        sys.exit(0)
    raise SystemExit(main())
