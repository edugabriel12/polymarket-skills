#!/usr/bin/env python3
"""Apaga 5 previsões específicas do BD de futebol (predictions.db). Script pontual.

Seguro por padrão:
  - DRY-RUN (sem --apply): só lista o que casaria, não altera nada.
  - Backup .bak do BD antes de qualquer DELETE.
  - Só apaga com --apply.
Casa pelo texto exato do mercado (market_question), sem diferenciar maiúsc/minúsc.

Uso (a partir da raiz do repo, na máquina onde o brain roda):
    python polymarket-soccer-goals/scripts/delete_preds.py            # 1) confira: 5 linhas [OK]
    python polymarket-soccer-goals/scripts/delete_preds.py --apply    # 2) cria .bak e apaga

Se o seu brain usa um caminho de BD custom, aponte para ele antes de rodar:
    Linux/macOS:   export SOCCER_PREDICTIONS_DB=/caminho/predictions.db
    Windows (cmd): set SOCCER_PREDICTIONS_DB=C:\\caminho\\predictions.db
"""
import os
import shutil
import sqlite3
import sys
from pathlib import Path

DB = os.environ.get("SOCCER_PREDICTIONS_DB",
                    str(Path.home() / ".polymarket-soccer" / "predictions.db"))

TARGETS = [
    "Grêmio Novorizontino vs. Vila Nova FC: Both Teams to Score",
    "Egypt vs. IR Iran: Both Teams to Score",
    "Grêmio Novorizontino vs. Vila Nova FC: O/U 2.5",
    "Egypt vs. IR Iran: O/U 1.5",
    "Norway vs. France: Both Teams to Score",
]


def main() -> int:
    apply = "--apply" in sys.argv
    if not os.path.exists(DB):
        print(f"BD não encontrado: {DB}\n"
              f"Defina SOCCER_PREDICTIONS_DB com o caminho correto.", file=sys.stderr)
        return 1

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    print(f"BD: {DB}\n")
    ids: list[int] = []
    for q in TARGETS:
        rows = con.execute(
            "SELECT id, game_slug, market, line, side, status FROM predictions "
            "WHERE lower(trim(market_question)) = lower(trim(?))", (q,)).fetchall()
        flag = "OK" if len(rows) == 1 else (">> 0 casou" if not rows else f">> {len(rows)} casaram")
        print(f"[{flag}] {q}")
        for r in rows:
            print(f"        id={r['id']} {r['game_slug']} {r['market']} "
                  f"line={r['line']} {r['side']} {r['status']}")
            ids.append(r["id"])

    print(f"\nTotal a apagar: {len(ids)} (esperado: 5)")
    if not apply:
        print("DRY-RUN — nada foi alterado. Confira a lista e rode de novo com --apply.")
        return 0

    bak = DB + ".bak"
    shutil.copy2(DB, bak)
    con.executemany("DELETE FROM predictions WHERE id=?", [(i,) for i in ids])
    con.commit()
    restantes = con.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
    con.close()
    print(f"\nBackup salvo em: {bak}")
    print(f"Apagados: {len(ids)} | Restam no BD: {restantes}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
