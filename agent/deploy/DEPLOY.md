# Deploy do weather-edge stack em VPS / nuvem (24/7)

Runbook para rodar o **weather edge bot + judge + advisor** numa máquina
sempre-ligada (VPS/nuvem), em vez do desktop — que ao **suspender congela
todos os processos** e faz o bot perder monitoramento de posições, cashouts,
resoluções e ciclos de forecast.

Tudo aqui é **paper trading**. Nada opera dinheiro real a menos que você ative
explicitamente o modo live (ver `agent/README.md` §Modo Live Autônomo).

---

## 1. Por que um VPS

O bot é um daemon systemd de loop de 60s que precisa rodar continuamente para:

- **monitorar posições abertas** e disparar cashout quando o `best_bid` atinge
  o alvo (a estratégia `cheap_convexity` depende disso);
- **varrer resoluções** de mercados que fecharam;
- **descobrir** mercados a cada ciclo de forecast (00Z/06Z/12Z/18Z **UTC**).

Suspender/hibernar o host = tudo congela. Um VPS pequeno resolve por ~US$5/mês.

**Dimensionamento:** cada serviço tem `MemoryMax=256M` + `CPUQuota=25%`. Um VPS
de **1 vCPU / 1 GB** roda bot+judge+advisor com folga. Escolha **2 GB** se
também quiser o dashboard na mesma máquina. Recomendado: **Ubuntu 24.04 LTS**
ou Debian 12.

---

## 2. Provisionar a máquina (uma vez)

1. Crie a VPS (Hetzner/DigitalOcean/Vultr/Lightsail/etc), Ubuntu 24.04, chave SSH.
2. Entre e crie um usuário não-root dedicado (os serviços são **user services**):
   ```bash
   ssh root@SEU_IP
   adduser --disabled-password --gecos "" polymarket
   usermod -aG sudo polymarket
   install -d -m 700 -o polymarket -g polymarket /home/polymarket/.ssh
   cp ~/.ssh/authorized_keys /home/polymarket/.ssh/ && chown polymarket:polymarket /home/polymarket/.ssh/authorized_keys
   ```
3. Firewall — **só SSH** de entrada (o bot só faz conexões de saída; o dashboard
   nunca deve ficar exposto):
   ```bash
   ufw allow OpenSSH && ufw --force enable
   ```
4. Atualizações de segurança automáticas:
   ```bash
   apt-get update && apt-get install -y unattended-upgrades
   dpkg-reconfigure -plow unattended-upgrades   # (ou aceite os defaults)
   ```
5. Saia do root e reconecte como o usuário dedicado:
   ```bash
   exit
   ssh polymarket@SEU_IP
   ```

---

## 3. Clonar + bootstrap

Como o usuário `polymarket`:

```bash
git clone https://github.com/edugabriel12/polymarket-skills.git ~/polymarket-skills
bash ~/polymarket-skills/agent/deploy/bootstrap.sh
# (com dashboard na mesma máquina: WITH_DASHBOARD=1 bash .../bootstrap.sh)
```

O `bootstrap.sh` é **idempotente** e faz:

- instala `python3/venv/pip/git` via apt;
- força **timezone UTC** + NTP (ciclos de forecast são UTC);
- habilita **linger** (`loginctl enable-linger`) — sem isso os user services
  **param quando você desloga do SSH**;
- cria o venv `~/.venv` e instala `anthropic requests py-clob-client eth-account`;
- cria `agent/.env` (chmod 600) a partir do template e preenche `POLYMARKET_SKILLS_ROOT`;
- inicializa o portfolio paper (`$1000`) se não existir;
- instala as unidades systemd **apontando para o python do venv** (as originais
  usam o python do sistema, que num VPS limpo não tem as libs → falha silenciosa);
- **não inicia** nada — você preenche as keys primeiro.

---

## 4. Configurar as API keys

```bash
chmod 600 ~/polymarket-skills/agent/.env
nano ~/polymarket-skills/agent/.env
```

