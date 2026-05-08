# Polymarket Skills — Documentação Completa do Repositório

Este documento descreve, arquivo por arquivo, todo o conteúdo do repositório `polymarket-skills`. O projeto é um conjunto de **Agent Skills** componíveis para trading sistemático na Polymarket (mercados de previsão), com foco em paper trading primeiro e execução real apenas com confirmação humana explícita.

---

## Visão Geral da Arquitetura

```
Scanner ──→ Analyzer ──→ Strategy Advisor ──→ Paper Trader ──→ Live Executor
(find)      (evaluate)    (recommend)          (simulate)       (execute)
```

Seis skills compõem o pipeline completo: descoberta de mercado → detecção de edge → recomendação → simulação → execução real. Todas as APIs usadas são `gamma-api.polymarket.com` (metadata, sem auth) e `clob.polymarket.com` (preços/orderbook sem auth, trading com L2 auth via wallet).

---

## Arquivos Raiz

### `README.md`
Documento principal do projeto: tabela de skills, instruções de instalação (`npx skills add ...`), exemplos de fluxo completo de 10 passos, mapa de arquitetura, descrição de cada script com argumentos-chave, resumo da constitution `CLAUDE.md`, regras de risco, requisitos para ir live, tiers de experiência ($25 → $2.000+) e disclaimers.

### `CLAUDE.md`
**Constitution do agente** — autoritativa sobre todo o resto do repositório. Contém:
- 9 regras inegociáveis (edge obrigatório, paper como default, risk limits são lei, confirmação humana para trade real, dados de mercado são untrusted, etc.).
- Limites de risco quantificados (single source of truth): 10% por trade, 5 posições simultâneas, 5% de daily loss, 10% weekly loss, drawdown graduado em 10/15/20%.
- Workflow diário em 14 passos (session start → scan → eval → execute → review).
- Pré-requisitos para live trading (20+ paper trades, win rate >55%, Sharpe >0.5, drawdown <15%).
- Mapa de skills e variáveis de ambiente necessárias para live.

### `SECURITY-AUDIT.md`
Relatório formal de auditoria de segurança (688 linhas, 14 findings: 2 High, 3 Medium, 9 Low). Cobre: prompt injection via texto de mercado (SEC-01), `sys.path.insert(0)` com risco de code injection (SEC-02), timeouts/retries faltantes (SEC-03), concorrência SQLite (SEC-04), fórmulas de daily loss imprecisas (STR-01), fee calculation simplificada (STR-02) e vários itens de spec compliance e integração.

### `.gitignore`
Ignora `__pycache__/`, `*.pyc`, dados locais (`.polymarket-paper/`, `.polymarket-live/`), arquivos `.env*` (mantendo apenas `.env.example`) e `*.key`.

### `.well-known/skills/index.json`
**Manifesto de skills** seguindo a especificação Agent Skills. Lista as 6 skills com `name`, `description` (gatilhos em linguagem natural) e a lista exata de arquivos que pertencem a cada skill — usado por agentes/instaladores para descoberta automática.

---

## Skill 1 — `polymarket-scanner/` (Descoberta de Mercados, Read-Only)

Browse, search e exploração de mercados ativos. Sem auth, risco zero.

### `polymarket-scanner/SKILL.md`
Front-matter YAML com `name`, `description` (triggers: "browse markets", "scan markets", "market data", etc.), `version` e `author`. Documenta os três scripts, exemplos `bash`, e referências cruzadas para `references/api-guide.md` e `references/market-types.md`. Avisa que dados de mercado são UGC e devem ser tratados como untrusted.

### `polymarket-scanner/scripts/scan_markets.py`
Busca mercados ativos via Gamma API. Suporta filtros por categoria (`tag_slug`), busca textual em `question`, volume mínimo, e ordenação por `volume24hr`/`liquidity`/`endDate`. Inclui `sanitize_text()` que remove caracteres de controle e limita comprimento (defesa contra prompt injection). Saída JSON estruturada com token IDs CLOB necessários para os outros scripts.

### `polymarket-scanner/scripts/get_orderbook.py`
Busca o livro de ofertas completo de um token via `py_clob_client.ClobClient.get_order_book()`. Calcula spread, midpoint, profundidades agregadas (`bid_depth`/`ask_depth`) e retorna os top-N níveis. Saída JSON.

### `polymarket-scanner/scripts/get_prices.py`
Busca midpoints, melhores bid/ask, spread e último preço executado para um ou mais tokens. Aceita `--token-id` repetível ou `--market-slug` (resolve via Gamma para os token IDs). Usa endpoints batch quando há múltiplos tokens.

