# Deep Research — Modelos estatísticos para *Total de Runs* em jogos da MLB

> Objetivo: fundamentar uma **operação que analisa jogos da MLB e sugere entradas no mercado de "total de runs" (Over/Under)** — no contexto Polymarket, paper-trading-first.
>
> Contexto da casa (CLAUDE.md): isto é **pesquisa, não recomendação financeira**. Toda operação real envolve risco de perda. "Fees/vig comem edge". Edge tem de ser **quantificável** e classificável; sem edge, não há trade.

Confiança por afirmação: **[A]** corroborada por múltiplas fontes independentes / matemática verificável · **[B]** fonte respeitável única ou consenso de praticantes · **[C]** backtest de fornecedor / amostra pequena / extrapolação entre esportes (tratar com ceticismo).

---

## 0. Bottom line (o que importa para a operação)

1. **Modele a DISTRIBUIÇÃO de runs do jogo, não só a média.** Over/Under é uma pergunta sobre a cauda da distribuição (`P(total ≥ linha)`), então você precisa de uma PMF, não de um número. **[A]**
2. **Poisson simples falha** porque runs são *superdispersos* (variância ≈ 2× média) e *zero-inflados* (muitos shutouts). Use **Binomial Negativa / "Enby" / distribuição Tango**, ou **simulação Markov/Monte Carlo** por inning. **[A]**
3. **O mercado é quase eficiente no fechamento.** O edge realista é pequeno: ROI ~2–5%, win rate 53–55% (break-even ~52,4% a -110). Qualquer sistema que prometa 60%+ é provável overfit/survivorship. **[A]**
4. **Vantagem estrutural mais defensável = clima (vento em parques específicos) e timing de informação** (apostar cedo antes da linha convergir), não park factors estáticos que a linha já precifica. **[B]**
5. **Valide por calibração (Brier/log-loss) + CLV antes de ROI.** Precisa de **~1.000+ entradas** para distinguir skill de sorte. **[A]**
6. **Vantagem da Polymarket:** o preço já É a probabilidade implícita (0–1). Você compara `P(Over)` do modelo direto ao preço (depois do spread/fee), sem precisar "de-vigar" odds americanas. Isso simplifica o passo de edge. **[B]**

---

## 1. O núcleo do modelo — abordagem por distribuição (recomendada)

### 1.1 Por que não Poisson puro
Dados públicos (2008–2013, seandolinar / FanGraphs Community):
- Runs/inning: AL média **0,483**, variância **1,014**; NL **0,447** / **0,904**. **[A]**
- Runs/jogo: AL **4,50** / variância **10,0**; NL **4,26** / **9,14**. **[A]**
- **Variância ≈ 2× média** em ambos → assinatura de superdispersão; Poisson (que exige variância = média) **subestima shutouts, superestima innings de 1 run e tem cauda fina**. **[A]**
- Curiosidade útil: a média é >4 runs/jogo, mas o **modo (resultado mais provável de um time) é 3 runs**. **[A]**

### 1.2 As distribuições que funcionam
- **Binomial Negativa (NegBin)**: o segundo parâmetro absorve a superdispersão; ajusta bem por inning e por jogo. Ajuste por **método dos momentos**: `média u = r·B`, `variância v = r·B·(1+B)` ⇒ `B = v/u − 1`, `r = u/B`. Limitação: ainda **subconta shutouts**. **[A]**
- **"Enby" (NegBin zero-modificada, Tom Tango/walksaber)**: corrige a frequência de shutout. **[B]**
- **Distribuição Tango** (combinação por inning, math de Ben Vollmayr-Lee): constante de controle `c = 0,767` (um time isolado) / **`0,852` (dois times no mesmo jogo)** — esta última é a relevante para o total do jogo. **[B]**
- **Poisson Bivariada (Karlis & Ntzoufras, pacote R `bivpois`, via EM)**: adiciona correlação entre os placares dos dois times; **só use a versão *diagonal-inflated* ou Conway-Maxwell-Poisson (CMP)** para também capturar superdispersão. **[B]**

