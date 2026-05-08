# Polymarket Autonomous Agent

Loop em Python que roda o workflow do `CLAUDE.md` em ciclos periódicos, dirigido pelo Anthropic SDK. Por design, **só opera em paper mode** — a `polymarket-live-executor` não é exposta como ferramenta para o agente.

## Visão geral

```
┌───────────────────────┐
│ systemd user service  │
│  polymarket-agent     │
└──────────┬────────────┘
           │
           ▼
┌───────────────────────┐    a cada AGENT_INTERVAL segundos
│ agent/run.py          │ ──► chama Claude (Sonnet 4.6 default)
│ - tool: run_script    │     com SYSTEM_PROMPT cacheado
│ - tool: read_file     │
└──────────┬────────────┘
           │
           ▼
┌───────────────────────┐
│ scripts das 5 skills  │  scanner, analyzer, monitor,
│ (live-executor OFF)   │  paper-trader, strategy-advisor
└───────────────────────┘
```

A cada ciclo:
1. O agente recebe um kickoff (session-start ou daily review, dependendo da hora UTC).
2. Decide quais scripts rodar e com quais argumentos.
3. Executa via a ferramenta `run_script` (que valida path e roda `python script.py`).
4. Dorme até o próximo ciclo.

Logs vão para stdout → journald. Persistência fica no SQLite paper trader em `~/.polymarket-paper/portfolio.db`.

## Pré-requisitos

- Python 3.11+ com pacotes `anthropic`, `requests`, `py-clob-client`. Crie um venv se ainda não tem:
  ```bash
  python3 -m venv ~/.venv
  source ~/.venv/bin/activate
  pip install anthropic requests py-clob-client eth-account
  ```
- Repo `polymarket-skills` clonado.
- Uma chave da API Anthropic (`ANTHROPIC_API_KEY`).
- systemd com user services habilitado (`loginctl enable-linger $USER` se precisar que rode mesmo sem sessão logada).

## Setup

1. **Configurar variáveis de ambiente:**
   ```bash
   cd ~/polymarket-skills/agent
   cp .env.example .env
   chmod 600 .env
   $EDITOR .env   # preencha ANTHROPIC_API_KEY e POLYMARKET_SKILLS_ROOT
   ```

2. **Inicializar portfolio (se ainda não tem):**
   ```bash
   source ~/.venv/bin/activate
   python ~/polymarket-skills/polymarket-paper-trader/scripts/paper_engine.py \
     --action init --balance 1000
   ```

3. **Smoke test (rodar uma vez no foreground):**
   ```bash
   set -a; source .env; set +a
   python run.py
   # Ctrl-C depois de um ciclo completo
   ```
   Verifique no log: `[turn N] stop=end_turn ...`. Se ver erros de import, faça `pip install` faltante. Se o tool `run_script` falhar com "not found", confira `POLYMARKET_SKILLS_ROOT`.

4. **Instalar como systemd user service:**
   ```bash
   mkdir -p ~/.config/systemd/user
   cp polymarket-agent.service ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now polymarket-agent
   ```

5. **Acompanhar logs:**
   ```bash
   journalctl --user -u polymarket-agent -f
   ```

## Operação

| Ação | Comando |
|---|---|
| Status | `systemctl --user status polymarket-agent` |
| Parar | `systemctl --user stop polymarket-agent` |
| Reiniciar | `systemctl --user restart polymarket-agent` |
| Ver últimos logs | `journalctl --user -u polymarket-agent -n 200 --no-pager` |
| Stream ao vivo | `journalctl --user -u polymarket-agent -f` |
| Desabilitar | `systemctl --user disable --now polymarket-agent` |

## Schedule

- **Ciclos regulares:** a cada `AGENT_INTERVAL` segundos (default 900 = 15 min) executam o session-start do `CLAUDE.md` §3 (health check → scan → find edges → momentum → advisor → execução paper).
- **Daily review:** no ciclo cuja hora UTC == `DAILY_REVIEW_HOUR_UTC` (default 23), o agente roda `daily_review.py --days 1` em vez do session-start.
- Os ciclos são **stateless entre si** — toda persistência fica no SQLite. Se o serviço cair, basta voltar — nenhum estado interno é perdido.

