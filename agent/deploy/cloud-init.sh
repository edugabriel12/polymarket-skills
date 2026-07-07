#!/bin/bash
#
# cloud-init.sh — script de "User data" / "Startup script" para provisionar uma
# VPS nova (DigitalOcean, Hetzner, Vultr, Lightsail, ...) no PRIMEIRO BOOT.
#
# Roda UMA vez, como ROOT, antes de você logar. Faz só o provisionamento de
# nível-sistema que exige root e que o bootstrap.sh (rodado depois como o
# usuário `polymarket`) NÃO faz — em especial CRIAR o usuário `polymarket` com
# acesso SSH, já que sem ele você só consegue logar como root e o bootstrap.sh
# se recusa a rodar como root.
#
# NÃO clona o repositório: assuma repo privado (deploy key), então o clone é
# manual depois do boot (ver DEPLOY.md §3b). Se o seu repo for público, você
# pode acrescentar um `git clone https://...` ao final.
#
# Idempotente: seguro re-rodar. Requer Debian/Ubuntu (apt). PAPER trading.
#
# Como usar:
#   - DigitalOcean: Create Droplet -> Advanced options -> Add Initial Scripts
#     (User data) -> cole este arquivo inteiro.
#   - Outros providers: campo equivalente de "user-data" / "cloud-init" /
#     "startup script".
#   Depois do boot (~1-2 min): `ssh polymarket@SEU_IP` e siga o DEPLOY.md a
#   partir de §3b (deploy key) / §4 (bootstrap).
#
set -eux

# Log tudo em /var/log/startup-script.log para depurar se algo falhar no boot
# (útil: `cat /var/log/startup-script.log` logando como root).
exec > /var/log/startup-script.log 2>&1

DEPLOY_USER="polymarket"

# --------------------------------------------------------------------------
# 1. Swap 2GB — margem de segurança mesmo no droplet de 1GB (bot+judge têm
#    MemoryMax=256M cada e o `pip install` do bootstrap encosta em várias
#    centenas de MB). Barato em disco; protege contra OOM em picos.
# --------------------------------------------------------------------------
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# --------------------------------------------------------------------------
# 2. Pacotes base + timezone UTC (os ciclos de forecast são 00/06/12/18 UTC)
# --------------------------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip git ca-certificates ufw unattended-upgrades
timedatectl set-timezone UTC

# --------------------------------------------------------------------------
# 3. Firewall — só SSH de entrada. O bot só faz conexões de SAÍDA; o dashboard
#    nunca deve ficar exposto (acesse por túnel SSH, ver DEPLOY.md §8).
# --------------------------------------------------------------------------
ufw allow OpenSSH
ufw --force enable

# --------------------------------------------------------------------------
# 4. Usuário não-root dedicado + herda a chave SSH registrada na droplet.
#    Os serviços são systemd USER services e rodam como este usuário.
# --------------------------------------------------------------------------
if ! id "$DEPLOY_USER" >/dev/null 2>&1; then
  adduser --disabled-password --gecos "" "$DEPLOY_USER"
fi
# sudo sem senha (login é por chave SSH; sem senha para digitar).
echo "$DEPLOY_USER ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/90-"$DEPLOY_USER"
chmod 440 /etc/sudoers.d/90-"$DEPLOY_USER"
# Copia authorized_keys do root para permitir `ssh polymarket@IP` com a MESMA
# chave que você registrou ao criar a droplet.
install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" /home/"$DEPLOY_USER"/.ssh
if [ -f /root/.ssh/authorized_keys ]; then
  cp /root/.ssh/authorized_keys /home/"$DEPLOY_USER"/.ssh/authorized_keys
  chown "$DEPLOY_USER":"$DEPLOY_USER" /home/"$DEPLOY_USER"/.ssh/authorized_keys
  chmod 600 /home/"$DEPLOY_USER"/.ssh/authorized_keys
fi

# --------------------------------------------------------------------------
# 5. Linger — permite que os USER services subam no boot e sobrevivam ao
#    logout do SSH (sem isso, param quando você desloga).
# --------------------------------------------------------------------------
loginctl enable-linger "$DEPLOY_USER"

# --------------------------------------------------------------------------
# Feito. Próximo passo é MANUAL (repo privado): conecte como o usuário
# dedicado e siga o runbook.
#   ssh polymarket@SEU_IP
#   # DEPLOY.md §3b: gerar deploy key, colar no GitHub, git clone git@...
#   # DEPLOY.md §4 : bash ~/polymarket-skills/agent/deploy/bootstrap.sh
# --------------------------------------------------------------------------
echo "cloud-init.sh concluído — conecte como '$DEPLOY_USER' e siga DEPLOY.md §3b/§4."
