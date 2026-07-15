"""Aba Kalshi — KPIs, posições abertas, histórico e performance do bot
kalshi_edge_bot. Read-only, fail-soft.

Fontes:
  - settings.KALSHI_EDGE_DB (~/.polymarket-paper/kalshi_edge.db): entries/
    monitor_checks/cashouts/resolutions do daemon Kalshi (mesmo schema
    weather_edge_db, apontado via WEATHER_EDGE_DB_PATH no serviço).
  - settings.PORTFOLIO_DB, portfólio "kalshi": banca paper SEPARADA da
    Polymarket (decisão do plano), mesmo portfolio.db.

Deliberadamente NÃO parametriza os services Polymarket existentes
(positions/analytics) neste PR: as queries são cópias enxutas trocando o DB,
sem o refresh CLOB (tickers Kalshi não existem no CLOB — o monitor do bot
mantém positions.current_price honesto via set_position_price) e sem os
links de evento da Polymarket.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .. import settings as S
# Reuso deliberado: _latest_bid/_latest_forecast_prob_yes recebem a conexão
# (venue-agnósticos — o bot Kalshi grava monitor_checks com a MESMA convenção
# forecast_prob_now = P(side) do bot Polymarket).
from .positions import (_humanize_duration, _latest_bid,
                        _latest_forecast_prob_yes, trigger_distances)

import weather_edge_db as wdb  # noqa: E402  (sys.path via settings)


def _ro_conn(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(str(path))
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# KPIs
# ---------------------------------------------------------------------------


def get_kpis() -> dict:
    """KPIs da banca paper "kalshi" + realizados do dia do kalshi_edge.db.

    Mesmas chaves do partial kpi_cards (portfolio_total_usd, open_positions,
    realized_pnl_today_usd, drawdown_pct_from_peak, ...) + "available".
    Fail-soft: banca inexistente (operador ainda não rodou
    `paper_engine --action init --name kalshi`) → available=False com
    reason, sem exceção."""
    try:
        pconn = _ro_conn(S.PORTFOLIO_DB)
    except FileNotFoundError:
        return {"available": False, "reason": "portfolio.db não encontrado"}
    try:
        # active=1 + id DESC: --action init desativa a banca antiga e cria
        # uma nova com o MESMO nome; sem o filtro, fetchone() devolvia a
        # linha mais antiga (desativada) e o card mostrava a banca errada
        # após qualquer re-init (visto no smoke: $997.57 da banca morta
        # enquanto --status mostrava $2000 da ativa).
        pf = pconn.execute(
            "SELECT * FROM portfolios WHERE name = ? AND active = 1 "
            "ORDER BY id DESC LIMIT 1",
            (S.KALSHI_PORTFOLIO,)).fetchone()
        if not pf:
            return {"available": False,
                    "reason": (f'banca paper "{S.KALSHI_PORTFOLIO}" não '
                               'existe — rode: python paper_engine.py '
                               f'--action init --name {S.KALSHI_PORTFOLIO} '
                               '--balance 1000')}
        cash = float(pf["cash_balance"])
        pid = pf["id"]
        starting = float(pf["starting_balance"])

        # Sem refresh CLOB: current_price é mantido pelo monitor do bot
        # (set_position_price) a cada ciclo.
        rows = pconn.execute(
            "SELECT * FROM positions WHERE portfolio_id = ? AND closed = 0",
            (pid,)).fetchall()
        positions_value = sum(
            float(p["shares"]) * float(p["current_price"] or p["avg_entry"])
            for p in rows)
        bank_open_count = len(rows)
        total = cash + positions_value

        # "Open Positions" conta as ENTRIES abertas do bot (kalshi_edge.db),
        # a MESMA fonte da tabela logo abaixo — a banca paper pode descasar
        # após um reset parcial (init da banca sem reset do DB, ou
        # vice-versa), e mostrar a contagem da banca ao lado de uma tabela
        # com outra contagem era pura confusão. O descasamento vira aviso
        # explícito (bank_synced=False) em vez de números contraditórios.
        open_count = bank_open_count
        try:
            wconn2 = _ro_conn(S.KALSHI_EDGE_DB)
            try:
                open_count = len(wdb.query_open_positions(wconn2))
            finally:
                wconn2.close()
        except FileNotFoundError:
            pass

        # Realizado hoje (UTC): cashouts + resoluções liquidadas hoje
        # (excluindo entries que já tiveram cashout — mesmo critério do
        # service Polymarket).
        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        realized_today = 0.0
        if S.KALSHI_EDGE_DB.exists():
            wconn = _ro_conn(S.KALSHI_EDGE_DB)
            try:
                row = wconn.execute(
                    "SELECT COALESCE(SUM(realized_pnl_usd), 0) "
                    "FROM cashouts WHERE DATE(ts) = ?", (today_utc,),
                ).fetchone()
                realized_today = float(row[0] or 0)
                try:
                    rrow = wconn.execute(
                        "SELECT COALESCE(SUM("
                        "  (r.payout_per_share - e.entry_price) * e.size_shares"
                        "), 0) "
                        "FROM resolutions r "
                        "JOIN entries e ON e.entry_id = r.entry_id "
                        "LEFT JOIN cashouts c ON c.entry_id = r.entry_id "
                        "WHERE DATE(r.ts_resolved) = ? AND c.cashout_id IS NULL",
                        (today_utc,)).fetchone()
                    realized_today += float(rrow[0] or 0)
                except sqlite3.OperationalError:
                    pass
            finally:
                wconn.close()

        delta_today = None
        snap = pconn.execute(
            "SELECT total_value FROM daily_snapshots "
            "WHERE portfolio_id = ? ORDER BY date DESC LIMIT 1",
            (pid,)).fetchone()
        if snap and snap[0]:
            delta_today = total - float(snap[0])

        peak_row = pconn.execute(
            "SELECT MAX(total_value) FROM daily_snapshots "
            "WHERE portfolio_id = ?", (pid,)).fetchone()
        peak = max(float(peak_row[0] or starting), starting, total)
        dd_pct = ((total - peak) / peak * 100) if peak > 0 else 0.0

        try:
            import paper_engine  # noqa
            max_pos = int(paper_engine.DEFAULT_RISK.get(
                "max_concurrent_positions", 50))
        except Exception:
            max_pos = 30

        return {
            "available": True,
            "portfolio_total_usd": round(total, 2),
            "portfolio_delta_today_usd": (round(delta_today, 2)
                                          if delta_today is not None else None),
            "open_positions": open_count,
            "max_positions": max_pos,
            "bank_open_count": bank_open_count,
            "bank_synced": bank_open_count == open_count,
            "realized_pnl_today_usd": round(realized_today, 2),
            "drawdown_pct_from_peak": round(dd_pct, 2),
            "drawdown_peak_usd": round(peak, 2),
            "cash_usd": round(cash, 2),
            "positions_value_usd": round(positions_value, 2),
            "starting_balance_usd": round(starting, 2),
            "price_source": "monitor",
        }
    finally:
        pconn.close()


# ---------------------------------------------------------------------------
# Posições abertas
# ---------------------------------------------------------------------------


def get_open_positions(sort_by: str = "entry_id") -> list[dict]:
    """Posições Kalshi abertas com bid do último monitor_check, P&L paper e
    distâncias de trigger. market_slug É o ticker Kalshi."""
    try:
        conn = _ro_conn(S.KALSHI_EDGE_DB)
    except FileNotFoundError:
        return []
    try:
        rows = list(wdb.query_open_positions(conn))
        out = []
        now = datetime.now(timezone.utc)
        for row in rows:
            entry_id = row["entry_id"]
            entry_price = float(row["entry_price"] or 0)
            shares = float(row["size_shares"] or 0)
            side = row["side"]
            peak = (float(row["peak_bid_seen"])
                    if row["peak_bid_seen"] is not None else None)
            current_bid, bid_ts = _latest_bid(conn, entry_id)
            if current_bid is None:
                current_bid = entry_price
            fcst = _latest_forecast_prob_yes(conn, entry_id)
            distances = trigger_distances(
                side=side, entry_price=entry_price, current_bid=current_bid,
                peak_bid=peak if peak is not None else current_bid,
                forecast_prob_yes=fcst)
            paper_pnl = (current_bid - entry_price) * shares
            try:
                entry_ts = datetime.fromisoformat(
                    row["ts"].replace("Z", "+00:00"))
                held_seconds = int((now - entry_ts).total_seconds())
            except Exception:
                held_seconds = 0
            out.append({
                "entry_id": entry_id,
                "ticker": row["market_slug"],
                "event_ticker": (row["ladder_event_slug"]
                                 if "ladder_event_slug" in row.keys()
                                 else None),
                "market_question": row["market_question"],
                "city": row["city_resolved"],
                "side": side,
                "entry_price": entry_price,
                "size_shares": shares,
                "size_usd": float(row["size_usd"] or 0),
                "current_bid": round(current_bid, 4),
                "peak_bid": round(peak, 4) if peak is not None else None,
                "paper_pnl_usd": round(paper_pnl, 2),
                "paper_pnl_pct": (round((current_bid - entry_price)
                                        / entry_price * 100, 2)
                                  if entry_price > 0 else 0),
                "held_human": _humanize_duration(held_seconds),
                "forecast_prob_yes": (round(fcst, 3)
                                      if fcst is not None else None),
                "bid_ts": bid_ts,
                "end_date": row["end_date"],
                "edge_pp_at_entry": float(row["edge_pp_at_entry"] or 0),
                "triggers": distances,
            })
        if sort_by == "size":
            out.sort(key=lambda p: p["size_usd"], reverse=True)
        else:
            out.sort(key=lambda p: p["entry_id"], reverse=True)
        return out
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Histórico
# ---------------------------------------------------------------------------


def get_history(limit: int = 200,
                filter_outcome: Optional[str] = None) -> list[dict]:
    """Posições Kalshi resolvidas (cashout OU settlement), mais recentes
    primeiro. Mesma semântica do histórico Polymarket."""
    try:
        conn = _ro_conn(S.KALSHI_EDGE_DB)
    except FileNotFoundError:
        return []
    try:
        rows = conn.execute("""
            SELECT
              e.entry_id, e.ts, e.market_slug, e.market_question,
              e.city_resolved, e.side, e.entry_price, e.size_shares,
              e.size_usd, e.threshold_value, e.threshold_unit,
              e.end_date, e.edge_pp_at_entry,
              c.cashout_id, c.ts AS cashout_ts, c.exit_price,
              c.realized_pnl_usd AS cashout_pnl, c.reason AS cashout_reason,
              r.resolution_id, r.ts_resolved, r.final_outcome,
              r.payout_per_share
            FROM entries e
            LEFT JOIN cashouts c ON c.entry_id = e.entry_id
            LEFT JOIN resolutions r ON r.entry_id = e.entry_id
            WHERE e.status IN ('EXECUTED','FAST_PATH')
              AND (c.cashout_id IS NOT NULL OR r.resolution_id IS NOT NULL)
            ORDER BY COALESCE(r.ts_resolved, c.ts) DESC
            LIMIT ?
        """, (limit,)).fetchall()
        out = []
        for row in rows:
            entry_price = float(row["entry_price"] or 0)
            shares = float(row["size_shares"] or 0)
            stake = float(row["size_usd"] or 0)
            if row["resolution_id"] and not row["cashout_id"]:
                exit_kind = "resolution"
                exit_price = float(row["payout_per_share"] or 0)
                exit_ts = row["ts_resolved"]
                realized = (exit_price - entry_price) * shares
                reason = f"resolved {row['final_outcome']}"
            else:
                exit_kind = "cashout"
                exit_price = float(row["exit_price"] or 0)
                exit_ts = row["cashout_ts"]
                realized = float(row["cashout_pnl"] or 0)
                reason = row["cashout_reason"] or ""
            pnl_pct = (realized / stake * 100) if stake else 0.0
            is_win = realized > 0
            if filter_outcome == "winner" and not is_win:
                continue
            if filter_outcome == "loser" and is_win:
                continue
            try:
                t0 = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
                t1 = datetime.fromisoformat(
                    (exit_ts or row["ts"]).replace("Z", "+00:00"))
                held_seconds = int((t1 - t0).total_seconds())
            except Exception:
                held_seconds = 0
            out.append({
                "entry_id": row["entry_id"],
                "ts": row["ts"],
                "exit_ts": exit_ts,
                "exit_kind": exit_kind,
                "ticker": row["market_slug"],
                "market_question": row["market_question"],
                "city": row["city_resolved"],
                "side": row["side"],
                "entry_price": round(entry_price, 4),
                "exit_price": round(exit_price, 4),
                "size_usd": round(stake, 2),
                "size_shares": round(shares, 2),
                "realized_pnl_usd": round(realized, 2),
                "pnl_pct": round(pnl_pct, 2),
                "is_win": is_win,
                "reason": reason,
                "final_outcome": row["final_outcome"],
                "held_human": _humanize_duration(held_seconds),
                "threshold_value": row["threshold_value"],
                "threshold_unit": row["threshold_unit"],
                "edge_pp_at_entry": float(row["edge_pp_at_entry"] or 0),
            })
        return out
    finally:
        conn.close()


def get_history_summary() -> dict:
    try:
        conn = _ro_conn(S.KALSHI_EDGE_DB)
    except FileNotFoundError:
        return {"available": False}
    try:
        row = conn.execute("""
            SELECT
              COUNT(*) n_total,
              SUM(CASE WHEN COALESCE(c.realized_pnl_usd,
                    (r.payout_per_share - e.entry_price) * e.size_shares
                  ) > 0 THEN 1 ELSE 0 END) n_wins,
              SUM(COALESCE(c.realized_pnl_usd,
                    (r.payout_per_share - e.entry_price) * e.size_shares
                  )) total_pnl,
              SUM(e.size_usd) total_stake
            FROM entries e
            LEFT JOIN cashouts c ON c.entry_id = e.entry_id
            LEFT JOIN resolutions r ON r.entry_id = e.entry_id
            WHERE e.status IN ('EXECUTED','FAST_PATH')
              AND (c.cashout_id IS NOT NULL OR r.resolution_id IS NOT NULL)
        """).fetchone()
        n = row["n_total"] or 0
        wins = row["n_wins"] or 0
        pnl = float(row["total_pnl"] or 0)
        stake = float(row["total_stake"] or 0)
        out = {
            "available": True,
            "n_total": n,
            "n_wins": wins,
            "n_losses": n - wins,
            "win_rate": round(wins / n, 3) if n else None,
            "total_pnl_usd": round(pnl, 2),
            "total_stake_usd": round(stake, 2),
            "pnl_per_dollar": round(pnl / stake, 4) if stake else None,
        }
        # "Custo da cautela": counterfactuals das rejeições (bot grava
        # quanto cada REJECTED/SKIPPED teria rendido por $100 nocionais).
        # pnl>0 recorrente = judge deixando dinheiro na mesa.
        try:
            c = conn.execute("""
                SELECT COUNT(*) n,
                       SUM(cf.hypothetical_hold_pnl) total,
                       SUM(CASE WHEN cf.hypothetical_hold_pnl > 0
                           THEN 1 ELSE 0 END) n_win
                FROM counterfactuals cf
                JOIN entries e ON e.entry_id = cf.entry_id
                WHERE e.status IN ('REJECTED','SKIPPED')
            """).fetchone()
            out["caution_n"] = c["n"] or 0
            out["caution_would_win"] = c["n_win"] or 0
            out["caution_total_per_100"] = round(float(c["total"] or 0), 2)
        except sqlite3.OperationalError:
            out["caution_n"] = 0
        return out
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def replay_entry_md(entry_id: int) -> str:
    """Replay markdown de um entry Kalshi — reusa o replay_entry
    venue-agnóstico do analyzer (recebe a conexão), apontado para o
    kalshi_edge.db. Inclui o veredito completo do judge (rationale,
    probs, custo) gravado em judge_reviews. Fail-soft."""
    try:
        conn = _ro_conn(S.KALSHI_EDGE_DB)
    except FileNotFoundError:
        return "_(kalshi_edge.db não encontrado — o bot ainda não rodou)_"
    try:
        from weather_edge_analyzer import replay_entry
        return replay_entry(conn, entry_id)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------------


def get_performance_series(days: int = 30) -> list[dict]:
    """P&L realizado por dia (cashouts + resoluções sem cashout) e cumulativo.

    Diferente do get_cumulative_pnl_series da Polymarket (só cashouts): na
    Kalshi a maioria das posições liquida por settlement, então ignorar as
    resoluções esconderia quase todo o P&L."""
    if not S.KALSHI_EDGE_DB.exists():
        return []
    from .analytics import since_iso
    since = since_iso(days)
    conn = _ro_conn(S.KALSHI_EDGE_DB)
    try:
        rows = conn.execute("""
            SELECT day, SUM(pnl) AS daily_pnl FROM (
              SELECT DATE(ts) AS day, realized_pnl_usd AS pnl
              FROM cashouts WHERE ts >= :since
              UNION ALL
              SELECT DATE(r.ts_resolved) AS day,
                     (r.payout_per_share - e.entry_price) * e.size_shares AS pnl
              FROM resolutions r
              JOIN entries e ON e.entry_id = r.entry_id
              LEFT JOIN cashouts c ON c.entry_id = r.entry_id
              WHERE r.ts_resolved >= :since AND c.cashout_id IS NULL
            ) GROUP BY day ORDER BY day ASC
        """, {"since": since}).fetchall()
        cum = 0.0
        out = []
        for r in rows:
            cum += float(r["daily_pnl"] or 0)
            out.append({"date": r["day"],
                        "daily_pnl": round(float(r["daily_pnl"] or 0), 2),
                        "cumulative_pnl": round(cum, 2)})
        return out
    finally:
        conn.close()
