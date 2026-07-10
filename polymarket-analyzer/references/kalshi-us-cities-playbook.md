# Kalshi — playbook por cidade (20 cidades dos EUA)

Pesquisa dedicada (2026-07-10), um agente de pesquisa independente por cidade
+ verificação adversarial (3 votos) da estação/fonte de resolução — a peça
mais crítica, já que errar a estação usada pela Kalshi invalida qualquer
previsão, por melhor que seja o modelo climático. Motivada pelo plano de
migrar as 20 cidades já operadas na Polymarket para a Kalshi (ver
`verify_kalshi_weather.py` e a comparação Kalshi-vs-Polymarket desta mesma
sessão).

**Isto é um documento de referência — nenhuma mudança em
`weather-cities.json`, no bot, no judge, ou em qualquer script foi feita a
partir desta pesquisa.** Segue o mesmo padrão de
`south-america-research-notes.md`: achados documentados, decisão de
integração pendente do operador.

## Status da verificação

- **20/20 cidades** têm pesquisa completa (fontes de forecast, padrões
  climáticos locais, fontes de notícia/insider, estação de resolução Kalshi).
- **20/20 cidades** tiveram a estação de resolução adicionalmente checada por
  3 verificadores adversariais independentes cada — completo, em 3 rodadas
  (bateu no limite de sessão do workflow duas vezes; retomado a partir do
  `runId` salvo até concluir todos os 80 agentes sem erro).
- Bloqueio recorrente em todos os agentes: `kalshi.com`, `help.kalshi.com` e
  `forecast.weather.gov` retornam HTTP 403 para fetch automatizado dentro
  deste ambiente (confirmado como bloqueio de política de proxy no nível de
  CONNECT, não anti-bot da Kalshi/NWS). O bucket S3
  `kalshi-public-docs.s3.amazonaws.com/contract_terms/` **não é bloqueado** —
  toda confirmação por fonte primária nesta pesquisa veio de PDFs baixados
  diretamente desse bucket.
- Achado estrutural adicional (via arquivamento CFTC de Nova Orleans): para
  cidades sem PDF dedicado, a Kalshi mantém o mapeamento cidade→estação num
  **"Appendix C (Confidential)"** dos seus filings regulatórios — ou seja,
  não é apenas que os verificadores não encontraram o documento; a Kalshi
  **não divulga publicamente** essa informação para essas cidades. A única
  forma de confirmar é lendo a aba "Rules" da página do mercado ao vivo.

### Achado estrutural: nem toda cidade tem um PDF de regras dedicado

Verificadores fizeram uma varredura completa do bucket S3 (3347+ chaves) e
encontraram dois regimes distintos:

- **Cidades com PDF de regras dedicado** (nomeiam a estação explicitamente em
  texto): Nova York (`NHIGH.pdf`), Chicago (`CHIHIGH.pdf`), Houston
  (`HOUHIGH.pdf`), Denver (`DENHIGH.pdf`), Miami (`MIAHIGH.pdf`), Los Angeles
  (`LAHIGH.pdf` **e** `LAXHIGH.pdf` — ver nota de ambiguidade abaixo),
  Filadélfia (`PHILHIGH.pdf`), Austin (`AUSHIGH.pdf`). Nessas, a estação foi
  confirmável por fonte primária direta.
- **Cidades SEM PDF dedicado** (Atlanta, San Antonio, Oklahoma City, Las
  Vegas, San Francisco, Seattle, Dallas, e provavelmente outras não
  testadas): caem num **template genérico** (`HIGHTEMP.pdf`, `CITYLOW.pdf`,
  `CITIESWEATHER.pdf`, `LOCALTEMPERATURE.pdf`, ou o mais recente
  `GLOBALTEMPERATURE.pdf`, autocertificado à CFTC em dez/2025) que usa
  placeholders (`<station>`/`<city>`) — a estação exata só é especificada na
  aba "Rules" da página **ao vivo** do mercado específico, que fica bloqueada
  neste ambiente. Para essas cidades, a hipótese de estação continua sendo a
  mais provável (nenhum verificador achou candidata concorrente, exceto
  Dallas — ver abaixo), mas **não é confirmável sem acesso humano ao site**.

## Achado mais importante: divergência de estação Polymarket → Kalshi

A Kalshi **não usa necessariamente a mesma estação que a Polymarket** para a
mesma cidade. Confirmado com evidência de fonte primária (PDF oficial de
`contract_terms` da própria Kalshi):

