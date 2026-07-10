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
- **6/20 cidades** (NYC, LA, Chicago, Houston, Phoenix, Denver) tiveram a
  estação de resolução adicionalmente checada por verificadores adversariais
  independentes — a rodada de verificação **bateu no limite de sessão do
  workflow** (`session limit · resets 11pm UTC`) na metade e não foi possível
  retomá-la (duas tentativas de resume falharam com erro de infraestrutura
  transitório do harness, não relacionado à Kalshi).
- **14/20 cidades** (Seattle, Boston, Miami, Dallas, Filadélfia, Atlanta, San
  Antonio, OKC, Las Vegas, SF, DC, Nova Orleans, Austin, Minneapolis) têm
  apenas a pesquisa de um agente, sem a camada extra de verificação
  adversarial — tratar a confiança indicada como teto, não como piso.
- Bloqueio recorrente em todos os 20 agentes: `kalshi.com`, `help.kalshi.com`
  e `forecast.weather.gov` retornam HTTP 403 para fetch automatizado dentro
  deste ambiente (bloqueio de política de proxy/anti-bot, não uma falha da
  Kalshi). Quando um agente conseguiu confirmação de fonte primária, foi
  baixando diretamente os PDFs de `contract_terms/*.pdf` hospedados em
  `kalshi-public-docs.s3.amazonaws.com` (esse bucket S3 não é bloqueado).

## Achado mais importante: divergência de estação Polymarket → Kalshi

A Kalshi **não usa necessariamente a mesma estação que a Polymarket** para a
mesma cidade. Confirmado com evidência de fonte primária (PDF oficial de
`contract_terms` da própria Kalshi) para 3 das 6 cidades verificadas
adversarialmente:

