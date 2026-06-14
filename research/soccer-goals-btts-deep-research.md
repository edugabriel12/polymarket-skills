# Deep Research — Total de Gols (Over/Under) e Ambos Marcam (BTTS) no Futebol

> Objetivo: estender o app com uma **operação de futebol** que analisa jogos e sugere entradas nos mercados de **total de gols** e **BTTS (both teams to score)** na Polymarket — análoga à operação de MLB (mas com **Dixon-Coles** no lugar da Binomial Negativa).
>
> Contexto da casa (CLAUDE.md): pesquisa, **não é recomendação financeira**. Edge tem de ser quantificável e classificável; sem edge, sem trade. Fees/spread comem edge.

Confiança: **[A]** corroborada por múltiplas fontes / matemática verificável · **[B]** fonte respeitável única ou consenso · **[C]** vendor/backtest/amostra pequena (ceticismo).

---

## 0. Bottom line

1. **Use Dixon-Coles, não Binomial Negativa.** Gols de futebol são **~equidispersos (variância/média ≈ 1)** — ao contrário do beisebol (~2×). Então o ganho da NegBin é marginal; o modelo certo é **Poisson duplo (Maher) + correção de placares baixos de Dixon-Coles + decaimento temporal**. **[A]**
2. **Tudo sai da matriz de placares.** Com `λ_casa` e `λ_fora`, monte `P(i,j)`; daí: `P(Over X.5) = Σ P(i,j) com i+j>X` e `P(BTTS) = 1 − P(casa=0) − P(fora=0) + P(0,0)`. **[A]**
3. **BTTS é assimétrico:** governado pelo **menor λ** (ataque mais fraco vs defesa mais forte). Um jogo "Over 2.5" pode ser "No BTTS" (ex.: 4–0 esperado). Métricas defensivas e desfalques de zaga pesam mais no BTTS que no total. **[A]**
4. **Mercado quase eficiente no fechamento.** Yield realista de modelo-vs-mercado em O/U 2.5 é **~0,8%** (e **só com odds máximas**; com odds médias dá prejuízo). Claims de 35–100% são overfit. **[A]**
5. **Vantagem Polymarket:** preço = probabilidade → `edge = P_modelo − preço − fee`, sem de-vig. Mas o próprio preço já é um *prior* afiado — exija margem e valide por CLV. **[A]**
6. **Input dominante = xG/xGA rolante** (mais preditivo que gols passados), depois baseline da liga, mando, forma com decaimento, escalações. **[A]**

---

## 1. O núcleo do modelo — Dixon-Coles

### 1.1 Poisson duplo (Maher 1982)
Gols do mandante `X ~ Poisson(λ)`, visitante `Y ~ Poisson(μ)`, com:
- `λ_casa = ataque_casa × defesa_fora × média_gols_casa_liga × vantagem_casa`
- `λ_fora = ataque_fora × defesa_casa × média_gols_fora_liga`
- `ataque_time = gols marcados/jogo ÷ média da liga`; `defesa_time = gols sofridos/jogo ÷ média da liga`. **[A]**
- Forma log-linear comum: `log λ = intercepto + casa + ataque_i + defesa_j`. **[A]**

### 1.2 Por que NÃO Binomial Negativa
Gols de futebol têm **VMR ≈ 1** (equidispersos; em alguns torneios até subdispersos). "A superdispersão, quando existe, é pequena e na prática não justifica a NegBin." → fica com Poisson. (Contraste com o beisebol, que é ~2× — por isso lá usamos NegBin.) **[A]**

### 1.3 Correção de Dixon-Coles (1997) — o ponto-chave para O/U e BTTS
Poisson independente **subestima empates e placares baixos** e ignora a correlação (~0,2) entre os placares. A correção `τ` multiplica `P(i,j)` só nas 4 linhas baixas:

| Placar (x,y) | τ |
|---|---|
| (0,0) | 1 − λμρ |
| (0,1) | 1 + λρ |
| (1,0) | 1 + μρ |
| (1,1) | 1 − ρ |
| outros | 1 |