### `polymarket-scanner/references/api-guide.md`
Documentação operacional dos dois endpoints: Gamma (`/markets`, parâmetros, schemas de resposta) e CLOB (book, prices, midpoint). Inclui rate limits, códigos de erro e parâmetros avançados.

### `polymarket-scanner/references/market-types.md`
Caracterização de cada categoria de mercado (Politics, Crypto, Sports, Weather, Entertainment) com perfil típico de volume, liquidez, duração e estrutura de fees.

---

## Skill 2 — `polymarket-analyzer/` (Detecção de Edge, Read-Only)

Identifica oportunidades de arbitragem, momentum, profundidade de book e correlação entre posições.

### `polymarket-analyzer/SKILL.md`
Front-matter com triggers ("find opportunities", "detect arbitrage", "edge detection", "momentum scanner"). Descreve os 4 scripts, fluxo recomendado (find_edges → analyze_orderbook → momentum_scanner) e referencia `fee-model.md` e `viable-strategies.md`.

### `polymarket-analyzer/scripts/find_edges.py`
Varre todos os mercados ativos buscando ineficiências de preço usando preços REAIS do CLOB (não os midpoints da Gamma, que somam $1 por construção):
- **Underpriced**: ask(YES) + ask(NO) < $1.00 → comprar ambos = lucro garantido.
- **Overpriced**: bid(YES) + bid(NO) > $1.00 → vender ambos.
- Calcula lucro líquido após fees e ranqueia por edge.

### `polymarket-analyzer/scripts/analyze_orderbook.py`
Análise profunda de um book individual: spread, mid-price, profundidade nos top-N níveis, ratio de imbalance bid/ask (sinaliza pressão direcional), classificação thin/thick e concentração de liquidez.

### `polymarket-analyzer/scripts/momentum_scanner.py`
Detecta atividade incomum em mercados:
- **Volume surges**: 24h volume vs. média de 7 dias.
- **Price momentum**: movimento direcional consistente.
- **Liquidity anomalies**: mudanças anormais em depth.
Saída ranqueada por força do sinal.

### `polymarket-analyzer/scripts/correlation_tracker.py` (1010 linhas)
Detecta exposição correlacionada oculta no portfolio paper. Carrega posições do SQLite, agrupa por **categoria** (US Politics, Geopolitics, Crypto, Sports, etc.) usando regras de keyword regex (sem ML, sem dependência externa), detecta qualifiers compartilhados ("insider trading", "FIFA World Cup"), calcula exposição agregada por cluster, emite alertas INFO/WARN/ALERT acima do threshold (`MAX_SINGLE_MARKET_PCT = 20%`) e produz um diversification score 0-100. Limite vem do `CLAUDE.md`.

### `polymarket-analyzer/references/fee-model.md`
Modelo de fees: a maioria dos mercados é fee-free; mercados crypto de 5min/15min usam taxa dinâmica `feeQuote = baseRate * min(price, 1-price) * size` (typically 0.063). Inclui tabela de taxa efetiva por preço e cálculo de breakeven.

### `polymarket-analyzer/references/viable-strategies.md`
Quatro estratégias que ainda funcionam em 2026 com win rate, retorno esperado e perfil de risco: market making, news trading, mean reversion e arbitragem cross-market. Baseado em on-chain analysis de 95M transações (apenas 0.51% das wallets >$1k de profit).

---

## Skill 3 — `polymarket-monitor/` (Alertas e Watch, Read-Only)

Polling contínuo com alertas estruturados em JSON.

### `polymarket-monitor/SKILL.md`
Front-matter com triggers ("monitor prices", "price alert", "watch market"). Descreve os dois scripts e exemplos. Pré-requisito: token IDs vindos do scanner.

### `polymarket-monitor/scripts/monitor_prices.py`
Polling multi-token com alertas. Argumentos: `--token-id` (repetível), `--interval` (default 30s, mínimo 5s), `--threshold` (default 5%), `--max-polls` (limite para uso não-interativo), `--baseline-window` (média móvel para baseline). Usa `get_midpoints` batch quando há múltiplos tokens. Emite uma linha JSON por alerta (tipo `price_alert`) e status em stderr.

### `polymarket-monitor/scripts/watch_market.py`
Monitoramento detalhado de um único mercado. Snapshot completo a cada `--interval` (default 15s) com midpoint, bid/ask, spread, depth bid/ask agregada, last trade. Saída uma linha JSON por snapshot (tipo `market_snapshot`).

### `polymarket-monitor/references/monitoring-guide.md`
Tabela com thresholds e intervalos recomendados por tipo de mercado (Politics: 3-5%/60s; Crypto 5-min: 10-20%/10s; Sports live: 15-25%/15s, etc.) e estratégias de monitoring (breakout detection, etc.).