| Cidade | Estação Polymarket (bot hoje) | Estação Kalshi (confirmada) | Status |
|---|---|---|---|
| **Nova York** | KLGA (LaGuardia) | **KNYC (Central Park)** | ⚠️ DIVERGE — confirmado 3-0, PDF `NHIGH.pdf` |
| **Chicago** | KORD (O'Hare) | **KMDW (Midway)** | ⚠️ DIVERGE — confirmado 3-0, PDF `CHIHIGH.pdf` |
| **Houston** | não configurado | **KHOU (Hobby)**, não IAH | confirmado 2-1, PDF `HOUHIGH.pdf` |
| Los Angeles | KLAX | KLAX | ✅ igual — confirmado 2-1 |
| Phoenix | KPHX | KPHX (Sky Harbor) | ✅ igual — confirmado 2-1 (ticker real: `KXHIGHTPHX`) |
| Denver | não configurado | **KDEN** (Denver Intl) | confirmado (1 voto, alta confiança), PDF `DENHIGH.pdf` |

Para as 14 cidades não re-verificadas, a pesquisa já identifica **candidatas
a divergência adicional** que precisam da mesma checagem antes de operar:

| Cidade | Estação Polymarket (bot hoje) | Estação Kalshi (hipótese, não verificada 2ª vez) | Confiança |
|---|---|---|---|
| **Dallas** | KDAL (Love Field) | **KDFW** (Dallas-Fort Worth Intl) | 0.7 — possível divergência, mesmo padrão de Chicago/Houston |
| Atlanta | KATL | KATL (mesma estação) | 0.6 |
| Seattle | KSEA | KSEA (mesma estação) | 0.75 |
| Boston | KBOS | KBOS (mesma estação) | 0.7 |
| Miami | KMIA | KMIA (mesma estação) | 0.7 |
| San Francisco | KSFO | KSFO (mesma estação) | 0.65 |
| Filadélfia | não configurado | KPHL | 0.6 |
| San Antonio | não configurado | KSAT | 0.55 (mais baixa de todas) |
| Oklahoma City | não configurado | KOKC | 0.7 |
| Las Vegas | não configurado | KLAS | 0.65 |
| Washington DC | não configurado | KDCA (Reagan National) | 0.8 |
| Nova Orleans | não configurado | KMSY | 0.7 |
| Austin | não configurado | **Ambíguo**: KATT (Camp Mabry) *ou* KAUS (Bergstrom) — fontes secundárias divergem | 0.3 na estação exata |
| Minneapolis | não configurado | KMSP | 0.6 |

**Ação recomendada antes de qualquer configuração em produção**: para cada
linha acima sem PDF primário confirmado, abrir a aba "Rules" do mercado ativo
na Kalshi (`kalshi.com/markets/kxhight<código>/...`) e ler o texto — resolve
a ambiguidade em segundos e é mais confiável que qualquer fonte secundária
citada aqui.

## Padrão geral de resolução Kalshi (confirmado, vale para as 20 cidades)

Confirmado por leitura direta de múltiplos PDFs oficiais (`NHIGH.pdf`,
`CHIHIGH.pdf`, `HOUHIGH.pdf`, `DENHIGH.pdf`, `MIAHIGH.pdf`,
`HIGHTEMP.pdf`/`CITYLOW.pdf` genéricos): a Kalshi liquida **sempre** pelo
**NWS Daily Climate Report / Climatological Report (Daily)** — produto `CLI`
emitido pelo escritório local do NWS — nunca por METAR bruto, média horária,
ou agregadores tipo Weather Underground/AccuWeather/Google Weather (que a
Polymarket historicamente usa, segundo `wethr.net`). Cláusula de contingência
recorrente: a determinação pode atrasar até ~11h ET se a máxima do CLI for
inconsistente com os extremos de 6h/24h do METAR, ou se o valor final for
menor que um relatório preliminar anterior. O dia climatológico segue **hora
padrão local (LST) o ano todo**, mesmo durante o horário de verão — ou seja,
durante o DST a janela do "dia" de liquidação não é meia-noite–meia-noite,
mas sim 01:00–00:59 do dia seguinte no relógio local.

Isso significa que **qualquer lógica cross-platform herdada do fluxo
Polymarket precisa ser revalidada por cidade**, não só copiada — tanto a
estação quanto a fonte de dado podem diferir.

---

## Cidades verificadas adversarialmente (alta confiança)

### Nova York (NYC) — confiança 0.93, confirmado 3-0
- **Resolução**: KNYC (Central Park), via NWS Daily Climate Report, WFO OKX. **Diverge de KLGA** (usado hoje na Polymarket).
- **Forecast**: NBM, LAMP, HRRR, MOS, AFD do OKX (forecast.weather.gov, site=OKX).
- **Padrões críticos**: interação brisa marítima × ilha de calor urbana (timing de poucas horas muda a máxima em vários °F); frentes "backdoor" do Atlântico/nordeste; divergência documentada entre Central Park/LaGuardia/JFK/Newark no mesmo evento (ex.: LaGuardia bateu recorde de temperatura de meia-noite em jul/2026 enquanto Central Park empatou o recorde histórico de 1966).
- **Fontes de edge**: John Homenuk (@nymetrowx, New York Metro Weather), NY1 Weather, Hope Osemwenkhae (@weatherwithhope).
- **Aberto**: ticker do mercado horário direcional (padrão `KXTEMPNY`?) não confirmado — API/site bloqueados neste ambiente.

### Los Angeles — confiança 0.75, confirmado 2-1
- **Resolução**: KLAX (mesma estação já usada), via NWS CLI, WFO LOX. Ticker `KXHIGHLAX` confirmado ativo.
- **Forecast**: NBM, LAMP, HRRR, AFD do LOX.
- **Padrões críticos**: "June Gloom"/"May Gray" (dissipação da camada marinha — maior fonte de erro); Catalina Eddy; ventos Santa Ana (podem elevar a máxima 10-20°F+ em poucas horas, mal cronometrados pelos modelos); LAX fica rente à costa, muito mais sensível a brisa marítima que o Downtown/Vale de San Fernando.
- **Fontes de edge**: Weather West (Dr. Daniel Swain, UCLA) — referência mais citada para padrões climáticos da Califórnia.
- **Nota**: dia de liquidação em Local Standard Time o ano todo (desloca 1h durante DST).

### Chicago — confiança 0.93, confirmado 3-0
- **Resolução**: **KMDW (Midway)**, via NWS Daily Climate Report, WFO LOT. **Diverge de KORD** (usado hoje na Polymarket) — confirmado por leitura direta do PDF `CHIHIGH.pdf`.
- **Forecast**: NBM, LAMP/MOS calibrados por estação, HRRR, AFD do LOT, modelo experimental WRF do próprio WFO Chicago.
- **Padrões críticos**: brisa do Lago Michigan (pode "travar" a maxima perto do lago enquanto Midway, mais interior, continua esquentando); Midway registra sistematicamente mais dias de 90°F+ que O'Hare (diferença de até 10 dias/ano); Alberta clippers/vórtice polar; neve de efeito lago.
- **Fontes de edge**: WGN-TV (legado Tom Skilling), Midwest Weather (Substack), NBC5 Storm Team 5.
- **Correção à pesquisa original**: ticker legado correto é `CHIHIGH` (não `HIGHCHI`); `NHIGH` é o contrato de Nova York, não de Chicago; ticker de mínima atual é `KXLOWTCHI` (com T).

### Houston — confiança 0.9, confirmado 2-1
- **Resolução**: **KHOU (Hobby)**, não Bush Intercontinental (KIAH) — confirmado por PDF `HOUHIGH.pdf`. Cidade não configurada hoje no bot.
- **Forecast**: NBM, LAMP, HRRR (crítico para timing de brisa do Golfo/Baía de Galveston), AFD do HGX.
- **Padrões críticos**: brisa marítima/de baía (principal fator de incerteza, ±1-3°F conforme timing); "blue northers" (quedas de 20-40°F em horas); nevoeiro de radiação alimentado por umidade do Golfo; temporada de furacões (jun-nov).
- **Fontes de edge**: **Space City Weather** (Eric Berger + Matt Lanza) — referência independente mais confiável da região, pico de 1M de acessos/dia durante o furacão Harvey.
- **Aberto**: estação da mínima diária (KXLOWTHOU) não confirmada por PDF primário, apenas por inferência de padrão.

### Phoenix — confiança 0.72, confirmado 2-1
- **Resolução**: KPHX (Sky Harbor, mesma estação já usada), via NWS CLI, WFO PSR. Ticker real confirmado: `KXHIGHTPHX` (não `KXHIGHPHX` como inferido inicialmente). Não existe PDF de regras dedicado a Phoenix no bucket S3 — usa o template genérico `HIGHTEMP.pdf`.
- **Forecast**: NBM, HRRR (crítico na monção), LAMP/MOS, AFD do PSR.
- **Padrões críticos**: uma das maiores ilhas de calor urbanas do mundo (10-14°F vs. área rural); monção do sudoeste (jun-set, padrão "bursts"/"breaks"); outflow boundaries e haboobs (tempestades de poeira, podem derrubar a temperatura em minutos); inversão térmica de inverno no vale.
- **Fontes de edge**: Amber Sullins (ABC15, @AmberSullins), Sean McLaughlin (Arizona's Family).

### Denver — confiança 0.93 (1 voto apenas, sessão esgotou antes dos outros 2)
- **Resolução**: **KDEN** (Denver Intl Airport, não Stapleton/downtown) — confirmado por PDF `DENHIGH.pdf`, WFO BOU. Cidade não configurada hoje no bot.
- **Forecast**: NBM (viés conhecido de subestimar TMAX em dias quentes), HRRR (crítico para Denver Cyclone/DCVZ), radiossondagem de Denver/Boulder, mesonets regionais (CoAgMet).
- **Padrões críticos**: ventos Chinook (elevam a temperatura 30-60°F em <36h, mal resolvidos por modelos); Denver Convergence Vorticity Zone (dispara tempestades vespertinas abruptamente, às vezes em ~90min); cold air damming/upslope; estação oficial fica ~19km do centro (mais exposta/ventosa, menos amortecida por UHI que "a sensação no centro").
- **Fontes de edge**: Weather5280, BoulderCAST.

---

## Cidades com pesquisa completa, sem verificação adversarial adicional

*(status "unverified" no workflow — confiança abaixo é a estimativa do
único pesquisador, não confirmada por um segundo agente independente)*

### Seattle — confiança 0.75
- **Resolução (hipótese)**: KSEA (Sea-Tac, mesma estação já usada), NWS CLI, WFO SEW.
- **Padrões críticos**: camada de stratus marinho/"burn-off" matinal; calha térmica costeira + escoamento offshore (mecanismo por trás do "heat dome" de 2021, quando a máxima foi subestimada em 2-5°C mesmo com 1-3 dias de antecedência); Zona de Convergência de Puget Sound; "marine push".
- **Fontes de edge**: Cliff Mass Weather Blog (UW, roda modelo WRF regional próprio de até 1,3km), Scott Sistek/Emerald City Weather.

### Boston — confiança 0.7
- **Resolução (hipótese)**: KBOS (Logan, mesma estação já usada), NWS CLI, WFO BOX.
- **Padrões críticos**: brisa marítima (Logan é uma das estações ASOS mais expostas a influência marítima do país — pode ficar 10-15°F mais frio que o interior); frentes "backdoor"; nevoeiro de advecção primavera/início de verão; nor'easters.
- **Fontes de edge**: Dave Epstein (Boston Globe/GBH, @growingwisdom), Eric Fisher (WBZ-TV), Woods Hill Weather.

### Miami — confiança 0.7
- **Resolução (hipótese)**: KMIA (mesma estação já usada), NWS CLI ("CLIMIA"), WFO MFL.
- **Padrões críticos**: brisa dupla Atlântico×Golfo com "efeito zíper" (convergência dispara convecção vespertina — maior fator de erro na estação chuvosa); ilha de calor com padrão espacial invertido (aeroporto no interior aquece mais que a orla); temporada de furacões.
- **Fontes de edge**: John Morales (NBC6), Michael Lowry ("Eye on the Tropics", Substack, ex-NHC).

### Dallas — confiança 0.7
- **Resolução (hipótese)**: **KDFW** (Dallas-Fort Worth Intl) — **possível divergência** do KDAL (Love Field) usado hoje na Polymarket, mesmo padrão de Chicago/Houston. NÃO confirmado por PDF primário (não localizado no bucket S3) — tratar com cautela extra.
- **Padrões críticos**: dryline (posição decide setor quente/seco vs. úmido); "the cap" (força do tampão de inversão decide se convecção vespertina se forma); outflow boundaries de MCS noturno (pode gerar diferença de 10-20°F entre lados opostos da fronteira); "blue northers".
- **Fontes de edge**: Pete Delkus (WFAA), dfwweather.org.

### Filadélfia (Philadelphia) — confiança 0.6
- **Resolução (hipótese)**: KPHL, NWS CLI, WFO PHI (Mount Holly, NJ). Cidade não configurada hoje.
- **Padrões críticos**: brisa marítima da Baía de Delaware; NBM tem viés documentado de **subestimar** a máxima em dias de calor extremo na região (relatado por blog local); Fall Line Piemonte×Coastal Plain gera gradiente térmico entre subúrbios e o aeroporto.
- **Fontes de edge**: theweatherguy.net (discute explicitamente vieses do NBM para a região).

### Atlanta — confiança 0.6
- **Resolução (hipótese)**: KATL (mesma estação já usada), NWS CLI, WFO FFC.
- **Padrões críticos**: cold air damming/"the wedge" (represamento de ar frio contra os Apalaches — um dos padrões mais difíceis da região); tempestades pop-up vespertinas (maior fator de erro no verão); remanescentes de furacões (ex.: Helene, set/2024).
- **Fontes de edge**: North Georgia Weather (forum), ForecastAdvisor (rastreia acurácia histórica por provedor).

### San Antonio — confiança 0.55 (a mais baixa de todas as 20)
- **Resolução (hipótese)**: KSAT, NWS CLI, WFO EWX. Cidade não configurada hoje.
- **Padrões críticos**: fronteira Balcones Escarpment/Edwards Plateau; dryline e quebra de cap convectivo; seca plurianual associada a La Niña (viés de maxima acima da climatologia normal).
- **Fontes de edge**: nenhum blog independente dedicado encontrado (lacuna identificada, diferente de Houston/Space City Weather) — depender de TV local (KSAT, WOAI, KENS5) e NWS EWX.

### Oklahoma City — confiança 0.7
- **Resolução (hipótese)**: KOKC, NWS CLI, WFO Norman (OUN). Cidade não configurada hoje.
- **Padrões críticos**: dryline; jato de baixos níveis noturno + MCS (nuvens residuais podem suprimir inesperadamente o aquecimento diurno); "blue northers"; retroalimentação de umidade do solo após convecção.
- **Fontes de edge**: Oklahoma Mesonet (rede de 120 estações, observação a cada 5 min), Rick Smith (NWS Norman, @ounwcm).

### Las Vegas — confiança 0.65
- **Resolução (hipótese)**: KLAS (Harry Reid Intl), NWS CLI, WFO VEF. Cidade não configurada hoje.
- **Padrões críticos**: bacia/vale cercado por montanhas (drenagem de ar frio noturno); monção norte-americana (jul-set, maior fonte de incerteza de máxima); grande amplitude térmica diária típica de deserto.
- **Fontes de edge**: NWS Las Vegas (@NWSVegas), Sam Argier (FOX5 Vegas).

### San Francisco — confiança 0.65
- **Resolução (hipótese)**: KSFO (mesma estação já usada), NWS CLI, WFO Monterey (MTR).
- **Padrões críticos**: camada marinha/"Karl the Fog" (maior fator — máxima depende quase inteiramente do horário de dissipação); brisa marítima canalizada pelo "San Bruno Gap"; ventos offshore tipo Diablo (apps de celular subestimam sistematicamente — caso documentado na imprensa de Google/Apple Weather errando enquanto NWS já sinalizava 80°F+); ciclo sazonal invertido (dias mais quentes do ano tipicamente em set/out, não jul/ago).
- **Fontes de edge**: **SFO Marine Stratus Forecast System** (ferramenta operacional dedicada NWS/MIT Lincoln Lab para prever dissipação do estrato — ferramenta de altíssimo valor, específica para esta estação), Jan Null (Golden Gate Weather Services), Daniel Swain (Weather West).

### Washington DC — confiança 0.8 (a mais alta entre as não re-verificadas)
- **Resolução (hipótese)**: KDCA (Reagan National), NWS CLI, WFO LWX (Sterling/Baltimore-Washington). Cidade não configurada hoje.
- **Padrões críticos**: ilha de calor urbana centrada exatamente na estação de liquidação (KDCA é ~10-15°F mais quente à noite que Dulles, a 40km); cold air damming/frentes "backdoor"; heat domes (recorde histórico de 102°F em jul/2026, batendo marca de 1898).
- **Fontes de edge**: **Capital Weather** (ex-Capital Weather Gang do Washington Post, se desligou do jornal em mai/jun-2026 e virou independente — Jason Samenow, Ian Livingston).

### Nova Orleans (New Orleans) — confiança 0.7
- **Resolução (hipótese)**: KMSY, NWS CLI, WFO LIX (New Orleans/Baton Rouge). Cidade não configurada hoje.
- **Padrões críticos**: convergência de dupla brisa (Lago Pontchartrain + Golfo — dispara pop-up storms vespertinas); ilha de calor urbana pronunciada (17ª pior entre 65 cidades dos EUA); baixa amplitude térmica diurna típica da Costa do Golfo (maxima mais sensível a pequenos erros de nebulosidade).
- **Fontes de edge**: Chris Franklin (WWL-TV), Jay Grymes (Climatologista Estadual da Louisiana).

### Austin — confiança 0.5 (ambiguidade genuína de estação)
- **Resolução (hipótese)**: **AMBÍGUO** entre Camp Mabry (KATT, estação climatológica oficial desde 1897, usada pela mídia local para recordes) e Aeroporto Bergstrom (KAUS, estação ASOS) — fontes secundárias divergem, diferença típica de 3-5°F entre as duas (mais pronunciada nas mínimas). Cidade não configurada hoje.
- **Correção importante**: o ticker `KXTEMPAUSH` citado como indício na pesquisa anterior desta sessão **foi refutado** — não aparece em nenhuma URL/documento real da Kalshi. Tickers confirmados ativos: `KXHIGHAUS` / `KXLOWTAUS`.
- **Padrões críticos**: dryline na fronteira Balcones Escarpment; "blue northers"; nebulosidade/neblina matinal por advecção do Golfo.
- **Fontes de edge**: Troy Kimmel (UT Austin, meteorologista de emissoras desde 1984), Bob Rose (LCRA Hydromet, cobre o Hill Country a montante).
- **Ação necessária antes de operar**: abrir a aba "Rules" do mercado `kxhighaus` na Kalshi para resolver a ambiguidade — isso não pôde ser feito neste ambiente (403).

### Minneapolis — confiança 0.6
- **Resolução (hipótese)**: KMSP (Minneapolis-St. Paul Intl), NWS CLI, WFO Twin Cities/Chanhassen (MPX). Cidade não configurada hoje.
- **Padrões críticos**: clima continental extremo sem barreiras a massas árticas (irrupções costumam vir mais severas que os modelos globais sugerem); ilha de calor urbana na própria estação de liquidação; "corn sweat" (evapotranspiração agrícola eleva o ponto de orvalho 5-10°F em jul-ago, fenômeno específico do meio-oeste).
- **Fontes de edge**: Paul Douglas (Star Tribune, 40+ anos de experiência em MN), MPR News Updraft.
- **Nota**: o próprio pesquisador sugeriu, por analogia ao piloto africano deste bot, considerar `resolution_source: metar` como fallback de verdade-terra para esta cidade dado o nível de incerteza.

---

## Recomendações cross-cutting

1. **Antes de configurar QUALQUER cidade para operar na Kalshi**: confirmar a
   estação lendo a aba "Rules" do mercado ativo diretamente no site/app da
   Kalshi (bloqueado neste ambiente de pesquisa, mas acessível normalmente
   pelo operador). Isso é especialmente crítico para Dallas (KDAL→KDFW?),
   Austin (ambíguo) e San Antonio (confiança mais baixa).
2. **Nunca reaproveitar cegamente a config de estação da Polymarket** para a
   Kalshi — já confirmado que diverge em NYC e Chicago, e é candidato a
   divergir em Dallas. O padrão de "estação secundária do aeroporto, não a
   mais famosa/central" já apareceu 3x (Central Park≠LaGuardia,
   Midway≠O'Hare, Hobby≠Bush Intercontinental) — é uma convenção real da
   Kalshi, não coincidência.
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
5. **8 cidades não têm nenhuma config hoje no bot** (Houston, Denver,
   Filadélfia, San Antonio, OKC, Las Vegas, DC, Nova Orleans) + Austin
   (ambíguo) = 9 cidades totalmente novas se a migração para Kalshi avançar.
6. **Completar a verificação adversarial das 14 cidades restantes** quando a
   cota de sessão permitir — o workflow pode ser retomado a partir do
   `runId` salvo (`wf_cef281ed-666`) sem repetir a pesquisa já feita.

## Caveats

- Nenhuma mudança de código/config foi feita a partir desta pesquisa.
- Confiança listada por cidade reflete o julgamento dos próprios agentes de
  pesquisa (0.0-1.0), não uma auditoria humana.
- Fontes de notícia/insider (contas de X, blogs, TV local) podem mudar de
  equipe/handle com o tempo — várias entradas já sinalizam isso
  explicitamente (ex.: WDSU New Orleans trocou de meteorologista-chefe 2x em
  menos de 1 ano).
- Este documento não substitui a checagem em tempo real da API/rules da
  Kalshi antes de qualquer trade real.