`P(x,y) = τ(x,y;λ,μ,ρ) · Poisson(x;λ) · Poisson(y;μ)`. `ρ=0` recupera o Poisson independente; `ρ>0` move massa de 1-0/0-1 para 0-0/1-1, ajustando **Under 2.5 e No-BTTS** diretamente. `ρ` é restrito para manter os 4 `τ ≥ 0`. **[A]**

### 1.4 Decaimento temporal (peso por recência)
Pondere cada jogo histórico por `φ(t) = exp(−ξ·t)`, `t` = idade do jogo. Dixon-Coles acharam **ξ ≈ 0,0065/dia** ótimo — permite treinar em várias temporadas dando mais peso ao recente (melhor que um corte "últimos N jogos"). **[B]**

### 1.5 Alternativas (não preferidas)
- **Poisson bivariada (Karlis & Ntzoufras 2003):** termo de covariância `λ₃ ≥ 0` (só correlação **positiva**; não modela negativa). Em benchmarks de RPS costuma **perder** para Dixon-Coles. **[B]**
- **xG-as-λ:** usar xG/xGA no lugar de gols históricos como λ — mais estável; RPS pré-jogo ~0,199 (Bundesliga). **[B]**

### 1.6 Benchmarks de acurácia
Em comparações com **RPS** (Ranked Probability Score, menor=melhor), **Dixon-Coles costuma ter o menor RPS**; bivariada o pior. RPS típico de bons modelos 1X2 ≈ **0,19–0,21**. **[B]**

---

## 2. Derivando os mercados + base rates

Monte `P(i,j)` para `i,j = 0..~10` (× `τ` no DC). Então:
- **`P(Over X.5) = Σ_{i+j>X} P(i,j)`** (Over 2.5 = soma de todas as células com `i+j ≥ 3`). **[A]**
- **`P(BTTS Sim) = Σ_{i≥1, j≥1} P(i,j) = 1 − e^{−λ} − e^{−μ} + τ(0,0)·e^{−(λ+μ)}`**. **[A]**

**Base rates:** média de gols por jogo nas grandes ligas **~2,5–3,1** (Bundesliga ~3,1 > Ligue 1 ~2,96 > PL ~2,93 > La Liga ~2,6 > Serie A ~2,56 em 24/25). **BTTS ~50–56%** (top-5 ≈ 54,9%). **[A]**

---

## 3. Inputs preditivos, ranqueados

### Total de gols (Over/Under)
1. **xG/xGA rolante e ajustado ao adversário** (de ambos) — domina; dirige `λ_casa + λ_fora`. xG é mais preditivo que gols passados (gols são raros/ruidosos; estabiliza após ~5 jogos). **[A]**
2. **Baseline de gols da liga** — diferenças grandes (2,56 → 3,14); nunca misture ligas sem re-normalizar. **[A]**
3. **Mando de campo:** +0,15 a +0,30 gols; **cai ~50% sem torcida** (estudos COVID). Infla `λ_casa`, empurra total e levemente o BTTS. **[A]**
4. **Forma com decaimento (ξ≈0,0065)** — prefira xG recente a resultados recentes (ruído). **[B]**
5. **Escalações/desfalques** (~1h antes do jogo): aplicar delta de xG por ausência confirmada. **[B]**
6. **Estilo (PPDA/pressão, bloco):** dois times abertos/pressão alta → total maior e BTTS maior. **[B]**
7. Congestão/fadiga (indireto via rotação), clima/árbitro (pequeno/ambíguo) — menor peso. **[C]**

### BTTS (assimétrico — precisa dos DOIS times)
`BTTS ≈ (1−e^{−λ})(1−e^{−μ})` → governado pelo **menor λ**.
1. **Ataque do time mais fraco × defesa do time mais forte** (o λ limitante) — input decisivo. **[A]**
2. **xGA de ambos (solidez defensiva)** — BTTS é mais sensível à defesa que o O/U. **[A]**
3. **Desfalque de ZAGUEIRO/goleiro** — eleva o λ limitante → maiores oscilações de BTTS (assimétrico vs total, onde falta de atacante pesa mais). **[B]**
4. Ataques de ambos (secundário); mando (modesto); estilo. **[B]**