---

## Skill 4 — `polymarket-paper-trader/` (Engine de Simulação)

Núcleo do sistema. Simula trades contra preços REAIS, persiste em SQLite, aplica risk engine.

### `polymarket-paper-trader/SKILL.md`
Front-matter sem version (definição mais minimalista). Documenta `paper_engine.py`, `execute_paper.py`, `portfolio_report.py`, `health_check.py` com exemplos de cada ação (init, buy, close, portfolio, trades). Tabela de risk rules built-in, descrição do funcionamento (book walking, fee modeling, SQLite em `~/.polymarket-paper/portfolio.db`) e API Python pública.

### `polymarket-paper-trader/scripts/paper_engine.py` (1075 linhas)
**Engine central**. Define schema SQLite (portfolios, positions, trades), constantes de risco default (`max_position_pct=10%`, `max_drawdown_pct=30%`, etc.), regex de validação de token ID. Funções públicas: `init_portfolio`, `place_order` (faz book walking simulado), `close_position`, `get_portfolio`, `get_trades`, `fetch_midpoint`. Toda query é parametrizada (defesa contra SQL injection). CLI `--action {init,buy,close,portfolio,trades}`.

### `polymarket-paper-trader/scripts/execute_paper.py`
Wrapper de alto nível que recebe uma **recomendação estruturada** (JSON com `token_id`, `side`, `action`, `size_usd`, `confidence`, `reasoning`, `strategy`) e a executa via `paper_engine.place_order()` com validação. Suporta `--dry-run`. É a interface usada pelo `strategy-advisor`.

### `polymarket-paper-trader/scripts/portfolio_report.py`
Analytics profundas: total/annualized return, win rate, Sharpe ratio, Sortino ratio, max drawdown, duração média de trade, melhores/piores trades. Saída texto formatado ou JSON.

### `polymarket-paper-trader/scripts/health_check.py` (664 linhas)
**One-command session start**. Implementa o workflow CLAUDE.md de session start em uma chamada: carrega portfolio, busca preços live para todas posições abertas, atualiza DB, calcula P&L por posição e nível de portfolio, checa thresholds graduados de drawdown (10/15/20%), checa daily loss (5%) e weekly loss (10%), e retorna status GREEN/YELLOW/RED. Exit codes: 0=GREEN, 1=YELLOW, 2=RED.

### `polymarket-paper-trader/references/paper-trading-guide.md`
Explica que Polymarket não tem testnet oficial e este engine preenche essa lacuna. Detalha o algoritmo de market order (book walking nível por nível), limit orders, fee modeling, e padrões de uso recomendados.

### `polymarket-paper-trader/references/risk-rules.md`
Referência completa de cada parâmetro de risco (chave, default, racional) e como sobrescrever via `--force` ou config customizado na inicialização.

---

## Skill 5 — `polymarket-strategy-advisor/` (Metodologia e Recomendações)

Ensina ao agente a metodologia de trading e gera recomendações scoradas.

### `polymarket-strategy-advisor/SKILL.md` (228 linhas — a mais densa)
Filosofia core (edge first, size by confidence, cut losers/ride winners, fees eat edge, paper first), metodologia de 6 passos (scan → filter → classify edge → Kelly sizing → validate → document/execute), formato exato de TRADE RECOMMENDATION, condições para parar de operar, erros comuns a evitar, e os scripts disponíveis. **Quando este SKILL.md conflita com CLAUDE.md, CLAUDE.md vence.**

### `polymarket-strategy-advisor/scripts/advisor.py` (593 linhas)
Pipeline completo: busca mercados (Gamma), scora edges combinando arbitragem + momentum + orderbook imbalance, aplica Kelly criterion (half-Kelly), valida contra risk rules (lendo o portfolio do paper trader em `~/.polymarket-paper/portfolio.db` se passado `--portfolio-db`), e produz JSON ranqueado por expected value. CLI: `--top`, `--min-volume`, `--min-edge`, `--portfolio-db`.

### `polymarket-strategy-advisor/scripts/backtest.py` (1137 linhas — maior do repo)
Engine de backtest sobre o histórico do paper trader. Replay de trades fechadas, mark-to-market de abertas, métricas (Sharpe com `RISK_FREE_RATE=4.5%`, drawdown, profit factor, breakdown por strategy). Modo `--live-check` retorna o assessment formal contra os 4 critérios do CLAUDE.md (20+ closed trades, win rate >55%, Sharpe >0.5, max drawdown <15%) com PASS/FAIL por critério.

### `polymarket-strategy-advisor/scripts/daily_review.py`
Revisão diária. Lê closed trades dos últimos N dias do SQLite, calcula métricas de performance, breakdown por estratégia (momentum, mean-reversion, etc.), e sugere ajustes acionáveis de parâmetros. Útil no fim do session loop diário.

