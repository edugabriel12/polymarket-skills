# Polymarket Skills — Documentação Completa do Repositório

Este documento descreve, arquivo por arquivo, todo o conteúdo do repositório `polymarket-skills`. O projeto é um conjunto de **Agent Skills** componíveis para trading sistemático na Polymarket (mercados de previsão), com foco em paper trading primeiro e execução real apenas com confirmação humana explícita.

---

## Visão Geral da Arquitetura

```
Scanner ──→ Analyzer ──→ Strategy Advisor ──→ Paper Trader ──→ Live Executor
(find)      (evaluate)    (recommend)          (simulate)       (execute)
```

Seis skills compõem o **pipeline base** de trading: descoberta de mercado → detecção de edge → recomendação → simulação → execução real. Todas as APIs usadas são `gamma-api.polymarket.com` (metadata, sem auth), `clob.polymarket.com` (preços/orderbook sem auth, trading com L2 auth via wallet) e `data-api.polymarket.com` (posições/trades de wallets, sem auth).

Em torno desse pipeline o repositório cresceu para um **sistema completo** (versão atual v13.x) que inclui, além das 6 skills base:

- **Skills componíveis adicionais** — `polymarket-category-watcher`, `polymarket-wallet-analyzer`, `polymarket-soccer-goals`, `polymarket-forecast-skill` e o core compartilhado `polymarket-forecasting`.
- **Subsistema Weather Edge Bot** — um bot autônomo de paper trading em mercados de clima (bot + judge de IA + advisor semanal), vivendo dentro de `polymarket-analyzer/scripts/`.
- **Agente autônomo** (`agent/`) — loop Python dirigido pelo Anthropic SDK que executa o workflow do `CLAUDE.md` em ciclos.
- **Quatro web apps** — `dashboard/` (monitor do weather bot), `polymarket-dashboard/` (modelo de futebol), `polymarket-wallet-dashboard/` (análise de CSV de histórico), `polymarket-copy-trader/` (copy-trade em paper).

As seções "Skill 1" a "Skill 6" abaixo cobrem o pipeline base; as seções "Componente 7" em diante cobrem tudo que foi somado depois.

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
**Manifesto de skills** seguindo a especificação Agent Skills. Lista as skills com `name`, `description` (gatilhos em linguagem natural) e a lista exata de arquivos que pertencem a cada skill — usado por agentes/instaladores para descoberta automática.

### `.run/`
Run configurations do IntelliJ IDEA (para os dashboards): configs de frontend (`npm dev`), backend (`uvicorn`) e a config combinada que roda ambos. Detectadas ao abrir o projeto na raiz do repositório.

### `research/soccer-goals-btts-deep-research.md`
Nota de pesquisa que fundamenta a skill `polymarket-soccer-goals`: modelagem de gols com Dixon-Coles, mercados Over/Under e BTTS, fontes de dados (Elo, xG) e a filosofia de edge calibrado. É o documento que a skill "operacionaliza".

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

## Componente 7 — Subsistema Weather Edge Bot (dentro de `polymarket-analyzer/scripts/`)

Um bot autônomo de **paper trading em mercados de clima** (temperatura máxima/mínima por cidade) construído em cima do analyzer. Descobre mercados de clima que resolvem nas próximas 48h, calcula P(YES) a partir de forecasts (OpenWeather + fontes de consenso), mede o edge contra o preço implícito, e — após um **judge de IA** aprovar — executa via paper trader e monitora até cashout ou resolução. Toda decisão é persistida em SQLite (`~/.polymarket-paper/weather_edge.db`) e num log append-only (`weather_edge.jsonl`).

### `weather_edge_bot.py`
**Daemon principal.** Loop de 60s que agenda tarefas por timestamp de último-run: **Discovery** (a cada 10 min — varre mercados de clima, faz parse, busca forecast, calcula edge, insere entradas `PROPOSED`), **Execute** (a cada 60s — pega entradas `APPROVED` pelo judge, revalida orderbook + risco, executa via `paper_engine`), **Monitor** (a cada 60s — cadência adaptativa por TTR; faz cashout só quando `forecast_prob_now < entry_implied` E `best_bid >= entry_price`), **Resolution** (a cada 1h — consulta Gamma por `outcomePrices` de posições vencidas) e **Heartbeat** (a cada 5 min). Paper-only por default.

