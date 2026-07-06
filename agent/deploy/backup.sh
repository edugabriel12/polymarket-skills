#!/usr/bin/env bash
#
# backup.sh — snapshot consistente do estado do weather stack (~/.polymarket-paper).
#
# Os .db são SQLite em modo WAL sendo escritos ao vivo pelos daemons, então um
# `cp`/`tar` cru pode sair torto (WAL não commitado / leitura parcial). Este
# script usa a API de BACKUP ONLINE do SQLite (sqlite3.backup) para tirar um
# snapshot consistente sem parar os serviços. Arquivos append-only (.jsonl,
# advisor_reports) são copiados direto.
#
# Gera ~/polymarket-backups/polymarket-paper-<UTC>.tgz e mantém os últimos N.
# Idempotente e seguro para rodar sob systemd timer (oneshot).
#
# Uso:  bash agent/deploy/backup.sh
# Env:  SRC_DIR, BACKUP_DIR, KEEP (default 8), PYTHON
#
set -euo pipefail

SRC_DIR="${SRC_DIR:-${HOME}/.polymarket-paper}"
BACKUP_DIR="${BACKUP_DIR:-${HOME}/polymarket-backups}"
KEEP="${KEEP:-8}"
PYTHON="${PYTHON:-${HOME}/.venv/bin/python}"
[ -x "$PYTHON" ] || PYTHON="python3"

say() { printf '\033[1;36m[backup]\033[0m %s\n' "$*"; }

[ -d "$SRC_DIR" ] || { echo "[backup] nada a fazer: ${SRC_DIR} não existe" >&2; exit 0; }
mkdir -p "$BACKUP_DIR"

# Timestamp UTC sem depender de flags GNU específicas.
STAMP="$("$PYTHON" -c 'import datetime; print(datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))')"
STAGE="$(mktemp -d "${BACKUP_DIR}/.stage-XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT

# 1. Snapshot consistente de cada .db via API de backup online do SQLite.
shopt -s nullglob
for dbfile in "$SRC_DIR"/*.db; do
    base="$(basename "$dbfile")"
    say "snapshot ${base}"
    "$PYTHON" - "$dbfile" "${STAGE}/${base}" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
d = sqlite3.connect(dst)
with d:
    s.backup(d)          # consistent online snapshot, safe on live WAL DBs
s.close(); d.close()
PY
done

# 2. Copia os append-only (uma linha parcial no fim é aceitável num backup).
for f in "$SRC_DIR"/*.jsonl; do cp -p "$f" "$STAGE/" 2>/dev/null || true; done
if [ -d "${SRC_DIR}/advisor_reports" ]; then
    cp -rp "${SRC_DIR}/advisor_reports" "$STAGE/" 2>/dev/null || true
fi
# Inclui o gate da cheap_convexity, se existir.
[ -f "${SRC_DIR}/cheap_convexity_gate.json" ] && cp -p "${SRC_DIR}/cheap_convexity_gate.json" "$STAGE/" || true

# 3. Empacota.
OUT="${BACKUP_DIR}/polymarket-paper-${STAMP}.tgz"
tar czf "$OUT" -C "$STAGE" .
say "criado ${OUT} ($(du -h "$OUT" | cut -f1))"

# 4. Rotaciona: mantém os KEEP mais recentes.
mapfile -t all < <(ls -1t "${BACKUP_DIR}"/polymarket-paper-*.tgz 2>/dev/null || true)
if [ "${#all[@]}" -gt "$KEEP" ]; then
    for old in "${all[@]:$KEEP}"; do
        say "removendo antigo $(basename "$old")"
        rm -f "$old"
    done
fi
say "ok (mantendo até ${KEEP} snapshots em ${BACKUP_DIR})"