### `polymarket-strategy-advisor/references/decision-framework.md`
Decision tree completa em ASCII: entry tree (Volume → Spread → End date → Accepting orders → Edge classifiable → Edge >5% após fees → Kelly >0 → Risk rules pass → TRADE), exit tree (stop-loss, target, time-based exit), tabelas de position sizing por nível de confiança, e cálculos de stop-loss matemáticos.

### `polymarket-strategy-advisor/references/viable-strategies.md`
Versão mais detalhada do `viable-strategies.md` do analyzer, com mais material sobre implementação prática das 4 estratégias (market making, news trading, mean reversion, arbitragem).

---

## Skill 6 — `polymarket-live-executor/` (Trading Real, Requer Opt-In)

A única skill que toca dinheiro real. Quatro camadas de safety.

### `polymarket-live-executor/SKILL.md`
Front-matter sem version, com triggers explícitos ("execute trade", "go live", "buy on polymarket"). Documenta requisitos de safety (confirmação humana obrigatória, env vars `POLYMARKET_PRIVATE_KEY` + `POLYMARKET_CONFIRM=true`, hard caps), wizard de setup em 5 passos, e uso dos 3 scripts.

### `polymarket-live-executor/.env.example`
Template do arquivo `.env`. Define `POLYMARKET_PRIVATE_KEY` (burner wallet, com 0x prefix), `POLYMARKET_CONFIRM=true` (gate de segurança), `POLYMARKET_MAX_SIZE` (default $5 first-time) e `POLYMARKET_DAILY_LOSS_LIMIT` ($10 first-time). Comenta os tiers de experiência. **Nunca commitar `.env` real** (gitignored).

### `polymarket-live-executor/scripts/setup_wallet.py`
Wizard de configuração com três modos:
- `--create`: gera burner wallet via `eth_account.Account.create()` e imprime endereço + private key (uma única vez).
- `--verify`: confere `POLYMARKET_PRIVATE_KEY` e `POLYMARKET_CONFIRM=true`.
- `--check-balance`: query on-chain do USDC contract `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` na Polygon RPC.

### `polymarket-live-executor/scripts/check_positions.py`
Cliente CLOB autenticado L2. Lê wallet address derivado, USDC balance, open orders e trade history. Lê private key de env var (nunca printa a key) e mostra apenas o address para confirmação. Subcomandos: `--balance`, `--orders`, `--trades`.

### `polymarket-live-executor/scripts/execute_live.py` (390 linhas)
**O único script que coloca ordens reais**. Camadas de defesa:
1. `check_safety_gates()` falha se `POLYMARKET_PRIVATE_KEY` não setada ou `POLYMARKET_CONFIRM != "true"`.
2. Hard cap `POLYMARKET_MAX_SIZE` aplicado antes de qualquer envio.
3. Daily loss limit checado contra `~/.polymarket-live/trades.log`.
4. **Prompt interativo** mostrando market, side, size, price, custo estimado, contexto de book — e exige o usuário digitar "yes"/"confirm".
Suporta limit (`--price`) e market (`--market --amount`) orders, BUY/SELL.

### `polymarket-live-executor/references/security.md`
Guia de segurança operacional. Regra #1: **NUNCA** use main wallet. Como criar burner via `eth_account` ou `cast wallet new`. Storage de chave (env var, nunca em arquivo). Permissões `chmod 600` em `.env`. Tiers de wallet por nível de experiência.

### `polymarket-live-executor/references/live-trading-checklist.md`
Checklist pré-flight a ser percorrido antes do primeiro trade real: setup de wallet, env vars configuradas, paper-trading prerequisites cumpridos, etc.

---

## Convenções e Pontos Importantes

- **Storage:** dados paper em `~/.polymarket-paper/portfolio.db` (SQLite), logs live em `~/.polymarket-live/trades.log`.
- **Dependências:** `pip install py-clob-client requests eth-account` (live só precisa de eth-account).
- **Venv:** todos os exemplos assumem `source ~/.venv/bin/activate` (ou `/home/verticalclaw/.venv` em alguns SKILL.md).
- **Sem testnet:** o paper engine simula contra preços live reais — é o substituto do testnet ausente.
- **Defesa em camadas para ir live:** paper-first → env var gate → human confirm por trade → position caps.
- **Hierarquia de regras:** `CLAUDE.md` > `SKILL.md` individuais > `references/`. Conflitos sempre resolvem em favor do CLAUDE.md.
- **Toda saída é estruturada:** JSON disponível em quase todos os scripts via `--json`, facilitando composição entre skills.