### `weather_edge_judge.py`
**Gatekeeper de IA (daemon Claude).** Faz poll de entradas `PROPOSED`, reúne fontes extra (NWS, Visual Crossing, web search) e emite verdict `APPROVE` / `REJECT` / `ADJUST`. A partir da v13.2 usa um **gate condicional**: `_judge_route()` resolve os casos decisivos SEM chamar o LLM (coin-flips deterministas → AUTO-REJECT; apostas de ensemble apertado longe do bin → AUTO-APPROVE); só os casos genuinamente incertos escalam para o LLM. Cap de budget diário `JUDGE_DAILY_BUDGET_USD` (default $15) — excedido, marca o resto como `SKIPPED`. Modelo default `claude-sonnet-5`.

### `weather_edge_db.py`
Camada de persistência SQLite do bot: schema das tabelas (`entries`, `monitor_checks`, `cashouts`, `resolutions`, `counterfactuals`, `judge_reviews`, `forecast_history`, `advisor_runs`) e helpers de acesso.

### `weather_edge_helpers.py`
Funções puras do bot: parsing de mercado (cidade/threshold/comparação/data), cálculo de P(YES) via CDF normal com MAE dinâmico, sizing por slippage (`compute_max_size_for_slippage`, reusado pelo copy-trader), lógica de cashout. Sem I/O — testável isoladamente.

### `weather_edge_analyzer.py`
Análise **contrafactual** + sugestão de thresholds. Agrega o histórico do bot (`aggregate_by_bucket`, `aggregate_judge`, `aggregate_cashout_triggers`, `compute_counterfactuals`, `replay_entry`) — as mesmas funções que o `dashboard/` consome — respondendo "as posições que fiz cashout teriam valido mais se eu tivesse segurado?".

### `weather_edge_backtest.py`
Backtester do subsistema: replay de trades passados com parâmetros alternativos de política de cashout (`profit_lock_pp`, etc.) e relatório de qual P&L cada configuração teria produzido.

### `weather_strategy_advisor.py`
**Meta-agente semanal (Claude Opus, read-only).** Lê a performance agregada do bot e propõe ajustes de tuning para revisão do operador. Escreve um relatório markdown em `~/.polymarket-paper/advisor_reports/` — **nunca modifica código ou config**. Os limites de risco do `CLAUDE.md` permanecem constitucionais: o advisor pode sugerir apertá-los, nunca afrouxá-los.

### `strategy_advisor_helpers.py`
Helpers compartilhados de agregação (ex.: `_city_performance` — win rate/P&L por cidade), consumidos pelo advisor e pelo dashboard.

### `force_resolution_sweep.py` / `repair_resolutions.py` / `snapshot_split.py`
Utilitários operacionais one-shot: varredura manual de resolução (limpa posições que o daemon deixou abertas), reparo de resoluções corrompidas por um bug antigo de settlement, e split do snapshot `weather_edge.db` em arquivos menores para download.

### Referências novas do analyzer
`weather-edge-strategy.md` (fórmula de edge, MAE dinâmico, consenso multi-source, versionamento v7→v13), `weather-judge-prompt.md` e `strategy-advisor-prompt.md` (prompts versionados), `weather-cities.json` (estações de resolução + bias por cidade), `applying-advisor-suggestions.md`, `deferred-tunings.md` e `data-api.md`.

---

## Componente 8 — `agent/` (Agente Autônomo de Paper Trading)

Loop Python que roda o workflow do `CLAUDE.md` em ciclos periódicos, dirigido pelo Anthropic SDK. **Só opera em paper mode** — a `polymarket-live-executor` não é exposta como ferramenta ao agente.

### `agent/run.py`
O loop principal: a cada `AGENT_INTERVAL` segundos chama Claude (Sonnet default) com o `SYSTEM_PROMPT` cacheado e um kickoff (session-start ou daily review conforme a hora UTC). Expõe duas ferramentas ao modelo — `run_script` (valida path e roda `python script.py` das 5 skills base) e `read_file`. Logs vão para stdout → journald; persistência no SQLite paper trader.

### `agent/reset_dbs.py`
Utilitário para resetar os bancos SQLite (portfolio + weather edge) a um estado limpo.

### Unidades systemd
`polymarket-agent.service` (o agente), `weather-edge-bot.service` + `weather-edge-judge.service` (o subsistema de clima), e `weather-strategy-advisor.service` + `.timer` (o advisor semanal). `agent/.env.example` documenta as env vars (`ANTHROPIC_API_KEY`, `POLYMARKET_SKILLS_ROOT`, intervalos).