### 1.3 Alternativa mecanicista — Markov + Monte Carlo
- Meia-entrada = **cadeia de Markov de 25 estados absorventes** (8 configs de corredores × 3 outs + estado "3 outs"). Run esperado = Σ (probabilidades de transição × runs). **[A]**
- Validação publicada: modelo Markov previu **0,4396 ER/meia-entrada vs 0,4452 real (2012)** → erro <1,3%. **[A]**
- **Rollup para o jogo:** ou **convolução** das 9 distribuições de inning, ou **simulação Monte Carlo** de milhares de jogos (seed = lineup + starter + bullpen + park + clima) → PMF empírica do total. A cadeia de Markov entrega *distribuição*, não só média — exatamente o que Over/Under precisa. **[A]**
- Implementações open-source: `github.com/calestini/markov-baseball`. **[B]**

### 1.4 Estimadores de run "de uma fórmula"
- **BaseRuns (David Smyth)** — melhor estimador de runs em nível de time, robusto em ambientes extremos: `Runs = A·B/(B+C) + D` (A=corredores, B=avanço, C=outs, D=HR). RMSE ≈ **22,4 runs/time-temporada**. É o motor que o **FanGraphs Depth Charts** usa para projetar runs marcados/sofridos por time. **[A]**
- **Linear weights** = aproximação linear local do BaseRuns (derivada da matriz de run expectancy). Dá média rápida, **mas não distribuição**. **[A]**

### 1.5 Abordagem por ML (complementar, não substituta)
- **XGBoost** é o top consistente em comparações publicadas, mas **o teto de acurácia é baixo**: Elfrink **55,5%**, Cui ~53% — e isso é para *vitória/derrota*, não totals. Implicação: o sinal extraível é fino; **avalie por log-loss vs mercado de-vigado, não por acurácia bruta**. **[A]**
- Projetos O/U open-source existem mas são amadores (ex.: `github.com/TimHanewich/Baseball-Betting-NN`). **[C]**

### 1.6 Converter distribuição → entrada no mercado
- Linha com meio-run (sem push), ex. 8,5: **`P(Over) = P(total ≥ 9) = 1 − Σ_{k=0}^{8} P(k)`** (soma da cauda da PMF). Linha inteira (9,0): trate `P(total=9)` como **push** e renormalize. **[A]**
- **Sportsbook tradicional:** de-vigar (normalizar as duas pernas para somar 100%); a -110/-110 → 50%/50% justo, break-even 52,38%. **[A]**
- **Polymarket:** o preço do share "Over" já é a probabilidade implícita. Edge = `P_modelo(Over) − preço_Over − custo` (spread/fees). Entre só quando exceder um **limiar mínimo** (ver §5). **[B]**

---

## 2. Inputs preditivos, ranqueados

Ordem de consenso de praticantes para o total de um único jogo: **starters (ambos) > park > ofensiva true-talent dos dois lineups > bullpens/disponibilidade > clima (vento, depois temperatura) > defesa ≈ umpire/framing.** **[B]**