Mínimo para o weather stack (todas free tier):

| Variável | Para quê | Onde |
|---|---|---|
| `OPENWEATHER_API_KEY` | forecast base | openweathermap.org (1000/dia grátis) |
| `VISUAL_CROSSING_API_KEY` | 2ª fonte (consenso) | visualcrossing.com (1000/dia grátis) |
| `NWS_USER_AGENT` | judge (NWS) | string `"polymarket-weather seu@email"` |
| `ANTHROPIC_API_KEY` | judge (Claude) | console.anthropic.com |

Opcionais úteis: `JUDGE_DAILY_BUDGET_USD` (cap de gasto do judge, default $15),
`ADVISOR_WEEKLY_BUDGET_USD`. **Deixe as variáveis de live vazias** — paper é o default.

---

## 5. Smoke test (offline, sem custo)

```bash
~/.venv/bin/python ~/polymarket-skills/polymarket-analyzer/scripts/weather_edge_bot.py \
  --once --dry-run --judge-mode=off --debug
```

Deve listar mercados weather, edges calculadas e decisões (skip/propose) **sem**
chamar API paga nem executar nada. Se der erro de import, algo faltou no venv;
se der erro de forecast, confira `OPENWEATHER_API_KEY`.

---

## 6. Ligar os serviços

```bash
systemctl --user enable --now weather-edge-bot weather-edge-judge
systemctl --user enable --now weather-strategy-advisor.timer   # advisor semanal (opcional)

# verificar
systemctl --user status weather-edge-bot --no-pager
systemctl --user list-timers | grep advisor
journalctl --user -u weather-edge-bot -u weather-edge-judge -f
```

Prova de que sobrevive a reboot/logout: reinicie a VPS (`sudo reboot`),
reconecte e confirme `systemctl --user status weather-edge-bot` = `active`
**sem** você ter feito nada (graças ao linger).

---

## 7. Estratégia cheap_convexity (nova — gated)

O caminho `cheap_convexity` (comprar bins baratos 1-20¢, sair no cashout) é
**fail-closed**: no-op até o gate de calibração de cauda passar.

```bash
# gera/atualiza ~/.polymarket-paper/cheap_convexity_gate.json
~/.venv/bin/python ~/polymarket-skills/polymarket-analyzer/scripts/cheap_convexity_calibration.py --write
```

Enquanto `tail_calibration_pass=false` (ex.: dados insuficientes), `--cheap-convexity`
não propõe nada. Quando quiser ativar (após o gate passar), adicione a flag ao
`ExecStart` do bot (`--cheap-convexity`) editando a unit instalada em
`~/.config/systemd/user/weather-edge-bot.service` e `systemctl --user daemon-reload
&& restart`. Rode o gate periodicamente (o advisor semanal é um bom gatilho para
reavaliar). Detalhes da estratégia: PR/commits `cheap_convexity Fase 1/2`.

---

## 8. Dashboard (opcional, via túnel SSH — nunca exposto)

O dashboard é **read-only** mas não deve ficar público. Rode-o ligado a
`127.0.0.1` e acesse pelo seu laptop via túnel:

```bash
# na VPS (bootstrap com WITH_DASHBOARD=1, ou pip install -r dashboard/requirements.txt)
~/.venv/bin/uvicorn dashboard.main:app --host 127.0.0.1 --port 8765
```
```bash
# no seu laptop
ssh -N -L 8765:127.0.0.1:8765 polymarket@SEU_IP
# abra http://127.0.0.1:8765
```

Para deixá-lo sempre no ar, use a unit pronta (o `bootstrap.sh` já a instala em
`~/.config/systemd/user/weather-dashboard.service`, ligada a `127.0.0.1:8765`):
```bash
systemctl --user enable --now weather-dashboard
```
Requer as deps do dashboard no venv (`WITH_DASHBOARD=1 bash bootstrap.sh`, ou
`~/.venv/bin/pip install -r ~/polymarket-skills/dashboard/requirements.txt`).