---

## Componente 9 — Skills componíveis adicionais

### `polymarket-category-watcher/` (Descoberta por categoria, read-only)
Preenche a lacuna onde o scanner base retorna uma única categoria hard-coded: aqui você dá uma **categoria/esporte** (basketball, tennis, soccer, baseball, hockey, esports…) e recebe **todos** os mercados live (paginado, não limitado a uma página), com stream opcional de preços. Scripts: `list_category_markets.py` (descoberta one-shot), `list_games_today.py` (jogos do dia), `watch_category.py` (stream com re-scan de mercados novos) e `category_common.py` (helpers compartilhados). Aliases em português funcionam (basquete, tênis, futebol…). Referência: `category-tags.md`.

### `polymarket-wallet-analyzer/` (Análise de wallet pública, read-only)
Analisa **qualquer** wallet pública pelo endereço via **Data API** (`data-api.polymarket.com`): posições, P&L realizado/não-realizado, ROI, win rate geral e breakdown **por categoria** (Tennis, Soccer, LoL, CS, Baseball…). Sem private key. Distinto de `live-executor/check_positions.py` (que inspeciona a *sua* wallet autenticada). Script: `analyze_wallet.py` (flag `--enrich-tags` usa tags da Gamma para categorização mais precisa). Referência: `data-api.md`.

### `polymarket-soccer-goals/` (Over/Under de gols + BTTS)
Operacionaliza `research/soccer-goals-btts-deep-research.md`: descobre os jogos de futebol do dia, modela a distribuição de gols com **Dixon-Coles** (Poisson + correção de baixos placares `τ`), calcula `P(Over)` / `P(BTTS)`, e sugere entradas com edge quantificado — sizing por half-Kelly sob os caps da constitution, filtrado a payout 1.50×–3.0×. λ resolvido automaticamente por dados (baseline de liga auto-calibrado via football-data.org / API-Football, força via ratings CSV → xG → API-Football → Elo). Read/análise apenas — nunca opera live. Scripts principais: `suggest_soccer.py`, `dixon_coles.py`, `forecast_soccer.py`, `backtest_soccer.py`, `soccer_predictions.py`, fontes de rating (`ratings_sources.py`, `apifootball_source.py`, `baselines_source.py`, `sharp_odds_soccer.py`), com suíte de testes offline. Referências: `model.md`, `data-sources.md`, `calibrated-forecasting-research.md`.

### `polymarket-forecasting/` (Cores de forecasting compartilhados)
Bibliotecas **sport-agnostic, pure stdlib, sem imports de skill**, reusadas pelos modelos de previsão (soccer) e pelos dashboards. Módulos: `run_distribution.py` (Negative-Binomial/totais, P(Over), μ implícito), `forecast.py` (pmf→cdf/quantil/intervalo/entropia + confiança), `scoring.py` (CRPS, Brier, log-loss), `calibration_core.py` + `calibration.py` (ECE/reliability/decomposição de Brier), `congruence.py` (concordância entre fontes) e `audit_log.py` (dump da auditoria de math). Todos os testes são offline.

### `polymarket-forecast-skill/` (Previsão do tempo via OpenWeather)
Skill standalone que fornece forecasts de clima em tempo real via **OpenWeather API** (chave em `config.json`). Comandos: `current`, `forecast <dias 1-5>`, `compare <loc1> <loc2>`. Script: `scripts/get_weather.py`. É a base de dados de clima que alimenta o raciocínio de mercados de temperatura. Referências: `setup.md`, `prompt_templates.md`.

---

## Componente 10 — Web apps (dashboards)

Quatro aplicações web independentes, todas paper-first / read-only e rodando em portas distintas para coexistirem.

### `dashboard/` (Weather Edge Dashboard — FastAPI + HTMX + Plotly + SSE, porta 8765)
Monitor read-only do stack bot/judge/advisor. Quatro abas: **Overview** (KPIs, P&L cumulativo, eventos recentes, top posições), **Positions** (posições abertas com barras de progresso de trigger P/T/C + modal de `replay_entry`), **Performance** (seletor de período → gráficos Plotly de P&L por trigger, win rate por cidade, calibração do judge, delta contrafactual + tabelas de detalhe) e **Live Events** (stream SSE do `weather_edge.jsonl`). Lê os SQLite do bot em modo `?mode=ro` (zero risco de corromper o estado enquanto o bot roda). Estrutura: `main.py`, `settings.py`, `services/` (portfolio, positions, analytics, charts, ladders, advisor, costs, live_trading…), `templates/` (Jinja + partials), `static/`. Cada service tem testes inline.