| Input | Achado quantificado | Conf. |
|---|---|---|
| **Métrica de pitcher** | Para *prever* ERA futura, **SIERA e xFIP > FIP > ERA** (ERA é a mais contaminada por sorte). SIERA usa K%, BB%, batted-ball. **K% e BB% estabilizam mais rápido; HR/9 é o mais ruidoso**. | [A] |
| **CSW%** (called strikes + whiffs) | Indicador rápido de skill de strikeout: R²≈0,59 com K%, estabiliza ~700 pitches. | [B] |
| **Times-through-the-order** | Cada passagem custa **~8–10 pts de wOBA**; OPS contra: 1ª .713 → 2ª .747 → 3ª .780. "Bullpen game" desloca o ambiente de runs. | [A] |
| **Bullpen** | Estimativa de mercado: bullpen explica ~20–25% da variação de runs tardios; troca para bullpen game move o total **~+0,3–0,7 run**. *Depende de disponibilidade recente dos braços* (a ERA não captura). | [C] |
| **Ofensiva** | **wRC+/wOBA** = melhor número único (ajustado a park/liga). Em nível de time o scoring é **não-linear nos extremos** → use BaseRuns. | [A] |
| **Platoon (L/R)** | Vantagem média ~**0,017 wOBA**; assimétrica (LHB têm split ~35 pts xwOBA vs ~6 pts dos RHB). Composição L/R do lineup vs mão do starter importa. | [B] |
| **Forma recente** | Stats de temporada (amostra maior) **batem "hot streaks" de 7 jogos** (ruído que regride). xwOBA > resultados crus. | [B] |
| **Park factor** | **Coors ~125–128 (runs +25–28%)**; GABP é o top de HR (~123–130). Coors e GABP são os únicos que sobem runs ≥5% E HR ≥10%. Tabelas: Baseball Savant. | [A] |
| **Temperatura** | **~+1% HR por +1°F (~2%/°C)** (Dartmouth/BAMS 2023; Nathan ~1,8%/°C). +10°F ≈ +3,3 ft de carry. | [A] |
| **Vento** | Variável climática mais impactante. 5 mph soprando para fora ≈ +18–20 ft de carry; **15+ mph para fora ≈ +1–2 runs no total**. Direção (out-to-CF) importa mais que velocidade. Vento no Wrigley pode mover a linha em até 1 run inteiro. | [B] |
| **Umpire / ABS** | Zona robótica/ABS aumenta ofensa: 1º mês cheio de ABS → walks +7,3%, pitches na zona ↓. **Rastreie quais jogos usam ABS/challenge.** | [B] |
| **Framing do catcher** | Bom framer vale ~15–25 runs/temporada (~0,1–0,15 run/jogo). Statcast: ~0,125 run por strike roubado. Neutralizado onde há ABS. | [B] |
| **Defesa (DRS/OAA)** | Spread de dezenas de runs/temporada (~0,1–0,3 run/jogo) — real, mas de 2ª ordem e já parcialmente embutido em estimadores batted-ball-aware. | [B] |

> **Lacuna honesta:** não existe estudo peer-reviewed com ranking de *feature importance* especificamente para *total de runs do jogo* (quase tudo mira vitória/derrota). Construir o próprio modelo e ler SHAP/feature importance sobre o alvo "total runs" preencheria isso. **[A]**

---

## 3. Realidade do mercado de totals (reality check)

- **Starters são o input dominante** que os books usam para montar a linha; troca de starter move a linha de baseball mais que qualquer mudança de jogador único em outros esportes → linhas "information-sensitive". **[B]**
- **Linha de fechamento ≈ eficiente** (absorveu lineups, clima, fluxo). Estudo de 2023: totals dos books explicam ~79% da variância dos resultados medianos. Benchmark de calibração da linha de fechamento (futebol, 397.935 jogos): R²≈0,997 — *extrapolação entre esportes, sinalizada*. **[C]**
- **Woodland & Woodland (1994, J. Finance):** mercado de moneyline da MLB tem viés (favorito-longshot reverso) detectável, **mas não lucrável após comissão**. É a conclusão neutra mais defensável para totals também. **[A]**
- **Edges documentados (todos com ressalva de overfitting/survivorship):**
  - *Vento/clima:* excluir parques de microclima (Oracle, Angel Stadium) "quase dobrou" o ROI reportado de uma amostra — sinal concentrado em poucos parques. **[C]**
  - *First-Five-Innings (F5):* mais previsível (starters dominam antes da variância do bullpen); linhas F5 mais perto de moneyline. Isola mismatch starter-forte/bullpen-fraco. **[C]**
  - *Steam:* seguir steam move documentado foi 1032-945 (**52,2%**) — mal cruza o break-even; **perseguir steam é geralmente não-lucrativo**, o edge está em *iniciar* o movimento (ter o número melhor primeiro). **[C]**
  - *Contrário:* jogos com ≥70% no under → over 234-226 (+2,6% ROI), amostra pequena (~460). **[C]**