> Assimetria-chave: **total premia a soma; BTTS premia o equilíbrio** (pune mismatch). Por isso métricas defensivas e notícias de zaga sobem no ranking do BTTS. **[A]**

---

## 4. Realidade do mercado (reality check)

- **Linha principal = O/U 2.5** (perto da mediana). Margens: livros afiados (Pinnacle) ~2–6%; recreativos 10–18%; **BTTS chega a ~10%** (derivado, menos líquido). **[B]**
- **Quase eficiente no fechamento.** Estudo (LSE/Int. J. Forecasting, **68.672 apostas em 12 anos** em O/U 2.5): yield de **~0,8% — só com odds máximas**; com odds médias, **prejuízo**. O edge vive no *odds-shopping* e em apostar cedo antes da convergência. **[A]**
- **CLV é o sinal de skill mais rápido** (significativo em ~50–100 apostas vs milhares no P&L). Benchmark = fechamento da Pinnacle (na Polymarket: preço de fechamento/último negociado). **[A]**
- **Edges documentados (todos com ressalva de overfit/survivorship):** Dixon-Coles 1997 (lucrativo só acima de um limiar de ~1% de discrepância, com odds da época mais "moles"); Goddard 2004 (lucro concentrado em **fim de temporada**, onde motivação é mal precificada); Constantinou-Fenton pi-ratings (metodologia robusta, mas edge fino vs fechamento). **PARX (Angelini-De Angelis) 35–100% = bandeira vermelha de overfit.** **[A/C]**
- **BTTS:** sem estudo peer-reviewed isolando yield; o edge documentado é **mispricing de base-rate em contextos atípicos** (ex.: BTTS de fase de grupos de Copa ~45,5% vs prior de liga ~54% → apostadores ancorados pagam caro no BTTS Sim). **[C]**
- **Anti-overfit:** backtest >20–30% ROI ≈ overfit/look-ahead; teste 100 estratégias e ~5 parecem lucrativas por acaso. Use walk-forward + White's Reality Check/Hansen SPA + odds de **fechamento** reais. **[A]**

---

## 5. Stack de dados (com flags de ToS)

| Camada | Fonte | Acesso / custo | Flag |
|---|---|---|---|
| **xG / chutes** | **FBref** (xG StatsBomb) + **Understat** (Big-5+RPL desde 14/15) via `soccerdata` (Python) | Grátis (scraping) | ToS de scraping (Sports Reference rate-limita; Understat zona cinza) **[A]** |
| **xG event-level** | **StatsBomb Open Data** (competições curadas) | Grátis (GitHub) | exige aceitar user-agreement + atribuição **[A]** |
| **xG NA** | American Soccer Analysis (MLS/NWSL/USL) | **API REST grátis, sem chave** (`itscalledsoccer`) | melhor opção grátis p/ Américas **[A]** |
| **Força das equipes** | **Club Elo** | **API grátis, sem chave** (`api.clubelo.com/<Clube>`) | mais limpa p/ feature de força contínua **[A]** |
| **Projeções (histórico)** | FiveThirtyEight SPI (`proj_score1/2`) | CSV no GitHub | **CONGELADO** (538 fechou em 03/2025) — só backtest, não ao vivo **[A]** |
| **Fixtures/resultados** | football-data.org (12 comps, grátis 10 req/min) ou TheSportsDB | Grátis (chave) | sem escalações no tier grátis do football-data.org **[B]** |
| **Escalações/lesões** | **API-Football** (`/fixtures/lineups`, `/injuries`, `/sidelined`) | Grátis 100 req/dia | XIs confirmados só **~1h antes**; p/ pré-dia precisa de "expected lineups" (pago) **[B]** |
| **Odds históricas O/U 2.5 (fechamento)** | **football-data.co.uk** (colunas `>2.5`/`<2.5`, `C` = closing) | Grátis (CSV) | **SEM coluna de BTTS** — ver abaixo **[A]** |
| **Odds históricas BTTS** | **The Odds API** (snapshots desde ~2020, inclui BTTS) | Pago (créditos) | rota limpa p/ BTTS; ou derive só o *resultado* BTTS dos gols (sem odds) **[B]** |
| **Clima** | **Open-Meteo** (por lat/long do estádio) | Grátis, sem chave | tier grátis **não-comercial**; efeito de clima no total é pequeno/ambíguo **[B]** |