### `polymarket-dashboard/` (Modelo de Futebol — FastAPI + React, portas 8000/5173)
UI colorida para o modelo `polymarket-soccer-goals` (total-goals + BTTS), rodado via subprocess. Duas abas: **Análises** (sugestões do dia com toda a math do modelo — λ_home/λ_away, P(Over)/P(Under)/P(BTTS), edge, payout, Kelly; scheduler in-process recalcula no topo de cada hora em fuso configurável) e **Resultados** (ROI/P&L/win rate diário/semanal/mensal; cada visita dispara settlement via feed da football-data.org). Backend `backend/app.py`; frontend React + Vite + Tailwind + Recharts. Env vars de modelo: `FOOTBALL_DATA_TOKEN`, `APIFOOTBALL_KEY`, `ODDS_API_KEY`, etc. Suporta notificações Telegram/email e auth de usuários.

### `polymarket-wallet-dashboard/` (Análise de CSV de histórico — FastAPI + React, portas 8001/5174)
Você **faz upload de um CSV** (`*_historico.csv`, `;`-delimitado, decimais BR) em vez de digitar um endereço. Reporta win rate, nº de apostas, P&L e ROI — geral, **por nível de confiança** (Alta/Média/Baixa), **por categoria** (Futebol, Baseball, Basquete, Combat Sports, Hockey, Tênis…) e **por sub-categoria** (Ambas Marcam, Over/Under, Moneyline, Run line, Spread…). Backend: `csv_parser.py` (classifica evento em categoria via dicionários de times + sinais), `subcategory.py` (classificador em camadas), `wallet_report.py` (`rollup_csv`). Um endpoint on-chain `GET /api/wallet?address=…` também está disponível. Texto de evento é untrusted (só pattern-matched).

### `polymarket-copy-trader/` (Copy-trade em paper — FastAPI + React, portas 8002/5175)
Fluxo separado de copy-trade: salva wallets públicas por nome+endereço, acompanha continuamente seus **buys e sells** via Data API `/trades`, e espelha num **paper portfolio de $10.000 fake-USD**. Por default só copia mercados de **clima** (`COPY_WEATHER_ONLY=0` copia todos). BUY copia o mesmo valor USD gasto, clampado a [$5, $100], com guarda de 20% de slippage; SELL espelha a fração vendida, pulado se exceder 20% de slippage; settlement só quando o mercado **resolve de fato** (não meramente porque o preço live está perto de 0/1). Read-only, **sem private key**. Reusa (sem reimplementar): o sizer de slippage do analyzer, o book-walk fill do paper trader e o feed de trades do wallet-analyzer — fiação em `backend/deps.py`. Abas: **Carteiras**, **Entradas**, **Resultados**.

---

## Convenções e Pontos Importantes

- **Storage:** dados paper em `~/.polymarket-paper/portfolio.db` (SQLite); estado do weather bot em `~/.polymarket-paper/weather_edge.db` + log append-only `weather_edge.jsonl`; relatórios do advisor em `~/.polymarket-paper/advisor_reports/`; logs live em `~/.polymarket-live/trades.log`. Cada web app usa seu próprio SQLite.
- **Portas dos web apps** (distintas para coexistirem): `dashboard/` 8765; `polymarket-dashboard/` 8000 (back) / 5173 (front); `polymarket-wallet-dashboard/` 8001 / 5174; `polymarket-copy-trader/` 8002 / 5175.
- **Dependências:** `pip install py-clob-client requests eth-account` (live só precisa de eth-account).
- **Venv:** todos os exemplos assumem `source ~/.venv/bin/activate` (ou `/home/verticalclaw/.venv` em alguns SKILL.md).
- **Sem testnet:** o paper engine simula contra preços live reais — é o substituto do testnet ausente.
- **Defesa em camadas para ir live:** paper-first → env var gate → human confirm por trade → position caps.
- **Hierarquia de regras:** `CLAUDE.md` > `SKILL.md` individuais > `references/`. Conflitos sempre resolvem em favor do CLAUDE.md.
- **Toda saída é estruturada:** JSON disponível em quase todos os scripts via `--json`, facilitando composição entre skills.