- **Trate todo ROI de fornecedor como candidato a survivorship bias** até reproduzir out-of-sample. **[A]**

---

## 4. Stack de dados (com flags de ToS — leia antes de construir)

| Fonte | Uso | Custo / acesso | Flag legal |
|---|---|---|---|
| **MLB Stats API** (`statsapi.mlb.com/api/v1/`) | Schedule, **probable pitchers** (`hydrate=probablePitcher`), live feed, boxscore, standings | Grátis, sem auth | **Não-comercial/não-bulk** por padrão (copyright MLBAM). Modelo de aposta é plausivelmente "comercial". **[A]** |
| **Baseball Savant / Statcast** | Batted-ball, xwOBA/xBA, velocidade/spin, **park factors** | Grátis, export CSV (`/statcast_search/csv`) | Mesma restrição MLBAM; bulk = zona cinza. **[A]** |
| **Retrosheet** | Play-by-play histórico 1898–2025 — **melhor set de backtest** | Grátis (downloads estáticos) | **Mais permissivo**, MAS exige exibir o "Retrosheet notice" verbatim. **[A]** |
| **FanGraphs** | wOBA/wRC+/SIERA, projeções, Guts! park factors | View grátis; Membership ~$15/mês p/ export | **Sem API pública; proíbe scraping**. Use export manual de Membership. **[A]** |
| **Baseball-Reference / Stathead** | Stats/splits/game logs | Browse grátis; Stathead pago | **ToS mais restrito**: ~20 req/min (429 + "session jail"); proíbe criar DB concorrente. **[A]** |
| **pybaseball** (`pip install pybaseball`) | Wrapper de Statcast/FG/BBRef/Retrosheet | Grátis (MIT) | Levemente mantido; **herda a ToS de cada fonte**. Statcast/Retrosheet = baixo risco; BBRef/FG = alto. **[B]** |
| **Clima — NWS** (`api.weather.gov`) | Vento/temp para 29 parques US (Toronto usa API global) | Grátis, sem key | Mais baixo risco; só mande `User-Agent`. **[A]** |
| **Clima — Visual Crossing** | **Backfill histórico** de clima no horário do jogo (50 anos) | Free ~1.000 records/dia; pago ~$35/mês | key necessária. **[B]** |
| **Clima — Open-Meteo** | Temp/vento/umidade por lat-long, sem key | Free ~10k chamadas/dia | **Free tier só não-comercial**; pago p/ comercial. **[B]** |

### Odds históricas para backtest (guardar a linha de FECHAMENTO, não a de abertura)

| Fonte | Cobertura | Custo / flag |
|---|---|---|
| **SportsBookReviewsOnline** (`.../scoresoddsarchives/mlb`) | Totals + ML + run line, planilhas .xlsx — **arquivo gratuito clássico, congelado** (temporadas antigas) | Grátis; checar erros vs placar final. Scraper: `github.com/ArnavSaraogi/mlb-odds-scraper`. **[B]** |
| **Kaggle** (christophertreasure, "MLB Vegas Data") | 2012–2021, closing ML/total/over-under/run line, CSV pronto | Grátis (conta Kaggle); ver licença. **[B]** |
| **sports-statistics.com** | 2010–2021, opening/closing totals | Grátis (mirror do SBR). **[C]** |
| **The Odds API** | Histórico de totals desde 2020-06 (snapshots) | Pago (~$99/mês Business); **licenciado/limpo** — rota segura. **[B]** |

> **Projetos open-source:** **não existe** modelo público maduro de *over/under de runs* (>50 stars, mantido) — o nicho é dominado por preditores de vitória/derrota e produtos pagos (Ballpark Pal). Mais próximo: `github.com/laplaces42/mlb_game_predictor` (prevê *placar* via Ridge/Linear → derive o total, mas é estimativa pontual, não distribuição). Tooling base: `github.com/toddrob99/MLB-StatsAPI`, `github.com/jldbc/pybaseball`. **É um espaço "build-it-yourself".** **[B]**

