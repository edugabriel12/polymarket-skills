#!/usr/bin/env bash
#
# bootstrap.sh — provisiona um VPS/nuvem headless (Debian/Ubuntu) para rodar o
# stack weather-edge (bot + judge + advisor) 24/7 como systemd USER services.
#
# Idempotente: seguro re-rodar. Não inicia os serviços — apenas prepara tudo e
# imprime os próximos passos (você precisa preencher o .env com as API keys
# antes de habilitar). PAPER trading por padrão.
#
# Uso (como o usuário NÃO-root que vai ser dono dos serviços):
#     bash agent/deploy/bootstrap.sh
#
# Variáveis opcionais:
#     VENV_DIR=~/.venv         diretório do virtualenv
#     WITH_DASHBOARD=1         também instala as deps do dashboard
#     REPO_LINK=~/polymarket-skills   caminho canônico esperado pelas units
#
set -euo pipefail

# --------------------------------------------------------------------------
# Resolve caminhos a partir da localização deste script (funciona onde clonar)
# --------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"        # .../agent/deploy -> raiz
VENV_DIR="${VENV_DIR:-${HOME}/.venv}"
REPO_LINK="${REPO_LINK:-${HOME}/polymarket-skills}"
UNIT_DIR="${HOME}/.config/systemd/user"
PAPER_DIR="${HOME}/.polymarket-paper"
WITH_DASHBOARD="${WITH_DASHBOARD:-0}"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------
# 0. Preflight
# --------------------------------------------------------------------------
[ "$(id -u)" -eq 0 ] && die "Rode como o usuário NÃO-root que será dono dos serviços (user services). Não use sudo/root."
command -v apt-get >/dev/null 2>&1 || die "Este bootstrap assume Debian/Ubuntu (apt). Em outra distro, faça o setup manual seguindo DEPLOY.md."
command -v systemctl >/dev/null 2>&1 || die "systemd não encontrado — necessário para os serviços."
SUDO=""
if command -v sudo >/dev/null 2>&1; then SUDO="sudo"; else
    warn "sudo ausente — os passos de apt/timezone/linger serão pulados; rode-os como root manualmente (veja DEPLOY.md)."
fi

say "Repo:        ${REPO_DIR}"
say "Venv:        ${VENV_DIR}"
say "Units em:    ${UNIT_DIR}"

# --------------------------------------------------------------------------
# 1. Pacotes de sistema + relógio em UTC (crítico: ciclos de forecast são UTC)
# --------------------------------------------------------------------------
if [ -n "$SUDO" ]; then
    say "Instalando pacotes de sistema (python3, venv, pip, git)..."
    $SUDO apt-get update -qq
    $SUDO apt-get install -y -qq python3 python3-venv python3-pip git ca-certificates
    say "Forçando timezone UTC e sincronização de relógio (systemd-timesyncd)..."
    $SUDO timedatectl set-timezone UTC || warn "timedatectl set-timezone falhou (container?) — garanta UTC manualmente."
    $SUDO timedatectl set-ntp true 2>/dev/null || true
else
    warn "Pulando apt/timezone (sem sudo)."
fi

# --------------------------------------------------------------------------
# 2. Linger — permite que os USER services rodem SEM sessão logada (headless!)
# --------------------------------------------------------------------------
if [ -n "$SUDO" ]; then
    if loginctl show-user "$USER" 2>/dev/null | grep -q '^Linger=yes'; then
        say "Linger já habilitado para ${USER}."
    else
        say "Habilitando linger (serviços sobem no boot, sem login)..."
        $SUDO loginctl enable-linger "$USER"
    fi
else
    warn "Sem sudo: rode 'sudo loginctl enable-linger ${USER}' — SEM isso os serviços param quando você desloga."
fi

# --------------------------------------------------------------------------
# 3. Caminho canônico ~/polymarket-skills (as units usam %h/polymarket-skills)
# --------------------------------------------------------------------------
if [ "$REPO_DIR" != "$REPO_LINK" ]; then
    if [ -e "$REPO_LINK" ] && [ ! -L "$REPO_LINK" ]; then
        warn "${REPO_LINK} já existe e não é symlink — as units esperam o repo aí. Verifique manualmente."
    else
        say "Apontando ${REPO_LINK} -> ${REPO_DIR} (symlink)."
        ln -sfn "$REPO_DIR" "$REPO_LINK"
    fi
fi