---

## 9. Operação e manutenção

| Ação | Comando |
|---|---|
| Status | `systemctl --user status weather-edge-bot` |
| Logs ao vivo | `journalctl --user -u weather-edge-bot -u weather-edge-judge -f` |
| Parar tudo (killswitch) | `systemctl --user stop weather-edge-bot weather-edge-judge` |
| Atualizar código | `cd ~/polymarket-skills && git pull && systemctl --user restart weather-edge-bot weather-edge-judge` |
| Zerar gasto do judge | `echo "JUDGE_DAILY_BUDGET_USD=0" >> ~/polymarket-skills/agent/.env && systemctl --user restart weather-edge-judge` |
| Análise | `~/.venv/bin/python ~/polymarket-skills/polymarket-analyzer/scripts/weather_edge_analyzer.py --since 2026-07-01` |

### Backup automático (recomendado — a VPS pode morrer)

Todo o estado é SQLite em `~/.polymarket-paper/`. Como os `.db` estão em modo
WAL sendo escritos ao vivo, um `tar` cru pode sair inconsistente — por isso o
`agent/deploy/backup.sh` usa a **API de backup online do SQLite** (snapshot
consistente sem parar os serviços), inclui os `.jsonl`/`advisor_reports`/gate, e
rotaciona mantendo os últimos `KEEP` (default 8) em `~/polymarket-backups/`.

O `bootstrap.sh` instala o timer semanal (domingo 05:00 UTC, `Persistent=true` —
roda no boot se a VPS estava desligada). Habilite:
```bash
systemctl --user enable --now weather-edge-backup.timer
systemctl --user start weather-edge-backup.service   # roda um agora, p/ testar
systemctl --user list-timers | grep backup
```
Rodada manual: `bash ~/polymarket-skills/agent/deploy/backup.sh`
(ajuste retenção com `KEEP=12 bash ...`).

**Leve os snapshots para fora da VPS** — um backup local não protege contra a
perda da máquina:
```bash
# do seu laptop, periodicamente:
scp polymarket@SEU_IP:~/polymarket-backups/polymarket-paper-*.tgz ./backups/
```
Restaurar: `tar xzf polymarket-paper-<UTC>.tgz -C ~/.polymarket-paper` com os
serviços parados.

**Custo estimado:** VPS ~US$5/mês + Anthropic ~US$15-50/mês (judge, Sonnet +
cache) + OpenWeather/Visual Crossing no free tier.

---

## 10. Migração v11 (cheap_convexity) — nota de ordem

Se você já roda uma versão anterior e está atualizando (`git pull`), a migration
v11 (coluna `strategy`) roda automática no próximo `init_db`. É idempotente e
tolerante, mas o backfill segura o writer lock por um instante — **prefira parar
o bot/judge antes do primeiro start pós-update**:
```bash
systemctl --user stop weather-edge-bot weather-edge-judge
cd ~/polymarket-skills && git pull
~/.venv/bin/python -c "import sys; sys.path.insert(0,'polymarket-analyzer/scripts'); import weather_edge_db as d; d.init_db()"
systemctl --user start weather-edge-bot weather-edge-judge
```

---

## 11. Checklist final

- [ ] VPS Ubuntu 24.04, usuário não-root, `ufw allow OpenSSH`, unattended-upgrades
- [ ] `git clone` + `bootstrap.sh` sem erros
- [ ] **linger habilitado** (`loginctl show-user $USER | grep Linger` → `yes`)
- [ ] **timezone UTC** (`timedatectl` → `Time zone: UTC`)
- [ ] `.env` preenchido (chmod 600), variáveis de live **vazias**
- [ ] smoke test `--once --dry-run` OK
- [ ] serviços `active`; sobrevivem a `reboot`
- [ ] `weather-edge-backup.timer` habilitado; snapshots copiados para fora da VPS