### Receita concreta: projeção → runs esperados do jogo
`runs_sofridos_timeA = (starter_RA9 × IP_starter/9) + (bullpen_RA9 × (9 − IP_starter)/9)`, ajustado por park/clima/oponente. Notas: **use RA9, não ERA** (totals liquidam em runs totais; converta `ERA → RA9` com fator ~**1,06–1,10**); **projete o IP do starter** (média moderna **<5,5 IP** → ~3,5–4,5 IP vão pro bullpen; ignorar o pen omite ~40% da prevenção de runs); some `runs_sofridos_A (vs ofensiva B) + runs_sofridos_B (vs ofensiva A)` → total esperado → jogue numa NegBin/Monte Carlo para virar **distribuição**. **[B]**

### Timing de lineups / probables (operacional)
- **Probables:** `statsapi.mlb.com/api/v1/schedule?sportId=1&date=YYYY-MM-DD&hydrate=probablePitcher` — confiável p/ hoje/amanhã, tentativo 3–7 dias à frente. **Re-puxe na manhã do jogo** e trate **scratches/"openers"** (books oferecem regra "Action vs Listed Pitcher"). **[A]**
- **Lineups confirmados:** saem ~2–4h antes; só "oficiais" quando o cartão vai ao umpire. **Trave a entrada ~5–10 min antes do primeiro arremesso** com lineup confirmado. MLB Stats API é a fonte de verdade (lineups.com/rotogrinders só para conferência visual; scraping deles fere ToS). **[B]**

### Sistemas de projeção
- **FanGraphs Depth Charts** = o único que entrega **runs marcados/sofridos por time** diretamente (blend 50/50 Steamer+ZiPS × playing time manual → **BaseRuns**), em `depthcharts.aspx?position=Standings`. **Melhor sinal off-the-shelf.** **[B]**
- **ZiPS** publica **projeções percentil 1–99** por jogador → ótimo para modelar caudas/incerteza do O/U. **[B]**
- **ATC** = ensemble ponderado por acurácia; métrica **InterSD** mede discordância entre sistemas (útil p/ incerteza). **[B]**
- **THE BAT / THE BAT X** (Derek Carty) = sistema "original" mais acurado por vários anos; já embute park/platoon/umpire/air density. Projeções diárias DFS são pagas. **[B]**
- **Marcel** = baseline aberto e auto-computável (`github.com/bdilday/marcelR`) — **a opção sem risco de ToS** para automatizar. Se seu modelo não bate o Marcel, repense. **[A]**

---

## 5. Staking e validação (a parte que separa skill de sorte)