## Custo

Estimativa grosseira para `AGENT_INTERVAL=900` (96 ciclos/dia):

| Modelo | Caching efetivo? | Custo aproximado/mês |
|---|---|---|
| `claude-sonnet-4-6` (**default**) | Sim (mínimo 2048 tokens) | $20–80 |
| `claude-haiku-4-5` | Sim | $5–20 |
| `claude-opus-4-7` | Não no prompt atual (~3K tokens, abaixo do mínimo de 4096 para Opus) | $400–900 |

Para reduzir mais: aumente `AGENT_INTERVAL` (1800 = 30 min, metade do custo), troque para Haiku 4.5 via `CLAUDE_MODEL=claude-haiku-4-5` no `.env`, ou diminua `AGENT_MAX_TURNS`.

## Limites e segurança

- **Live trading nunca é ativado autonomamente.** A skill `polymarket-live-executor` é deliberadamente excluída do `ALLOWED_SKILLS` em `run.py`. Mesmo se você adicionar, ela exige `POLYMARKET_CONFIRM=true` + confirmação humana interativa para cada trade — o que não funciona dentro do loop não-interativo. Para ir live, você roda `execute_live.py` manualmente.
- O service unit usa `ProtectHome=read-only` + `ReadWritePaths=%h/.polymarket-paper` — o agente só consegue gravar no diretório do paper trader. CLAUDE.md, skills, scripts permanecem read-only.
- `MemoryMax=512M` e `CPUQuota=50%` evitam que o serviço derrube a máquina.
- O `.env` tem `chmod 600` — não comite.
- Tool `run_script` valida `skill ∈ ALLOWED_SKILLS`, `script` termina em `.py` e não tem `..` ou `/`. Não há shell — chama `subprocess.run([python, path, *args])` direto.

## Tunning

| Cenário | Mude |
|---|---|
| Quer mais ciclos por hora | `AGENT_INTERVAL=300` (5 min) |
| Quer menos custo | `CLAUDE_MODEL=claude-sonnet-4-6` ou `=claude-haiku-4-5` |
| Agente está consumindo muitos turnos | Aperte o kickoff em `kickoff_for_now()` para ser mais específico |
| Quer ver raciocínio completo nos logs | `display: "summarized"` já está ativo; aumentar `effort` para `max` (Opus only) |
| Erros de timeout em scripts pesados | `SCRIPT_TIMEOUT=300` |

## Troubleshooting

- **`ANTHROPIC_API_KEY not set`**: o `EnvironmentFile=` do unit não foi lido. Confira que `.env` existe e tem a key sem espaços/aspas.
- **`CLAUDE.md not found`**: `POLYMARKET_SKILLS_ROOT` está errado.
- **Cada turno consome ~$0.50 e você esperava menos**: cache não está hitando. Confirme via log — `cache_read=` deve crescer entre turnos do mesmo ciclo. Se for 0 sempre, ou troque para Sonnet 4.6, ou ignore (custo aceitável para você?).
- **`Permission to … denied`** ao gravar no DB: ajuste `ReadWritePaths` no unit para o caminho real do seu portfolio.
- **Agente repetindo a mesma análise**: normal — cada ciclo é independente. Se quiser memória entre ciclos, escreva no DB ou em um arquivo dentro de `~/.polymarket-paper/`.

## Modo Live Autônomo (opt-in)

⚠️ **Dinheiro real. Sem confirmação humana por trade.** Leia `CLAUDE.md` §4.1 antes de ativar.

### Pré-requisitos

1. **Burner wallet criada e fundada.** Nunca use sua wallet principal.
   ```bash
   source ~/.venv/bin/activate
   python ~/polymarket-skills/polymarket-live-executor/scripts/setup_wallet.py --create
   # Salva endereço + chave (mostrados uma única vez)
   # Envia ~$100 USDC + ~0.05 MATIC pra o endereço, na rede Polygon
   POLYMARKET_PRIVATE_KEY=0x... python setup_wallet.py --check-balance
   ```