# --------------------------------------------------------------------------
# 4. Virtualenv + dependências Python
# --------------------------------------------------------------------------
if [ ! -d "$VENV_DIR" ]; then
    say "Criando virtualenv em ${VENV_DIR}..."
    python3 -m venv "$VENV_DIR"
fi
say "Instalando dependências Python no venv..."
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet anthropic requests py-clob-client eth-account
if [ "$WITH_DASHBOARD" = "1" ]; then
    say "Instalando dependências do dashboard..."
    "${VENV_DIR}/bin/pip" install --quiet -r "${REPO_DIR}/dashboard/requirements.txt"
fi

# --------------------------------------------------------------------------
# 5. .env (a partir do template) — chmod 600, nunca comitar
# --------------------------------------------------------------------------
ENV_FILE="${REPO_DIR}/agent/.env"
if [ ! -f "$ENV_FILE" ]; then
    say "Criando ${ENV_FILE} a partir do template..."
    cp "${REPO_DIR}/agent/.env.example" "$ENV_FILE"
    # Preenche o path do repo automaticamente para conveniência.
    sed -i "s#^POLYMARKET_SKILLS_ROOT=.*#POLYMARKET_SKILLS_ROOT=${REPO_LINK}#" "$ENV_FILE" || true
    chmod 600 "$ENV_FILE"
    NEED_ENV=1
else
    say ".env já existe — mantido."
    chmod 600 "$ENV_FILE"
    NEED_ENV=0
fi

# --------------------------------------------------------------------------
# 6. Portfolio paper (inicializa se não existir)
# --------------------------------------------------------------------------
if [ ! -f "${PAPER_DIR}/portfolio.db" ]; then
    say "Inicializando portfolio paper (\$1000)..."
    "${VENV_DIR}/bin/python" "${REPO_DIR}/polymarket-paper-trader/scripts/paper_engine.py" \
        --action init --balance 1000
else
    say "Portfolio paper já existe — mantido."
fi

# --------------------------------------------------------------------------
# 7. Instala as units apontando para o PYTHON DO VENV (as originais usam o
#    python do sistema, que não tem as libs num VPS limpo)
# --------------------------------------------------------------------------
mkdir -p "$UNIT_DIR"
install_unit() {
    local src="$1" dst="${UNIT_DIR}/$(basename "$1")"
    # Substitui '/usr/bin/env python3' pelo python do venv (%h expande p/ home).
    sed 's#/usr/bin/env python3#%h/.venv/bin/python#' "$src" > "$dst"
    say "  instalado $(basename "$dst")"
}
say "Instalando unidades systemd (venv python)..."
install_unit "${REPO_DIR}/agent/weather-edge-bot.service"
install_unit "${REPO_DIR}/agent/weather-edge-judge.service"
install_unit "${REPO_DIR}/agent/weather-strategy-advisor.service"
cp "${REPO_DIR}/agent/weather-strategy-advisor.timer" "${UNIT_DIR}/"
systemctl --user daemon-reload

# --------------------------------------------------------------------------
# 8. Próximos passos
# --------------------------------------------------------------------------
echo
say "Bootstrap concluído."
echo
if [ "${NEED_ENV}" = "1" ]; then
    warn "AÇÃO NECESSÁRIA: preencha as API keys em ${ENV_FILE}"
    echo "     OPENWEATHER_API_KEY, VISUAL_CROSSING_API_KEY, NWS_USER_AGENT, ANTHROPIC_API_KEY"
    echo
fi
cat <<EOF
Próximos passos:
  1) Edite o .env:            \$EDITOR ${ENV_FILE}
  2) Smoke test (offline):    ${VENV_DIR}/bin/python ${REPO_LINK}/polymarket-analyzer/scripts/weather_edge_bot.py --once --dry-run --judge-mode=off --debug
  3) Habilite os serviços:    systemctl --user enable --now weather-edge-bot weather-edge-judge
  4) Advisor semanal (opc.):  systemctl --user enable --now weather-strategy-advisor.timer
  5) Acompanhe:               journalctl --user -u weather-edge-bot -u weather-edge-judge -f

Estratégia cheap_convexity (nova, gated):
  - Rode o gate de calibração: ${VENV_DIR}/bin/python ${REPO_LINK}/polymarket-analyzer/scripts/cheap_convexity_calibration.py --write
  - Enquanto o gate não passar, --cheap-convexity é no-op (fail-closed).

Detalhes, segurança e manutenção: ${REPO_LINK}/agent/deploy/DEPLOY.md
EOF