- **Kelly para aposta binária:** `f* = (b·p − q)/b`, `b` = odd decimal − 1, `p` = prob do modelo, `q = 1−p`. Ex.: Over a 1,91 (-110) com `p`=0,55 → f* ≈ **6,06%** do bankroll (Kelly cheio). **[A]**
- **Use Kelly fracionário (¼–½), cap de ~5% por entrada.** Variância escala com o *quadrado* da fração; ½-Kelly corta variância ~75% perdendo só ~25% de crescimento. Kelly cheio só é ótimo se `p` for exato — e nunca é. → casa direto com o **half-Kelly da §2 do CLAUDE.md**. **[A]**
- **EV:** `EV = p·(lucro|win) − (1−p)·stake`. Break-even a -110 = **52,38%**; edge mínimo = 2,38 pts sobre a linha justa. Na Polymarket: exija `P_modelo − preço` acima de um buffer que cubra spread/fee + erro de calibração (ex.: só entrar com edge ≥ ~3–4 pts). **[A]**
- **Backtesting:** **walk-forward** (treina passado, testa próximo segmento cronológico), **time-cuts estritos pré-primeiro-arremesso** (nada de info pós-jogo), preço cada entrada histórica na linha real do momento. k-fold comum é inválido (vaza futuro). **[A]**
- **Linha de fechamento é o melhor preditor pré-jogo; abertura é o pior** (Miller & Rapach, NFL) → aposte em totals "moles" cedo e meça-se contra o fechamento. **[A]**
- **Métricas:** **Brier** `=(1/N)Σ(pᵢ−oᵢ)²` e **log-loss** `=−(1/N)Σ[oᵢln pᵢ+(1−oᵢ)ln(1−pᵢ)]` (log-loss pune excesso de confiança mais forte) + **reliability diagram** (calibração na diagonal 45°). **Avalie calibração + CLV antes de ROI.** **[A]**
- **Tamanho de amostra:** ~**1.100 entradas** para um yield de 5% atingir p<0,05. Uma temporada MLB tem ~2.430 jogos → ~1 entrada/jogo/temporada é o mínimo para começar a provar skill. **[A]**
- **ROI realista de sharp:** **2–5% / win rate 53–55%**. Só ~3–5% dos apostadores são lucrativos no longo prazo. **[A]**
- **CLV** (positivo persistente) é o sinal de skill mais rápido — confiável bem antes do P&L. Mas é **mais fraco em sub-mercados de baixa liquidez** (cuidado se o mercado de totals da Polymarket for raso). **[B]**

---

## 6. Blueprint de operação para o seu repositório

Encaixa no fluxo `Scanner → Analyzer → Strategy Advisor → Paper Trader` e na constituição (edge classificável, half-Kelly, fees comem edge, paper-default).

```
1. INGEST  (diário, pré-jogo)
   - MLB Stats API: jogos do dia + probable pitchers  (já temos list_games_today.py p/ a Polymarket!)
   - FanGraphs Depth Charts (export): runs marcados/sofridos por time (BaseRuns)
   - Savant: park factors;  NWS/Visual Crossing: vento+temp por estádio
   - Polymarket: preço atual do mercado "total runs" de cada jogo

2. MODEL  (edge type = "model/news-driven")
   - λ_casa, λ_visitante = f(starter SIERA/xFIP, bullpen, ofensiva wRC+ vs mão,
                              park factor, temp, vento)  [ajuste a média]
   - Distribuição do total: NegBin/Enby por time  OU  Markov+Monte Carlo por inning
   - P(Over linha) = soma da cauda da PMF;  trate push em linha inteira

3. EDGE
   - edge = P_modelo(Over) − preço_Over_Polymarket − custo(spread/fee)
   - entra só se edge ≥ limiar (ex. 3–4 pts) E confiança classificável

4. SIZE
   - half-Kelly com p=P_modelo, cap 5% (e os caps da §2 do CLAUDE.md)

5. EXECUTE (paper primeiro) + LOG
   - registra TODO trade e TODO skip (CLAUDE.md regra #8)

6. REVIEW (contínuo)
   - Brier, log-loss, reliability diagram, CLV vs preço de fechamento, ROI
   - não confie em ROI até ~1.000 entradas; confie em CLV/calibração antes
```

**Decisões de design recomendadas:**
- **Comece pela distribuição** (NegBin/Enby), não por ML — menos dados, mais interpretável, entrega cauda. Adicione XGBoost depois como cross-check, julgado por log-loss.
- **Baseline obrigatório = Marcel + BaseRuns** (open, sem ToS) antes de pagar FanGraphs.
- **Foque o edge onde o mercado sub-ajusta: vento em parques específicos e timing** (entrar antes da linha mover), não no park factor estático.
- **Calibração honesta:** rode a operação 100% paper por ≥1 temporada-equivalente medindo Brier/CLV antes de qualquer capital real (e os pré-requisitos de live da §4 do CLAUDE.md: 20+ trades, win>55%, Sharpe>0,5, drawdown<15%).

---

## 7. Lacunas e ressalvas (honestidade intelectual)