**Libs:** `soccerdata` (FBref/Understat/ClubElo/football-data.co.uk — mantida, melhor porta de entrada), `understatapi`, `ScraperFC`, `worldfootballR` (R), `statsbombpy`, `itscalledsoccer`. **[A]**

> **Maior lacuna p/ ESTE modelo:** **odds históricas de fechamento de BTTS** — football-data.co.uk não tem. Confirme a profundidade do The Odds API p/ suas ligas, ou backteste BTTS pelo *resultado* derivado dos gols (sem CLV de BTTS na rota grátis). **[A]**

---

## 6. Validação e staking

- **Métricas:** **RPS** (Ranked Probability Score) p/ 1X2 ordenado; **Brier + log-loss** p/ binário (BTTS, O/U). Brier `=(1/N)Σ(p−o)²`; log-loss pune excesso de confiança. *(Debate: Wheatcroft 2021 defende o log/ignorance score sobre o RPS — registre os dois.)* **[A]**
- **Calibração:** reliability diagram (média prevista vs frequência observada na diagonal 45°) + ECE; recalibre com **Platt** (<1.000 pontos) ou **isotônica** (≥1.000), em fold dedicado. **[A]**
- **Backtesting:** **walk-forward** por temporada, **só info pré-jogo** (nada pós-escalação), precificando cada aposta na odd **real/de fechamento**, out-of-sample. **[A]**
- **CLV vs fechamento** = KPI primário (significativo em ~100 apostas) — mais rápido que ROI. **[A]**
- **Staking:** Kelly binário `f* = (p − c)/(1 − c)` p/ comprar YES a preço `c`. Em mercado ~par (c≈0,5), `f* ≈ 2·edge`. Use **meio-Kelly** (≈75% do crescimento, ~metade da variância) com os tetos da §2 do CLAUDE.md. **[A]**
- **Amostra/yield:** ~**1.000–2.000 apostas** p/ distinguir edge de variância; yield realista **2–5%** (e isso já é "sharp"). **[A]**
- **Polymarket:** `edge = P_modelo − preço − slippage`; sem de-vig. O preço líquido já é um prior afiado → exija buffer. **[A]**

---

## 7. Blueprint para o app (operação de futebol análoga à de MLB)

Encaixa no mesmo padrão `Scanner → modelo → edge → half-Kelly → previsões → dashboard`.

```
1. INGEST (pré-jogo)
   - Jogos do dia na Polymarket: o scanner de categoria (mercados fifwc-/epl-/... + slugs -total-/-btts-)
     — espelhar o que já fazemos p/ MLB (filtro por prefixo de liga + tipo de mercado).
   - xG/xGA por time (FBref/Understat via soccerdata) + Club Elo + média de gols da liga + mando.
   - Escalações/lesões (API-Football, ~1h antes); clima (Open-Meteo) opcional.

2. MODELO (Dixon-Coles)
   - λ_casa, λ_fora = f(ataque/defesa xG-based, baseline da liga, mando, decaimento ξ).
   - Matriz P(i,j) com correção τ (ρ ajustado).
   - P(Over linha) = soma da cauda;  P(BTTS) = 1 − e^{-λ} − e^{-μ} + τ(0,0)e^{-(λ+μ)}.

3. EDGE
   - edge = P_modelo(lado) − preço_Polymarket − fee;  classificável (news/model-driven).
   - filtros: faixa de payout (ex. 1.60–3.0x como na MLB), edge ≥ limiar, pré-jogo, liquidez.

4. SIZE  → meio-Kelly + tetos da §2 (2% model/news, 1% estreia).

5. EXECUTE (paper) + LOG  → reusar a tabela de previsões (status PENDENTE→ACERTO/ERRO),
   guardando o mercado (over/under/btts), a linha, μ_casa/μ_fora, ρ, e o link Polymarket.

6. REVIEW  → Brier/log-loss + calibração + CLV vs preço de fechamento.
```