2. **Critérios de live-readiness atendidos** (CLAUDE.md §4): 20+ paper trades fechados, win rate >55%, Sharpe >0.5, drawdown <15%. O agente bloqueia live automaticamente se qualquer um falhar.
   ```bash
   python ~/polymarket-skills/polymarket-strategy-advisor/scripts/backtest.py --live-check
   # verdict deve ser READY
   ```

3. **Bot Telegram** pra alertas:
   - Falar com [@BotFather](https://t.me/botfather), `/newbot`, salvar token
   - Mandar `/start` pro bot
   - `curl https://api.telegram.org/bot<TOKEN>/getUpdates` → copiar `chat.id`

### Ativação

Edite `.env`:
```bash
POLYMARKET_AUTO_CONFIRM=true
POLYMARKET_PRIVATE_KEY=0x...        # burner wallet
POLYMARKET_CONFIRM=true
POLYMARKET_MAX_SIZE=5
POLYMARKET_DAILY_LOSS_LIMIT=50
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

Restart:
```bash
systemctl --user restart polymarket-agent
journalctl --user -u polymarket-agent -f
```

No log você deve ver:
```
[startup] live_enabled=True ...
[startup] LIVE mode is ENABLED. Per-cycle gates: ...
[mode] LIVE_ENABLED=true ready=True reason='4/4 criteria'
```

Se aparecer `ready=False`, o agente segue em paper até as métricas passarem.

### Killswitch

Pra parar live trading **imediatamente** (próximo ciclo, ≤15 min):
```bash
touch ~/halt-trading
```

Pra retomar:
```bash
rm ~/halt-trading
```

O killswitch é checado:
- No início de cada ciclo (skip do ciclo inteiro)
- Antes de cada chamada à `execute_live.py` (refuse no meio do ciclo)

Pra parada total imediata: `systemctl --user stop polymarket-agent`.

### Monitoramento

- **Telegram:** uma mensagem por trade (sucesso ou falha) com argumentos e stdout.
- **Trade log:** `~/.polymarket-live/trades.log` (JSON, uma linha por evento).
- **Posições on-chain:** `python check_positions.py --balance --orders --trades`.
- **journalctl:** `journalctl --user -u polymarket-agent -f | grep -E '\[live\]|\[mode\]'`.

### Camadas de segurança que permanecem em modo autônomo

| Camada | Ativa em live autônomo? |
|---|---|
| `POLYMARKET_CONFIRM=true` env gate | Sim |
| `POLYMARKET_MAX_SIZE` hard cap por trade | Sim ($5) |
| `POLYMARKET_DAILY_LOSS_LIMIT` | Sim ($50) |
| Drawdown graduado 10/15/20% (CLAUDE.md §2) | Sim — fecha tudo aos $20 perdidos |
| Live-readiness gate (CLAUDE.md §4) | Sim — checado todo ciclo |
| Killswitch `~/halt-trading` | Sim — checado pré-ciclo + pré-trade |
| Telegram alert por trade | Sim |
| systemd `ProtectHome=read-only` | Sim |
| Burner wallet (não main) | Disciplina sua |

### Reverter pra paper-only

Edite `.env`, deixe `POLYMARKET_AUTO_CONFIRM=` (vazio), restart. Nenhuma mudança de código necessária.

### Riscos aceitos

Lendo `CLAUDE.md` §4.1 e ativando este modo, você está conscientemente aceitando:
1. Bug no agente → ordem errada (capped a $5).
2. Prompt injection via market data → trade indevido (capped + alertado).
3. Caps + drawdown podem ainda assim resultar em perda total dos $100.
4. Slippage/market impact reais não capturados em paper.
5. Sem confirmação por trade — regra #4 do CLAUDE.md fica suspensa enquanto este modo está ativo.

---

## Limitações

- Sem replay/resume — se um ciclo crash no meio de uma execução paper, pode ficar half-done. O `paper_engine.py` é transacional, então o DB fica consistente, mas a recomendação foi perdida.
- Não substitui supervisão humana. Revise `journalctl` pelo menos uma vez ao dia, e o `daily_review.py` semanalmente.
- Modelo de custo é estimativa — meça os primeiros dias com `journalctl ... | grep "in="` e calcule real.