- **Nenhum estudo peer-reviewed reporta RMSE em total de runs ou Brier/log-loss calibrado no mercado O/U da MLB** com curvas de calibração — você terá de gerar essas métricas. **[A]**
- Números de **bullpen (20–25%)** e **vento (1–2 runs)** são estimativas de praticantes, não peer-reviewed → direcionais. **[C]**
- Run values de **framing** variam muito por metodologia (40–50+ runs antigos vs 15–25 modernos). **[B]**
- Várias páginas primárias (FanGraphs, BBRef, Savant, PDFs do Nathan, arXiv) retornaram **HTTP 403 ao fetch automático** — números vieram de índices de busca; **confira os decimais na fonte antes de codificar** (ex.: RMSE .021/.063 de SIERA, 0,125 run/strike de framing, R²≈0,997 de fechamento). **[A]**
- **ToS/comercial:** um modelo de aposta é plausivelmente "comercial"; MLBAM e Sports-Reference restringem uso comercial/bulk. Para produção/live, considere um feed licenciado (Sportradar, SportsDataIO). **[A]**
- Acurácia de ML ~55% é **vitória/derrota**, não totals — use como teto de quanto sinal é extraível, não como performance de O/U. **[A]**

---

## 8. Fontes principais (deduplicadas)

**Distribuições/modelos:** stats.seandolinar.com (Poisson / NegBin run distribution); community.fangraphs.com/run-distribution-using-the-negative-binomial-distribution; walksaber.blogspot.com "On Run Distributions" pt.1–4 (Enby/Tango); Karlis & Ntzoufras `bivpois` (jstatsoft.org/article/view/v014i10); arXiv 2409.17129 (Bayesian Bivariate CMP); faculty.winthrop.edu/polaskit/Spring13/Baseball.pdf (Markov, erro <1,3%); github.com/calestini/markov-baseball; library.fangraphs.com/features/baseruns; Elfrink (cs.vu.nl/~sbhulai/papers/paper-elfrink.pdf, XGBoost 55,5%); fisher.wharton.upenn.edu Cui thesis.

**Inputs:** library.fangraphs.com/pitching/siera; blogs.fangraphs.com/new-siera-part-four-of-five-testing; pitcherlist.com (CSW%); sabr.org/.../lichtman-the-penalty-for-pitchers-going-through-the-batting-order; library.fangraphs.com/offense/wrc; blogs.fangraphs.com/estimating-hitter-platoon-skill; baseballsavant.mlb.com/leaderboard/statcast-park-factors; home.dartmouth.edu/news/2023/04/... (temp→HR); baseball.physics.illinois.edu/carry.html (vento, Nathan); baseballsavant.mlb.com/leaderboard/catcher-framing.

**Mercado/eficiência:** onlinelibrary.wiley.com/doi/10.1111/j.1540-6261.1994.tb04429.x (Woodland 1994); link.springer.com/article/10.1007/s12197-015-9322-x (season-win bias); pinnacle.com (efficient market); vsin.com (CLV); arxiv.org/pdf/1211.4000 (Miller & Rapach, closing line).

**Dados/projeções:** statsapi.mlb.com + github.com/toddrob99/MLB-StatsAPI/wiki; baseballsavant.mlb.com/statcast_search; retrosheet.org/downloads; github.com/jldbc/pybaseball; api.weather.gov (NWS); visualcrossing.com; fangraphs.com/depthcharts.aspx?position=Standings; baseball-reference.com/about/marcels.shtml + github.com/bdilday/marcelR; blogs.fangraphs.com/faq-exporting-data (no-scraping).

**Staking/validação:** en.wikipedia.org/wiki/Kelly_criterion; marketmath.io/blog/kelly-criterion-guide; boydsbets.com/expected-value-in-sports-betting; rebelbetting.com/faq/p-value (1.100 entradas); howtolearnmachinelearning.com/articles/brier-score; blog.trainindata.com/probability-calibration-in-machine-learning; pinnacleoddsdropper.com/blog/closing-line-value.

---

*Pesquisa para construção de modelo em modo paper — não é recomendação financeira. Operação real envolve risco de perda.*