**Decisões recomendadas:**
- **Skill nova `polymarket-soccer-goals`** com um engine `dixon_coles.py` (Poisson+τ+decay, derivação de Over/BTTS) — espelhando `run_distribution.py` da MLB, mas sem NegBin.
- **Dois mercados:** total de gols (Over/Under na linha 2.5 e alternativas) **e** BTTS (Sim/Não). Tratar BTTS com o ranking de inputs assimétrico (defesa pesa mais).
- **Reaproveitar** o dashboard (abas Análises/Resultados), a tabela de previsões e a liquidação cruzada — só adicionar o tipo de mercado e o engine de futebol.
- **Baseline aberto sem ToS:** Club Elo (grátis) + média de gols da liga; xG via soccerdata (flag de scraping). Fallback implícito do mercado quando faltar dado (nunca fabricar edge), como na MLB.

---

## 8. Lacunas e ressalvas

- **Sem odds de fechamento de BTTS grátis** (football-data.co.uk não tem) → backtest de CLV de BTTS exige The Odds API (pago) ou só o resultado derivado. **[A]**
- **Escalações confirmadas só ~1h antes** — p/ pré-dia, use "expected lineups" (pago) ou rode/atualize o λ perto do jogo. **[B]**
- **Efeito de motivação/derby, clima, árbitro sobre GOLS** é pouco evidenciado — pesos baixos/baixa confiança. **[C]**
- **538 SPI está morto** (não usar ao vivo). **[A]**
- **Edge real é pequeno (~1%)** e mercado quase eficiente; qualquer modelo sugerindo >10% de yield é overfit até prova out-of-sample contra fechamento. Validação em paper por ~1.000+ entradas (Brier/CLV) antes de capital real. **[A]**
- Várias páginas primárias deram 403 ao fetch automático; números vêm de índices de busca cruzados (ex.: τ de DC e λ₃ corroborados em 4+ fontes). Confira decimais na fonte antes de codar. **[A]**

---

## 9. Fontes principais (deduplicadas)

**Modelos:** Maher 1982 (Statistica Neerlandica); Dixon & Coles 1997 (JRSS-C, rss.onlinelibrary.wiley.com/doi/abs/10.1111/1467-9876.00065); Karlis & Ntzoufras 2003 (stat-athens.aueb.gr/~jbn/papers2/08_Karlis_Ntzoufras_2003_RSSD.pdf); dashee87.github.io (DC + time-weighting, ξ=0.0065); pena.lt/y (DC em Python; comparação de RPS); arXiv 2203.07531 (dispersão EURO 2020).

**Inputs:** americansocceranalysis.com (xG é o melhor preditor); FiveThirtyEight SPI README (gols ajustados + xG + nsxG); thedatabetics.com (gols/jogo por liga); ScienceDaily 2021 / PMC8566522 (mando sem torcida); dashee87 (decaimento); Frontiers/arXiv 2311.13707 (Bayes-xG por jogador); premierleague.com (PPDA).

**Mercado:** ScienceDirect/LSE (S0169207019302559 — yield ~0,8% em O/U 2.5); Dixon-Coles 1997; Goddard 2004 (for.877); Constantinou-Fenton pi-football; Buchdahl/Pinnacle (CLV); football-data.co.uk/blog/pinnacle_efficiency.

**Dados:** github.com/probberechts/soccerdata; github.com/statsbomb/open-data; clubelo.com; api-football.com; football-data.co.uk/data.php; the-odds-api.com/historical-odds-data; open-meteo.com.

**Validação/staking:** Constantinou & Fenton 2012 (RPS); Wheatcroft arXiv 1908.08980 (contra o RPS); en.wikipedia.org/wiki/Kelly_criterion; rebelbetting.com/faq/p-value; help.outlier.bet (de-vig); navnoorbawa.substack.com (math de prediction markets binários).

---

*Pesquisa para construção de modelo em modo paper — não é recomendação financeira. Operação real envolve risco de perda.*