| Cidade | Estação Polymarket (bot hoje) | Estação Kalshi (confirmada por PDF primário) | Status |
|---|---|---|---|
| **Nova York** | KLGA (LaGuardia) | **KNYC (Central Park)** | ⚠️ DIVERGE — confirmado 3-0, `NHIGH.pdf` |
| **Chicago** | KORD (O'Hare) | **KMDW (Midway)** | ⚠️ DIVERGE — confirmado 3-0, `CHIHIGH.pdf` |
| **Houston** | não configurado | **KHOU (Hobby)**, não IAH | confirmado 3-0, `HOUHIGH.pdf` |
| Los Angeles | KLAX | KLAX (mas ver ambiguidade Downtown vs. Airport abaixo) | confirmado 3-0 |
| Phoenix | KPHX | KPHX (Sky Harbor) | confirmado 2-1 (ticker real: `KXHIGHTPHX`) |
| Denver | não configurado | **KDEN** (Denver Intl) | confirmado 3-0, `DENHIGH.pdf` |
| Filadélfia | não configurado | KPHL | confirmado 3-0, `PHILHIGH.pdf` |
| Washington DC | não configurado | KDCA (Reagan National) | confirmado 3-0 |
| Miami | KMIA | KMIA (mesma estação) | confirmado 3-0 |
| Boston | KBOS | KBOS (mesma estação) | confirmado 2-1 |
| Austin | não configurado | **KAUS (Bergstrom)**, não Camp Mabry (KATT) | confirmado por PDF `AUSHIGH.pdf` — ambiguidade original resolvida |

**Nota sobre Los Angeles**: o bucket S3 tem **dois** documentos de regras
distintos — `LAHIGH.pdf` (estação = "Downtown Los Angeles, CA") e
`LAXHIGH.pdf` (estação = "Los Angeles Airport, CA"). São contratos/tickers
diferentes para a mesma cidade. O ticker atualmente observado
(`KXHIGHLAX`) e o nome do arquivo (`LAXHIGH`) sugerem fortemente o aeroporto,
mas **isso não foi 100% descartado** — confirmar na aba "Rules" antes de
operar.

### Cidades sem confirmação de fonte primária (sem PDF dedicado no S3)

Nestes casos os 3 verificadores concordaram entre si que a hipótese
provavelmente está certa (nenhum encontrou estação concorrente), mas nenhum
conseguiu confirmá-la contra um documento oficial da Kalshi — por isso o
workflow marcou como "disputed" (a instrução de verificação exige
`agrees=false` quando não há confirmação primária, mesmo sem evidência
contrária). Tratar como **hipótese forte, não fato confirmado**:

| Cidade | Estação Polymarket (bot hoje) | Estação Kalshi (hipótese sem PDF, sem contradição encontrada) | Confiança |
|---|---|---|---|
| Atlanta | KATL | KATL (mesma estação) — sem estação concorrente plausível na região | 0.6 |
| Seattle | KSEA | KSEA (mesma estação) — corroboração adicional forte (mirror independente da UW, `CLISEA`) | 0.75 |
| San Antonio | não configurado | KSAT (mas ticker real é `KXHIGHTSATX`, não `KXHIGHSAT`) | 0.55 (mais baixa de todas) |
| Oklahoma City | não configurado | KOKC — corroboração adicional via CFTC filing + IEM independente | 0.7 |
| Las Vegas | não configurado | KLAS — **ver risco regulatório abaixo** (litígio Nevada vs. Kalshi) | 0.65 |
| San Francisco | KSFO | KSFO — ticker de mínima `KXLOWTSFO` confirmado real | 0.65 |

### Dallas — a única divergência genuína de incerteza (não apenas falta de PDF)

Diferente das cidades acima, os 3 verificadores de Dallas fizeram uma
descoberta que **enfraquece, e possivelmente inverte**, a hipótese original
(`KDFW`). O argumento de precedente citado pela pesquisa original
("Chicago→Midway em vez de O'Hare, Houston→Hobby em vez de Bush
Intercontinental, logo Dallas→DFW em vez de Love Field") foi checado com
rigor por dois verificadores independentes e **aponta na direção oposta**:
em Chicago e Houston, a Kalshi escolheu o aeroporto **menor e mais central**
(Midway, Hobby), não o grande hub internacional periférico (O'Hare, Bush
Intercontinental). Em Dallas, é o **DFW que é o hub grande/periférico**
(~88.8M passageiros/ano, único ponto de entrada internacional) e o **Love
Field (KDAL, já usado pelo bot) que é o aeroporto menor/mais central** — os
papéis estão invertidos em relação a Chicago/Houston. Aplicando a mesma
lógica com rigor, a analogia favoreceria **KDAL**, não KDFW. Nenhum PDF
dedicado de Dallas existe no bucket S3 (testado exaustivamente). **Tratar
Dallas como genuinamente não resolvido — não presumir nem KDAL nem KDFW sem
ler a aba "Rules" do mercado `kxhightdal` diretamente.**

## Padrão geral de resolução Kalshi (confirmado, vale para as 20 cidades)

Confirmado por leitura direta de múltiplos PDFs oficiais (legados e os
templates genéricos atuais `HIGHTEMP.pdf`/`CITYLOW.pdf`/
`CITIESWEATHER.pdf`/`GLOBALTEMPERATURE.pdf`): a Kalshi liquida **sempre**
pelo **NWS Daily Climate Report / Climatological Report (Daily)** — produto
`CLI` emitido pelo escritório local do NWS — nunca por METAR bruto, média
horária, ou agregadores tipo Weather Underground/AccuWeather/Google Weather
(que a Polymarket historicamente usa, segundo `wethr.net`). Cláusula de
contingência: a determinação pode atrasar até ~11h ET se a máxima do CLI for
inconsistente com os extremos de 6h/24h do METAR, ou se o valor final for
menor que um relatório preliminar anterior — **confirmada verbatim apenas no
PDF de Nova York**; não apareceu nos PDFs específicos de Chicago/Houston, o
que sugere que pode ser uma cláusula do rulebook mestre (aplicável a todos)
em vez de duplicada por cidade, mas isso não foi confirmado. O dia
climatológico segue **hora padrão local (LST) o ano todo**, mesmo durante o
horário de verão — durante o DST a janela do "dia" de liquidação não é
meia-noite–meia-noite, mas sim 01:00–00:59 do dia seguinte no relógio local.

**O prefixo de ticker NÃO é uniforme entre cidades** — não assumir um padrão
fixo tipo "KXHIGHT+código": Miami usa `KXHIGHMIA` (sem "T"), Chicago tem
ticker legado `HIGHCHI`, San Antonio usa `KXHIGHTSATX` (código "SATX", não
"SAT"), Phoenix usa `KXHIGHTPHX` (com "T"). Confirmar o ticker exato por
cidade antes de codificar qualquer lógica de discovery baseada em padrão.

Isso significa que **qualquer lógica cross-platform herdada do fluxo
Polymarket precisa ser revalidada por cidade**, não só copiada — tanto a
estação quanto a fonte de dado podem diferir.

## Risco regulatório específico: Las Vegas / Nevada

Um verificador encontrou, de forma incidental, evidência de **litígio ativo
entre o estado de Nevada e a Kalshi** sobre jurisdição de jogos de azar, com
decisões determinando que a Kalshi pare de oferecer contratos no estado.
Isso é um risco **adicional e distinto** do risco de estação — mesmo que
KLAS seja a estação correta, o mercado pode não estar disponível/operacional
para usuários/contratos ligados a Nevada. Verificar o status regulatório
atual antes de configurar Las Vegas para operar.

---

## Cidades confirmadas com fonte primária (PDF oficial da Kalshi)

### Nova York (NYC) — confiança 0.93, confirmado 3-0
- **Resolução**: KNYC (Central Park), via NWS Daily Climate Report, WFO OKX. **Diverge de KLGA** (usado hoje na Polymarket).
- **Forecast**: NBM, LAMP, HRRR, MOS, AFD do OKX (forecast.weather.gov, site=OKX).
- **Padrões críticos**: interação brisa marítima × ilha de calor urbana (timing de poucas horas muda a máxima em vários °F); frentes "backdoor" do Atlântico/nordeste; divergência documentada entre Central Park/LaGuardia/JFK/Newark no mesmo evento (ex.: LaGuardia bateu recorde de temperatura de meia-noite em jul/2026 enquanto Central Park empatou o recorde histórico de 1966).
- **Fontes de edge**: John Homenuk (@nymetrowx, New York Metro Weather), NY1 Weather, Hope Osemwenkhae (@weatherwithhope).
- **Aberto**: ticker do mercado horário direcional (padrão `KXTEMPNY`?) não confirmado — API/site bloqueados neste ambiente.

### Los Angeles — confiança 0.75, confirmado 3-0
- **Resolução**: KLAX, via NWS CLI, WFO LOX. Ticker `KXHIGHLAX` confirmado ativo. **Ambiguidade residual**: existe também `LAHIGH.pdf` (estação "Downtown Los Angeles") no bucket — confirmar qual dos dois contratos o ticker atual usa antes de operar.
- **Forecast**: NBM, LAMP, HRRR, AFD do LOX.
- **Padrões críticos**: "June Gloom"/"May Gray" (dissipação da camada marinha — maior fonte de erro); Catalina Eddy; ventos Santa Ana (podem elevar a máxima 10-20°F+ em poucas horas, mal cronometrados pelos modelos); LAX fica rente à costa, muito mais sensível a brisa marítima que o Downtown/Vale de San Fernando.
- **Fontes de edge**: Weather West (Dr. Daniel Swain, UCLA) — referência mais citada para padrões climáticos da Califórnia.
- **Nota**: dia de liquidação em Local Standard Time o ano todo (desloca 1h durante DST).

### Chicago — confiança 0.93, confirmado 3-0
- **Resolução**: **KMDW (Midway)**, via NWS Daily Climate Report, WFO LOT. **Diverge de KORD** (usado hoje na Polymarket) — confirmado por leitura direta do PDF `CHIHIGH.pdf`.
- **Forecast**: NBM, LAMP/MOS calibrados por estação, HRRR, AFD do LOT, modelo experimental WRF do próprio WFO Chicago.
- **Padrões críticos**: brisa do Lago Michigan (pode "travar" a maxima perto do lago enquanto Midway, mais interior, continua esquentando); Midway registra sistematicamente mais dias de 90°F+ que O'Hare (diferença de até 10 dias/ano); Alberta clippers/vórtice polar; neve de efeito lago.
- **Fontes de edge**: WGN-TV (legado Tom Skilling), Midwest Weather (Substack), NBC5 Storm Team 5.
- **Correções confirmadas**: ticker legado correto é `CHIHIGH` (não `HIGHCHI`); ticker de mínima atual é `KXLOWTCHI` (não `KXLOWCHI`); a cláusula de atraso de ~11h ET (vista no PDF de NYC) **não** aparece verbatim no PDF de Chicago.

### Houston — confiança 0.9, confirmado 3-0
- **Resolução**: **KHOU (Hobby)**, não Bush Intercontinental (KIAH) — confirmado por PDF `HOUHIGH.pdf`. Cidade não configurada hoje no bot.
- **Forecast**: NBM, LAMP, HRRR (crítico para timing de brisa do Golfo/Baía de Galveston), AFD do HGX.
- **Padrões críticos**: brisa marítima/de baía (principal fator de incerteza, ±1-3°F conforme timing); "blue northers" (quedas de 20-40°F em horas); nevoeiro de radiação alimentado por umidade do Golfo; temporada de furacões (jun-nov).
- **Fontes de edge**: **Space City Weather** (Eric Berger + Matt Lanza) — referência independente mais confiável da região, pico de 1M de acessos/dia durante o furacão Harvey.
- **Aberto**: estação da mínima diária (KXLOWTHOU) confirmada como ticker real, mas a estação (presumida Hobby por simetria) continua sem PDF dedicado — Kalshi usa template genérico `CITYLOW.pdf` para mínimas de todas as cidades.

### Phoenix — confiança 0.72, confirmado 2-1
- **Resolução**: KPHX (Sky Harbor, mesma estação já usada), via NWS CLI, WFO PSR. Ticker real confirmado: `KXHIGHTPHX` (não `KXHIGHPHX` como inferido inicialmente). Não existe PDF de regras dedicado a Phoenix no bucket S3 — usa o template genérico `HIGHTEMP.pdf`.
- **Forecast**: NBM, HRRR (crítico na monção), LAMP/MOS, AFD do PSR.
- **Padrões críticos**: uma das maiores ilhas de calor urbanas do mundo (10-14°F vs. área rural); monção do sudoeste (jun-set, padrão "bursts"/"breaks"); outflow boundaries e haboobs (tempestades de poeira, podem derrubar a temperatura em minutos); inversão térmica de inverno no vale.
- **Fontes de edge**: Amber Sullins (ABC15, @AmberSullins), Sean McLaughlin (Arizona's Family).

### Denver — confiança 0.93, confirmado 3-0
- **Resolução**: **KDEN** (Denver Intl Airport, não Stapleton/downtown) — confirmado por PDF `DENHIGH.pdf`, WFO BOU. Cidade não configurada hoje no bot.
- **Forecast**: NBM (viés conhecido de subestimar TMAX em dias quentes), HRRR (crítico para Denver Cyclone/DCVZ), radiossondagem de Denver/Boulder, mesonets regionais (CoAgMet).
- **Padrões críticos**: ventos Chinook (elevam a temperatura 30-60°F em <36h, mal resolvidos por modelos); Denver Convergence Vorticity Zone (dispara tempestades vespertinas abruptamente, às vezes em ~90min); cold air damming/upslope; estação oficial fica ~19km do centro (mais exposta/ventosa, menos amortecida por UHI que "a sensação no centro").
- **Fontes de edge**: Weather5280, BoulderCAST.
- **Correção confirmada**: ticker de mínima é `KXLOWTDEN` (não `KXLOWDEN`).

### Filadélfia (Philadelphia) — confiança 0.6→confirmado 3-0
- **Resolução**: KPHL, via NWS Daily Climate Report — confirmado por PDF dedicado `PHILHIGH.pdf` (achado novo: não existia na pesquisa original, localizado pelos verificadores). WFO PHI (Philadelphia/Mount Holly, fisicamente em Mount Holly, NJ). Cidade não configurada hoje.
- **Forecast**: NBM (viés documentado de **subestimar** a máxima em dias de calor extremo na região, segundo blog local), HRRR, LAMP, AFD do PHI.
- **Padrões críticos**: brisa marítima da Baía de Delaware; Fall Line Piemonte×Coastal Plain gera gradiente térmico entre subúrbios e o aeroporto; frentes "backdoor".
- **Fontes de edge**: theweatherguy.net (discute explicitamente vieses do NBM para a região).

### Washington DC — confiança 0.8, confirmado 3-0
- **Resolução**: KDCA (Reagan National), NWS CLI, WFO LWX (Sterling/Baltimore-Washington). Cidade não configurada hoje.
- **Padrões críticos**: ilha de calor urbana centrada exatamente na estação de liquidação (KDCA é ~10-15°F mais quente à noite que Dulles, a 40km); cold air damming/frentes "backdoor"; heat domes (recorde histórico de 102°F em jul/2026, batendo marca de 1898).
- **Fontes de edge**: **Capital Weather** (ex-Capital Weather Gang do Washington Post, se desligou do jornal em mai/jun-2026 e virou independente — Jason Samenow, Ian Livingston).

### Miami — confiança 0.7, confirmado 3-0
- **Resolução**: KMIA (mesma estação já usada), NWS CLI ("CLIMIA"), WFO MFL.
- **Padrões críticos**: brisa dupla Atlântico×Golfo com "efeito zíper" (convergência dispara convecção vespertina — maior fator de erro na estação chuvosa); ilha de calor com padrão espacial invertido (aeroporto no interior aquece mais que a orla); temporada de furacões.
- **Fontes de edge**: John Morales (NBC6), Michael Lowry ("Eye on the Tropics", Substack, ex-NHC).

### Boston — confiança 0.7, confirmado 2-1
- **Resolução**: KBOS (Logan, mesma estação já usada), NWS CLI, WFO BOX.
- **Padrões críticos**: brisa marítima (Logan é uma das estações ASOS mais expostas a influência marítima do país — pode ficar 10-15°F mais frio que o interior); frentes "backdoor"; nevoeiro de advecção primavera/início de verão; nor'easters.
- **Fontes de edge**: Dave Epstein (Boston Globe/GBH, @growingwisdom), Eric Fisher (WBZ-TV), Woods Hill Weather.

---

## Cidades sem PDF dedicado (hipótese forte, sem confirmação primária)

### Seattle — confiança 0.75, disputed 1-2 (sem contradição — apenas sem PDF)
- **Resolução (hipótese)**: KSEA (Sea-Tac, mesma estação já usada), NWS CLI, WFO SEW, identificador de produto `CLISEA` — corroborado por um mirror independente do Dept. de Atmospheric Sciences da UW (não afiliado à Kalshi/wethr.net).
- **Padrões críticos**: camada de stratus marinho/"burn-off" matinal; calha térmica costeira + escoamento offshore (mecanismo por trás do "heat dome" de 2021, quando a máxima foi subestimada em 2-5°C mesmo com 1-3 dias de antecedência); Zona de Convergência de Puget Sound; "marine push".
- **Fontes de edge**: Cliff Mass Weather Blog (UW, roda modelo WRF regional próprio de até 1,3km), Scott Sistek/Emerald City Weather.

### Atlanta — confiança 0.6, disputed 0-3 (sem contradição — apenas sem PDF)
- **Resolução (hipótese)**: KATL (mesma estação já usada), NWS CLI, WFO FFC. Sem estação concorrente plausível na região metro (ao contrário de Chicago/Houston/Dallas, Atlanta não tem um segundo aeroporto comercial de porte comparável).
- **Padrões críticos**: cold air damming/"the wedge" (represamento de ar frio contra os Apalaches — um dos padrões mais difíceis da região); tempestades pop-up vespertinas (maior fator de erro no verão); remanescentes de furacões (ex.: Helene, set/2024).
- **Fontes de edge**: North Georgia Weather (forum), ForecastAdvisor (rastreia acurácia histórica por provedor).

### San Antonio — confiança 0.55 (a mais baixa de todas as 20), disputed 0-3
- **Resolução (hipótese)**: KSAT, NWS CLI, WFO EWX. Cidade não configurada hoje.
- **Correção confirmada**: o ticker real é `KXHIGHTSATX` (código de cidade "SATX", não "SAT" como inferido originalmente).
- **Padrões críticos**: fronteira Balcones Escarpment/Edwards Plateau; dryline e quebra de cap convectivo; seca plurianual associada a La Niña (viés de maxima acima da climatologia normal).
- **Fontes de edge**: nenhum blog independente dedicado encontrado (lacuna identificada, diferente de Houston/Space City Weather) — depender de TV local (KSAT, WOAI, KENS5) e NWS EWX.

### Oklahoma City — confiança 0.7, disputed 1-2 (sem contradição — apenas sem PDF)
- **Resolução (hipótese)**: KOKC, NWS CLI, WFO Norman (OUN). Corroborado por checagem independente do produto CLIOKC via Iowa Environmental Mesonet (arquivo acadêmico, não afiliado à Kalshi) e pelo texto do CFTC filing `CITIESWEATHER.pdf`/`CITYLOW.pdf`. Cidade não configurada hoje.
- **Padrões críticos**: dryline; jato de baixos níveis noturno + MCS (nuvens residuais podem suprimir inesperadamente o aquecimento diurno); "blue northers"; retroalimentação de umidade do solo após convecção.
- **Fontes de edge**: Oklahoma Mesonet (rede de 120 estações, observação a cada 5 min), Rick Smith (NWS Norman, @ounwcm).

### Las Vegas — confiança 0.65, disputed 0-3 (sem contradição — apenas sem PDF)
- **Resolução (hipótese)**: KLAS (Harry Reid Intl), NWS CLI, WFO VEF. Cidade não configurada hoje.
- **⚠️ Risco adicional identificado**: litígio ativo Nevada vs. Kalshi sobre jurisdição de jogos de azar — verificar status regulatório antes de configurar este mercado.
- **Padrões críticos**: bacia/vale cercado por montanhas (drenagem de ar frio noturno); monção norte-americana (jul-set, maior fonte de incerteza de máxima); grande amplitude térmica diária típica de deserto.
- **Fontes de edge**: NWS Las Vegas (@NWSVegas), Sam Argier (FOX5 Vegas).

### San Francisco — confiança 0.65, disputed 0-3 (sem contradição — apenas sem PDF)
- **Resolução (hipótese)**: KSFO (mesma estação já usada), NWS CLI, WFO Monterey (MTR). Ticker de mínima `KXLOWTSFO` confirmado como real (resolvendo incerteza anterior).
- **Padrões críticos**: camada marinha/"Karl the Fog" (maior fator — máxima depende quase inteiramente do horário de dissipação); brisa marítima canalizada pelo "San Bruno Gap"; ventos offshore tipo Diablo (apps de celular subestimam sistematicamente — caso documentado na imprensa de Google/Apple Weather errando enquanto NWS já sinalizava 80°F+); ciclo sazonal invertido (dias mais quentes do ano tipicamente em set/out, não jul/ago).
- **Fontes de edge**: **SFO Marine Stratus Forecast System** (ferramenta operacional dedicada NWS/MIT Lincoln Lab para prever dissipação do estrato — ferramenta de altíssimo valor, específica para esta estação), Jan Null (Golden Gate Weather Services), Daniel Swain (Weather West).

## Cidade com incerteza genuína (não apenas falta de PDF)

### Dallas — confiança rebaixada, disputed 0-3 (contradição real encontrada)
- **Resolução**: **NÃO RESOLVIDO**. Hipótese original era KDFW; a verificação encontrou que o argumento de precedente citado (Kalshi prefere o aeroporto menor/central sobre o hub grande) na verdade **favorece KDAL** (Love Field, já usado pelo bot) quando aplicado com rigor a Dallas — os papéis dos dois aeroportos são invertidos em relação a Chicago/Houston. Nenhum PDF dedicado existe no bucket S3. **Não presumir nem KDAL nem KDFW — ler a aba "Rules" do mercado `kxhightdal` diretamente antes de qualquer config.**
- **Padrões críticos**: dryline (posição decide setor quente/seco vs. úmido); "the cap" (força do tampão de inversão decide se convecção vespertina se forma); outflow boundaries de MCS noturno (pode gerar diferença de 10-20°F entre lados opostos da fronteira); "blue northers".
- **Fontes de edge**: Pete Delkus (WFAA), dfwweather.org.

---

## Austin — RESOLVIDO por fonte primária (correção à hipótese original)

### Austin — confiança 0.9, disputed 1-2 no voto binário, mas com confirmação primária real
- **Resolução**: **KAUS (Austin-Bergstrom International Airport)**, NÃO Camp Mabry (KATT) — a ambiguidade da pesquisa original (confiança 0.5, "genuinamente ambígua") foi **resolvida** por dois verificadores que localizaram e leram o PDF dedicado `AUSHIGH.pdf` no bucket S3 da Kalshi. Texto literal: *"the maximum temperature recorded... published in the National Weather Service's Daily Climate Report for Austin Bergstrom"*. Um dos verificadores também baixou o filing de autocertificação CFTC (Regra 40.2(a), assinado por Xavier Sottile, Head of Markets) que confirma o mesmo. O contrato de **mínima** (`KXLOWTAUS`) não tem PDF próprio (usa template genérico `CITYLOW.pdf`) — a mesma estação é uma inferência forte, não 100% documentada separadamente.
- **Correção confirmada**: o ticker `KXTEMPAUSH` citado como indício em pesquisa anterior desta sessão **foi refutado** — não existe. Tickers reais: `KXHIGHAUS` / `KXLOWTAUS`.
- **Padrões críticos**: dryline na fronteira Balcones Escarpment; "blue northers"; nebulosidade/neblina matinal por advecção do Golfo; diferença real de 3-5°F entre Camp Mabry e Bergstrom (mais pronunciada nas mínimas) — relevante mesmo com a estação já resolvida, pois qualquer forecast/climatologia usada precisa ser calibrada para Bergstrom especificamente, não Camp Mabry.
- **Fontes de edge**: Troy Kimmel (UT Austin, meteorologista de emissoras desde 1984), Bob Rose (LCRA Hydromet, cobre o Hill Country a montante).

## Cidades sem PDF dedicado (continuação — Nova Orleans e Minneapolis)

### Nova Orleans (New Orleans) — confiança 0.7, disputed 1-2 (sem contradição — apenas sem PDF)
- **Resolução (hipótese)**: KMSY, NWS CLI, WFO LIX (New Orleans/Baton Rouge). Sem PDF dedicado (testado exaustivamente) — e um verificador descobriu que o mapeamento cidade→estação para essas cidades é literalmente marcado como confidencial no arquivamento regulatório da Kalshi (Appendix C). Cidade não configurada hoje.
- **Padrões críticos**: convergência de dupla brisa (Lago Pontchartrain + Golfo — dispara pop-up storms vespertinas); ilha de calor urbana pronunciada (17ª pior entre 65 cidades dos EUA); baixa amplitude térmica diurna típica da Costa do Golfo (maxima mais sensível a pequenos erros de nebulosidade).
- **Fontes de edge**: Chris Franklin (WWL-TV), Jay Grymes (Climatologista Estadual da Louisiana).

### Minneapolis — confiança 0.6, disputed 0-3 (sem contradição — apenas sem PDF)
- **Resolução (hipótese)**: KMSP (Minneapolis-St. Paul Intl), NWS CLI, WFO Twin Cities/Chanhassen (MPX). Sem PDF dedicado (testado exaustivamente). Cidade não configurada hoje.
- **Correção confirmada**: o ticker real é `KXHIGHTMIN`/`KXLOWTMIN` (abreviação da cidade "MIN"), **não** `KXHIGHMSP`/`KXLOWMSP` como inferido por analogia na pesquisa original.
- **Ambiguidade residual não descartada**: St. Paul tem seu próprio aeroporto menor (KSTP, Holman Field/downtown) — situação estruturalmente análoga à ambiguidade Downtown-vs-Airport já documentada para Los Angeles. Não há evidência de que seja usado, mas também não foi descartado.
- **Padrões críticos**: clima continental extremo sem barreiras a massas árticas (irrupções costumam vir mais severas que os modelos globais sugerem); ilha de calor urbana na própria estação de liquidação; "corn sweat" (evapotranspiração agrícola eleva o ponto de orvalho 5-10°F em jul-ago, fenômeno específico do meio-oeste).
- **Fontes de edge**: Paul Douglas (Star Tribune, 40+ anos de experiência em MN), MPR News Updraft.
- **Nota**: o próprio pesquisador sugeriu, por analogia ao piloto africano deste bot, considerar `resolution_source: metar` como fallback de verdade-terra para esta cidade dado o nível de incerteza.

---

## Recomendações cross-cutting

1. **Antes de configurar QUALQUER cidade para operar na Kalshi**: confirmar a
   estação lendo a aba "Rules" do mercado ativo diretamente no site/app da
   Kalshi (bloqueado neste ambiente de pesquisa, mas acessível normalmente
   pelo operador). Isso é **obrigatório** para Dallas (única incerteza
   genuína e não resolvida: KDAL/KDFW) e Los Angeles (Downtown vs. Airport,
   ambiguidade residual), e recomendado para as 8 cidades sem PDF dedicado
   (Atlanta, Seattle, San Antonio, OKC, Las Vegas, SF, Nova Orleans,
   Minneapolis). Austin **já foi resolvido** por fonte primária (KAUS/
   Bergstrom) e não precisa dessa checagem adicional.
2. **Nunca reaproveitar cegamente a config de estação da Polymarket** para a
   Kalshi — confirmado que diverge em NYC e Chicago. O padrão de "estação
   secundária/mais central, não a maior/mais famosa" é real (Central
   Park≠LaGuardia, Midway≠O'Hare, Hobby≠Bush Intercontinental) — mas
   **não é uma regra mecânica infalível**: aplicado a Dallas, o mesmo
   raciocínio favorece a estação que JÁ está configurada (KDAL), não uma
   nova.
3. **NBM/LAMP/HRRR/AFD do WFO local são o quarteto comum a todas as 20
   cidades** — vale considerar como uma integração genérica (análoga ao que
   IPMA/met.no fizeram para a Europa), em vez de tratamento ad-hoc por
   cidade.
4. **O NWS Daily Climate Report (produto CLI) é a fonte universal de
   resolução** — nunca METAR bruto nem agregadores tipo Weather Underground
   (usado pela Polymarket). Isso abre a porta para uma rota de resolução
   `resolution_source: nws_cli` análoga ao `resolution_source: metar` já
   implementado no piloto africano, mas lendo o produto CLI real em vez de
   METAR.
5. **Prefixo de ticker não é uniforme** — confirmar caso a caso via API
   (`GET /trade-api/v2/series` ou `/markets?series_ticker=`) em vez de
   assumir um padrão fixo ao implementar discovery automático.
6. **10 cidades não têm nenhuma config hoje no bot** (Houston, Denver,
   Filadélfia, San Antonio, OKC, Las Vegas, DC, Nova Orleans, Minneapolis,
   Austin) — todas totalmente novas se a migração para Kalshi avançar; a
   estação já está resolvida com boa confiança para todas elas exceto San
   Antonio (0.55, a mais baixa) e as demais sem PDF dedicado.
7. **Verificação adversarial completa (20/20)** — não há mais pendência de
   research nesta pesquisa. O único item que continua genuinamente em aberto
   é a estação de Dallas.

## Caveats

- Nenhuma mudança de código/config foi feita a partir desta pesquisa.
- Confiança listada por cidade reflete o julgamento dos próprios agentes de
  pesquisa (0.0-1.0), não uma auditoria humana.
- "Disputed" no workflow não significa necessariamente "provavelmente
  errado" — na maioria dos casos (Atlanta, Seattle, San Antonio, OKC, Las
  Vegas, SF) significa apenas "sem PDF de regras dedicado para confirmar",
  com os verificadores concordando que a hipótese original continua sendo a
  mais provável. Dallas é a única exceção genuína, com contra-evidência real.
- Fontes de notícia/insider (contas de X, blogs, TV local) podem mudar de
  equipe/handle com o tempo — várias entradas já sinalizam isso
  explicitamente (ex.: WDSU New Orleans trocou de meteorologista-chefe 2x em
  menos de 1 ano).
- Este documento não substitui a checagem em tempo real da API/rules da
  Kalshi antes de qualquer trade real.

---

## ADENDO 2026-07-10 (pós-implementação): 20/20 confirmadas por `settlement_sources` da API

**A pendência acima está encerrada.** Durante o smoke do bot em host real, o
operador descobriu (com o `--sample` + `GET /series/{ticker}`) que o objeto
de SÉRIE da API pública carrega o campo `settlement_sources`, com a URL
exata do produto CLI de resolução — incluindo o WFO (`site=`) e a ESTAÇÃO
(`issuedby=`). Exemplo real:

```
GET /trade-api/v2/series/KXHIGHTSEA →
  "settlement_sources": [{
    "name": "NWS Climatological Report Seattle",
    "url": "https://forecast.weather.gov/product.php?site=SEW&product=CLI&issuedby=SEA"
  }]
```

Isso torna o "Appendix C confidencial" irrelevante: a estação é pública na
API, só não estava no `rules_primary` dessas cidades (que diz apenas
"recorded at Seattle"). Sweep completo do operador (33 séries — highs+lows
das 20 cidades) em 2026-07-10:

| Cidade | WFO | issuedby | Estação | Veredito vs pesquisa |
|---|---|---|---|---|
| Seattle | SEW | SEA | KSEA | hipótese confirmada |
| Atlanta | FFC | ATL | KATL | hipótese confirmada |
| San Antonio | EWX | SAT | KSAT | hipótese confirmada (era a de menor confiança, 0.55) |
| Oklahoma City | OUN | OKC | KOKC | hipótese confirmada |
| Las Vegas | VEF | LAS | KLAS | hipótese confirmada (⚠️ litígio Nevada segue relevante) |
| San Francisco | MTR | SFO | KSFO | hipótese confirmada |
| New Orleans | LIX | MSY | KMSY | hipótese confirmada |
| Minneapolis | MPX | MSP | KMSP | hipótese confirmada; ambiguidade KSTP descartada |
| **Dallas** | **FWD** | **DFW** | **KDFW** | **RESOLVIDO — contra o padrão "estação secundária"; NÃO é o KDAL do fluxo Polymarket** |
| Los Angeles | LOX | LAX | KLAX | ambiguidade Downtown vs Airport resolvida: **Airport** |
| NYC/Chicago/Houston/Phoenix/Denver/Philly/DC/Miami/Boston/Austin | — | NYC/MDW/HOU/PHX/DEN/PHL/DCA/MIA/BOS/AUS | conforme config | todas as 11 validadas, zero divergência |

Consequências aplicadas:
1. `references/kalshi-cities.json` expandido para **20 cidades** (as 9 novas
   com risk_notes deste playbook, `pilot: true`, séries high+low
   confirmadas; lows de LA/PHX/PHIL/MIA preenchidos — mesma estação do high).
2. Dallas entra com aviso destacado: climatologia/calibração do fluxo
   Polymarket é de KDAL e NÃO se aplica a KDFW sem recalibração.
3. Rota de confirmação recomendada daqui em diante: `GET /series/{ticker}` →
   `settlement_sources` (mais forte e mais barata que PDF/aba Rules).
