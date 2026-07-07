#!/usr/bin/env python3
"""Repara a atribuição de P&L distorcida pelo bug de posições fundidas
(post-mortem 2026-07-06, corrigido em v13.4).

O paper engine chaveia posições por (portfolio, token, side); o weather bot
chaveia entries por perna de ladder. Discoveries repetidas no mesmo mercado
criaram entries em grupos de ladder DIFERENTES compartilhando o MESMO token
de outcome — todas as compras fundiram numa única posição. O primeiro cashout
vendeu a posição inteira (inclusive as shares das irmãs) e registrou tudo na
própria linha de cashouts; as irmãs ficaram sem fechar ("No open position" a
cada tick) até o sweep de resolução escrever uma linha de resolutions.

Distorções resultantes (só de ATRIBUIÇÃO — o caixa do portfolio está correto,
pois foi creditado nas vendas reais):

  1. `cashout_overstated` — a linha de cashouts da entry vencedora registra
     exit_shares/realized_pnl_usd da posição fundida INTEIRA (shares > as
     próprias size_shares).
  2. `phantom_resolution_win` — entry irmã sem cashout ganhou uma linha de
     resolutions; os relatórios (COALESCE(c.realized_pnl_usd, fórmula da
     resolução)) fabricam P&L de payout para shares que já tinham sido
     vendidas no cashout fundido do irmão.
  3. `stuck_open_position` — linha closed=0 órfã no portfolio.db (vítimas do
     IntegrityError pré-migração) cujas entries já estão todas liquidadas no
     weather_edge.db.

Correções (com --apply):

  1. Re-atribui pro-rata: a linha superestimada passa a registrar só as
     próprias shares, pnl = (exit_price − entry_price) × size_shares; cada
     irmã cujas shares foram vendidas na mesma ordem recebe uma linha
     `phantom_shared_close:historical_repair` com o pnl atribuído. A soma dos
     pnl atribuídos é idêntica ao pnl fundido original (fee 0) — nada de
     dinheiro é criado ou destruído, só re-atribuído.
  2. Idem (a linha historical_repair da irmã faz o COALESCE parar de cair na
     fórmula de resolução).
  3. Fecha a posição órfã via paper_engine.close_position(force_exit_price=
     payout da resolução) — requer o schema pós-migração (índice parcial).

Se a reconciliação de shares não fecha (ex.: trades manuais entremeados), o
token é rebaixado a REPORT-ONLY e nada é escrito para ele.

Caixa do portfolio: NUNCA ajustado por 1-2 (foi creditado corretamente nas
vendas reais). Rode com os daemons parados:

    systemctl --user stop weather-edge-bot weather-edge-judge
    python repair_ladder_cashouts.py            # auditoria, sem writes
    python repair_ladder_cashouts.py --apply    # corrige
    systemctl --user start weather-edge-bot weather-edge-judge

Depois do --apply, recomputar contrafactuais:
    python weather_edge_analyzer.py --recompute-counterfactuals
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                       / "polymarket-paper-trader" / "scripts"))

import weather_edge_db as db  # noqa: E402

PORTFOLIO_DB = Path.home() / ".polymarket-paper" / "portfolio.db"
_EPS_SHARES = 0.01          # tolerância absoluta na reconciliação de shares
_OPEN = ("EXECUTED", "FAST_PATH")


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _settled(conn, entry_id: int) -> bool:
    """Entry liquidada = tem cashout OU resolution."""
    return conn.execute(
        "SELECT 1 FROM cashouts WHERE entry_id=? "
        "UNION SELECT 1 FROM resolutions WHERE entry_id=? LIMIT 1",
        (entry_id, entry_id)).fetchone() is not None


# ---------------------------------------------------------------------------
# Categorias 1+2 — atribuição pro-rata em weather_edge.db
# ---------------------------------------------------------------------------

def audit_attribution(conn) -> list[dict]:
    """Encontra cashouts superestimados e reconstrói a atribuição por token.

    Para cada linha de cashouts com exit_shares > size_shares da própria
    entry: as shares excedentes pertencem às entries irmãs (mesmo token do
    lado apostado) executadas ANTES do cashout e sem cashout próprio. Se
    own + Σ(irmãs) ≈ exit_shares, a atribuição é determinística; senão o
    token é ambíguo (report-only).

    Retorna uma lista de "planos" por cashout superestimado:
      {status: 'deterministic'|'ambiguous', cashout_id, entry_id, token,
       exit_price, old_shares, old_pnl, new_shares, new_pnl,
       siblings: [{entry_id, shares, pnl, has_resolution, category}]}
    """
    plans = []
    rows = conn.execute(
        "SELECT c.cashout_id, c.entry_id, c.ts AS cashout_ts, c.exit_price, "
        "       c.exit_shares, c.realized_pnl_usd, c.reason, "
        "       e.ts AS entry_ts, e.side, e.entry_price, e.size_shares, "
        "       e.token_id_yes, e.token_id_no, e.market_slug "
        "FROM cashouts c JOIN entries e ON e.entry_id = c.entry_id "
        "WHERE (c.reason IS NULL OR c.reason NOT LIKE 'phantom_shared_close%') "
        "  AND c.exit_shares IS NOT NULL AND e.size_shares IS NOT NULL "
        "  AND c.exit_shares > e.size_shares + ? "
        "ORDER BY c.cashout_id", (_EPS_SHARES,)).fetchall()

    for r in rows:
        token = r["token_id_yes"] if r["side"] == "YES" else r["token_id_no"]
        tok_col = "token_id_yes" if r["side"] == "YES" else "token_id_no"
        # Irmãs: mesmo token/lado, executadas antes do cashout, sem cashout
        # próprio (a linha delas não existe — é exatamente o buraco).
        sibs = conn.execute(
            f"SELECT e.entry_id, e.entry_price, e.size_shares, "
            f"       (SELECT COUNT(*) FROM resolutions x "
            f"        WHERE x.entry_id = e.entry_id) AS has_res "
            f"FROM entries e "
            f"LEFT JOIN cashouts c ON c.entry_id = e.entry_id "
            f"WHERE e.{tok_col} = ? AND e.side = ? "
            f"  AND e.entry_id != ? AND e.status IN ('EXECUTED','FAST_PATH') "
            f"  AND e.ts <= ? AND c.cashout_id IS NULL "
            f"ORDER BY e.ts", (token, r["side"], r["entry_id"],
                               r["cashout_ts"])).fetchall()

        own = float(r["size_shares"])
        sib_total = sum(float(s["size_shares"] or 0) for s in sibs)
        exit_shares = float(r["exit_shares"])
        exit_price = float(r["exit_price"] or 0)
        plan = {
            "cashout_id": r["cashout_id"], "entry_id": r["entry_id"],
            "slug": r["market_slug"], "token": token,
            "exit_price": exit_price,
            "old_shares": exit_shares,
            "old_pnl": float(r["realized_pnl_usd"] or 0),
            "new_shares": own,
            "new_pnl": round((exit_price - float(r["entry_price"] or 0)) * own, 4),
            "siblings": [],
        }
        if abs(own + sib_total - exit_shares) <= max(_EPS_SHARES,
                                                     0.01 * exit_shares):
            plan["status"] = "deterministic"
            for s in sibs:
                sh = float(s["size_shares"] or 0)
                plan["siblings"].append({
                    "entry_id": s["entry_id"],
                    "shares": sh,
                    "pnl": round((exit_price - float(s["entry_price"] or 0))
                                 * sh, 4),
                    "has_resolution": bool(s["has_res"]),
                    "category": ("phantom_resolution_win" if s["has_res"]
                                 else "phantom_open"),
                })
        else:
            plan["status"] = "ambiguous"
            plan["reconcile_delta"] = round(own + sib_total - exit_shares, 4)
        plans.append(plan)
    return plans


def apply_attribution(conn, plan: dict) -> None:
    """Aplica um plano determinístico: corrige a linha superestimada e
    insere as linhas historical_repair das irmãs. NÃO commita."""
    conn.execute(
        "UPDATE cashouts SET exit_shares = ?, realized_pnl_usd = ?, "
        "reason = COALESCE(reason,'') || ' |repaired v13.4 (was shares=' "
        "|| ? || ', pnl=' || ? || ')' "
        "WHERE cashout_id = ?",
        (plan["new_shares"], plan["new_pnl"],
         round(plan["old_shares"], 4), round(plan["old_pnl"], 4),
         plan["cashout_id"]))
    for s in plan["siblings"]:
        db.insert_cashout(
            conn, entry_id=s["entry_id"], ts=_now_iso(),
            exit_price=plan["exit_price"], exit_shares=s["shares"],
            realized_pnl_usd=s["pnl"],
            reason="phantom_shared_close:historical_repair")


# ---------------------------------------------------------------------------
# Categoria 3 — posições órfãs no portfolio.db
# ---------------------------------------------------------------------------

def audit_stuck_positions(conn, portfolio_db: Path) -> list[dict]:
    """Linhas closed=0 no portfolio.db cujas entries (mesmo token+side no
    weather_edge.db) estão TODAS liquidadas — vítimas do IntegrityError
    pré-migração. Payout de fechamento vem da resolução mais recente."""
    if not portfolio_db.exists():
        return []
    out = []
    pconn = sqlite3.connect(str(portfolio_db))
    pconn.row_factory = sqlite3.Row
    try:
        positions = pconn.execute(
            "SELECT p.* FROM positions p "
            "JOIN portfolios pf ON pf.id = p.portfolio_id AND pf.active = 1 "
            "WHERE p.closed = 0").fetchall()
    finally:
        pconn.close()

    for pos in positions:
        tok_col = ("token_id_yes" if pos["side"] == "YES" else "token_id_no")
        owners = conn.execute(
            f"SELECT entry_id, status FROM entries "
            f"WHERE {tok_col} = ? AND side = ?",
            (pos["token_id"], pos["side"])).fetchall()
        if not owners:
            continue  # posição de outra origem (CLI manual etc.) — não é nossa
        live = [o for o in owners
                if o["status"] in _OPEN and not _settled(conn, o["entry_id"])]
        if live:
            continue  # ainda há entry aberta — o monitor cuida dela
        # payout: resolução mais recente entre as entries donas
        res = conn.execute(
            f"SELECT r.final_outcome, r.payout_per_share FROM resolutions r "
            f"JOIN entries e ON e.entry_id = r.entry_id "
            f"WHERE e.{tok_col} = ? AND e.side = ? "
            f"ORDER BY r.resolution_id DESC LIMIT 1",
            (pos["token_id"], pos["side"])).fetchone()
        out.append({
            "position_id": pos["id"], "token": pos["token_id"],
            "side": pos["side"], "shares": float(pos["shares"] or 0),
            "close_price": (float(res["payout_per_share"])
                            if res and res["payout_per_share"] is not None
                            else None),
            "owners": [o["entry_id"] for o in owners],
        })
    return out


def apply_stuck_close(item: dict, portfolio_name: str = "default") -> str:
    """Fecha a posição órfã via paper_engine (force_exit_price, sem rede).
    Requer o schema pós-migração (fix D) — o import já a executa."""
    if item["close_price"] is None:
        return "no_resolution_price"
    try:
        import paper_engine
    except ImportError as e:
        return f"paper_engine_import_failed: {e}"
    try:
        r = paper_engine.close_position(
            token_id=item["token"], side=item["side"],
            portfolio_name=portfolio_name,
            reasoning="repair_ladder_cashouts: stuck position "
                      f"(entries {item['owners']})",
            force_exit_price=item["close_price"])
        results = r if isinstance(r, list) else [r]
        return ("closed" if all(x.get("status") == "closed" for x in results)
                else "close_failed")
    except Exception as e:
        return f"close_failed: {e}"


# ---------------------------------------------------------------------------
# Orquestração
# ---------------------------------------------------------------------------

def run(core_db_path=None, portfolio_db_path=None, apply: bool = False,
        portfolio_name: str = "default") -> dict:
    """Auditoria (e, com apply=True, reparo). Retorna o resumo (testável)."""
    portfolio_db = Path(portfolio_db_path) if portfolio_db_path else PORTFOLIO_DB
    core_path = Path(core_db_path) if core_db_path else None
    connect = (lambda: db.connect(core_path)) if core_path else db.connect

    with connect() as conn:
        plans = audit_attribution(conn)
        stuck = audit_stuck_positions(conn, portfolio_db)

    det = [p for p in plans if p["status"] == "deterministic"]
    amb = [p for p in plans if p["status"] == "ambiguous"]
    n_phantoms = sum(len(p["siblings"]) for p in det)
    n_res_wins = sum(1 for p in det for s in p["siblings"]
                     if s["category"] == "phantom_resolution_win")

    applied = {"attribution": 0, "phantom_rows": 0, "stuck_closed": 0,
               "stuck_results": []}
    if apply:
        if det:
            with connect() as conn:
                for p in det:
                    apply_attribution(conn, p)
                    applied["attribution"] += 1
                    applied["phantom_rows"] += len(p["siblings"])
                conn.commit()
        for item in stuck:
            outcome = apply_stuck_close(item, portfolio_name)
            applied["stuck_results"].append(
                {"position_id": item["position_id"], "outcome": outcome})
            if outcome == "closed":
                applied["stuck_closed"] += 1

    return {"overstated": len(plans), "deterministic": len(det),
            "ambiguous": len(amb), "phantom_siblings": n_phantoms,
            "phantom_resolution_wins": n_res_wins, "stuck": len(stuck),
            "plans": plans, "stuck_positions": stuck, "applied": applied}


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Audita/repara a atribuição de cashouts fundidos "
                    "(bug de token compartilhado, 2026-07-06). Dry-run por "
                    "default; --apply escreve.")
    ap.add_argument("--apply", action="store_true",
                    help="escreve as correções (default: só relatório)")
    ap.add_argument("--portfolio", default="default",
                    help="nome do portfolio paper (default: default)")
    args = ap.parse_args()
    dry = not args.apply

    s = run(apply=args.apply, portfolio_name=args.portfolio)

    print(f"{'DRY-RUN' if dry else 'APPLY'}: {s['overstated']} cashout(s) "
          f"superestimado(s) — {s['deterministic']} determinístico(s), "
          f"{s['ambiguous']} ambíguo(s)\n")
    for p in s["plans"]:
        tag = "OK  " if p["status"] == "deterministic" else "AMBÍGUO"
        print(f"  [{tag}] cashout #{p['cashout_id']} entry #{p['entry_id']} "
              f"{p['slug'][:44]:44s} shares {p['old_shares']}→{p['new_shares']} "
              f"pnl {p['old_pnl']:+.2f}→{p['new_pnl']:+.2f}")
        for sib in p["siblings"]:
            print(f"          ↳ irmã #{sib['entry_id']}: {sib['shares']} shares "
                  f"@ ${p['exit_price']:.3f} → pnl {sib['pnl']:+.2f} "
                  f"({sib['category']})")
        if p["status"] == "ambiguous":
            print(f"          shares não reconciliam (Δ={p['reconcile_delta']}) "
                  f"— nada será escrito para este token")
    if s["stuck_positions"]:
        print(f"\n  Posições órfãs no portfolio.db ({s['stuck']}):")
        for it in s["stuck_positions"]:
            px = (f"${it['close_price']:.2f}" if it["close_price"] is not None
                  else "SEM RESOLUÇÃO (report-only)")
            print(f"    position #{it['position_id']} side={it['side']} "
                  f"{it['shares']} shares — fechar a {px} "
                  f"(entries {it['owners']})")

    print(f"\nresumo: overstated={s['overstated']} "
          f"phantom_siblings={s['phantom_siblings']} "
          f"(resolution_wins={s['phantom_resolution_wins']}) "
          f"stuck={s['stuck']}")
    if dry and (s["deterministic"] or s["stuck"]):
        print("  → re-rode com --apply para corrigir (daemons parados).")
    elif not dry:
        a = s["applied"]
        print(f"  → corrigidos {a['attribution']} cashout(s), inseridas "
              f"{a['phantom_rows']} linha(s) historical_repair, fechadas "
              f"{a['stuck_closed']}/{s['stuck']} posição(ões) órfã(s).")
        if a["stuck_results"]:
            for r in a["stuck_results"]:
                print(f"     position #{r['position_id']}: {r['outcome']}")
        print("  → recomende: recomputar contrafactuais "
              "(weather_edge_analyzer --recompute-counterfactuals).")


# ---------------------------------------------------------------------------
# Teste inline (offline, DBs sintéticos)
# ---------------------------------------------------------------------------

def _test_attribution() -> None:
    import tempfile
    tmpdir = Path(tempfile.mkdtemp())
    core = tmpdir / "core_test.db"
    port = tmpdir / "portfolio_test.db"
    db.init_db(core)
    ts0 = "2026-07-06T00:00:00+00:00"
    ts1 = "2026-07-06T06:00:00+00:00"

    with db.connect(core) as conn:
        def add_entry(slug, ts, price, shares, tok="N1", status="EXECUTED"):
            conn.execute(
                "INSERT INTO entries (ts, market_slug, market_question, side, "
                "status, entry_price, size_shares, token_id_yes, token_id_no, "
                "strategy) VALUES (?, ?, 'q', 'NO', ?, ?, ?, 'Y1', ?, "
                "'weather_edge')", (ts, slug, status, price, shares, tok))
            return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # e1 (10 sh @ .30) cashout vendeu 15 sh @ .90 (fundiu as 5 de e2)
        e1 = add_entry("seoul-26c", ts0, 0.30, 10.0)
        e2 = add_entry("seoul-26c-run2", ts0, 0.40, 5.0)
        conn.execute(
            "INSERT INTO cashouts (entry_id, ts, exit_price, exit_shares, "
            "realized_pnl_usd, reason) VALUES (?, ?, 0.9, 15.0, 8.5, "
            "'ladder_group:convergence')", (e1, ts1))
        conn.execute(
            "INSERT INTO resolutions (entry_id, ts_resolved, final_outcome, "
            "payout_per_share) VALUES (?, '2026-07-06T12:00:00+00:00', "
            "'NO', 1.0)", (e2,))
        # caso ambíguo: e3 vendeu 12 sh mas own+irmã=9 (não reconcilia)
        e3 = add_entry("sf-66f", ts0, 0.20, 6.0, tok="N2")
        add_entry("sf-66f-run2", ts0, 0.25, 3.0, tok="N2")
        conn.execute(
            "INSERT INTO cashouts (entry_id, ts, exit_price, exit_shares, "
            "realized_pnl_usd, reason) VALUES (?, ?, 0.8, 12.0, 6.0, "
            "'stop_loss')", (e3, ts1))
        conn.commit()

    # portfolio.db sintético: posição órfã closed=0 no token N1 (entries
    # todas liquidadas após o repair) — só dry-run aqui.
    pconn = sqlite3.connect(port)
    pconn.executescript("""
        CREATE TABLE portfolios (id INTEGER PRIMARY KEY, name TEXT,
            starting_balance REAL, cash_balance REAL, peak_value REAL,
            created_at TEXT, updated_at TEXT, risk_config TEXT,
            active INTEGER);
        CREATE TABLE positions (id INTEGER PRIMARY KEY, portfolio_id INTEGER,
            token_id TEXT, market_question TEXT, side TEXT, shares REAL,
            avg_entry REAL, current_price REAL, opened_at TEXT,
            updated_at TEXT, closed INTEGER DEFAULT 0, closed_at TEXT);
        INSERT INTO portfolios VALUES (1,'default',1000,1000,1000,'t','t','{}',1);
        INSERT INTO positions (portfolio_id, token_id, side, shares,
            avg_entry, current_price, opened_at, updated_at, closed)
            VALUES (1,'N1','NO',5.0,0.4,0.4,'t','t',0);
    """)
    pconn.commit()
    pconn.close()

    # Dry-run: 2 superestimados (1 determinístico + 1 ambíguo); irmã de e1 é
    # phantom_resolution_win; nada escrito.
    s = run(core_db_path=core, portfolio_db_path=port, apply=False)
    assert s["overstated"] == 2 and s["deterministic"] == 1, s
    assert s["ambiguous"] == 1, s
    assert s["phantom_siblings"] == 1 and s["phantom_resolution_wins"] == 1, s
    det = [p for p in s["plans"] if p["status"] == "deterministic"][0]
    assert det["entry_id"] == e1 and det["new_shares"] == 10.0, det
    assert abs(det["new_pnl"] - 6.0) < 1e-9, det          # (0.9-0.3)*10
    sib = det["siblings"][0]
    assert sib["entry_id"] == e2 and abs(sib["pnl"] - 2.5) < 1e-9, sib
    # conservação: 6.0 + 2.5 == 8.5 (pnl fundido original)
    assert abs(det["new_pnl"] + sib["pnl"] - det["old_pnl"]) < 1e-9
    with db.connect(core) as conn:
        n_cash = conn.execute("SELECT COUNT(*) FROM cashouts").fetchone()[0]
    assert n_cash == 2, n_cash                            # dry-run: sem writes
    print("Test 1 PASS: dry-run — determinístico + ambíguo detectados, "
          "conservação de P&L (6.0+2.5=8.5), nenhum write")

    # Stuck: posição N1 detectada (entries todas liquidadas), payout 1.0.
    assert s["stuck"] == 1, s
    st = s["stuck_positions"][0]
    assert st["token"] == "N1" and st["close_price"] == 1.0, st
    print("Test 2 PASS: posição órfã N1 detectada com payout 1.0")

    # Apply: cashout de e1 corrigido; e2 ganha historical_repair; token
    # ambíguo N2 intocado. (Fechamento da órfã testado à parte — depende do
    # paper_engine real; aqui portfolio_db é sintético e o close falha
    # graciosamente sem abortar o run.)
    s2 = run(core_db_path=core, portfolio_db_path=port, apply=True)
    with db.connect(core) as conn:
        c1 = conn.execute("SELECT * FROM cashouts WHERE entry_id=?",
                          (e1,)).fetchone()
        c2 = conn.execute("SELECT * FROM cashouts WHERE entry_id=?",
                          (e2,)).fetchone()
        c3 = conn.execute("SELECT * FROM cashouts WHERE entry_id=?",
                          (e3,)).fetchone()
    assert abs(c1["exit_shares"] - 10.0) < 1e-9, dict(c1)
    assert abs(c1["realized_pnl_usd"] - 6.0) < 1e-9, dict(c1)
    assert "repaired v13.4" in c1["reason"], c1["reason"]
    assert c2 is not None and abs(c2["realized_pnl_usd"] - 2.5) < 1e-9, c2
    assert c2["reason"] == "phantom_shared_close:historical_repair", c2["reason"]
    assert abs(c3["exit_shares"] - 12.0) < 1e-9, dict(c3)  # ambíguo intocado
    assert s2["applied"]["attribution"] == 1
    assert s2["applied"]["phantom_rows"] == 1
    print("Test 3 PASS: --apply corrige e1 (10 sh, pnl 6.0), insere "
          "historical_repair de e2 (pnl 2.5), ambíguo intocado")

    # Idempotência: segundo run não encontra mais nada determinístico.
    s3 = run(core_db_path=core, portfolio_db_path=port, apply=False)
    assert s3["deterministic"] == 0, s3
    assert s3["ambiguous"] == 1, s3                       # segue reportado
    print("Test 4 PASS: idempotente (re-run: 0 determinísticos, ambíguo "
          "segue report-only)")

    print("\nAll repair_ladder_cashouts tests PASS")


if __name__ == "__main__":
    if "--test-attribution" in sys.argv or "--test" in sys.argv:
        _test_attribution()
    else:
        main()
