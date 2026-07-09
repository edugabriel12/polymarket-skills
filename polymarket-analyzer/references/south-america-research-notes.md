# Notas de pesquisa — América do Sul (2026-07)

> Status: **PARCIALMENTE IMPLEMENTADO.** Duas rodadas de deep-research
> (Sonnet 5) investigaram se o bot pode operar cidades sul-americanas com o
> mesmo rigor que a Europa/África receberam nos PRs #165-172. Veredito
> original: base de evidência **bem mais fraca** que Europa/África — não
> curar sem confirmar mercado + estação real primeiro.
>
> **Atualização 2026-07-09**: o operador rodou `verify_eu_stations.py
> --cities "Sao Paulo" "Rio de Janeiro"` e confirmou um mercado REAL ativo
> para São Paulo, com a Rules citando a estação explicitamente ("recorded at
> the Sao Paulo-Guarulhos International Airport Station" → SBGR) — cruzando
> a barreira que travava a implementação. **São Paulo foi curado** em
> `weather-cities.json` (ver `_south_america_doc`): `stations["São Paulo"]`
> = SBGR/Guarulhos, sem `om_models` (nenhum modelo regional Open-Meteo existe
> para a região, cai no trio global — mesmo padrão do piloto África) e sem
> `resolution_source`/`pilot` (São Paulo fica na zona sudeste do Brasil que a
> pesquisa NÃO flagrou como degradada para ERA5 — ver seção "Qualidade da
> verdade-terra" abaixo — diferente da África, onde o arquivo era
> confiavelmente ruim). `station_names` ganhou `guarulhos`/
> `sao paulo-guarulhos`/variantes → SBGR, para o auto-extractor resolver
> automaticamente se o mercado reaparecer. **Rio de Janeiro** e os outros 10
> candidatos não tinham mercado ativo no momento da verificação — ficam
> pendentes, deliberadamente sem coords pré-preenchidas (diferente do padrão
> EU/África de pré-preencher com "aeroporto primário padrão", já que aqui não
> temos uma base de precedentes confirmados para arriscar um palpite) —
> re-rodar `verify_eu_stations.py` (lista default já inclui os 12) quando um
> mercado aparecer.

## Rodada 1 — visão geral (Open-Meteo, APIs nacionais)

**Confirmado [3-0, 2x independente]:** o Open-Meteo **não integra nenhum
modelo NWP regional sul-americano**. A lista oficial de 15+ serviços
meteorológicos nacionais cobre Europa/América do Norte/Ásia/Oceania — zero da
América do Sul. Qualquer cidade da região cai só no trio global
(`icon_seamless`/`gfs_seamless`/`ecmwf_ifs025`) — sem o refinamento regional
que a Europa ganhou (`icon_d2`/`arpege`/`arome`/`harmonie`).

**Confirmado [3-0, 2x independente]:** a Argentina (SMN — Servicio
Meteorológico Nacional) publica um modelo **WRF 4.0 real, a 4km de
resolução**, cobrindo Argentina + Chile + Uruguai + Paraguai + partes da
Bolívia/Brasil. Hospedado grátis na AWS Open Data
(`s3://smn-ar-wrf`, região `us-west-2`), 2 rodadas/dia (00/12 UTC),
inicializado com condições de contorno do GFS, lead time máximo 72h,
temperatura horária a 2m, licença CC BY 2.5 Argentina, contato
`odp-aws@smn.gov.ar`. **Mas é GRIB/NetCDF cru num bucket S3** — não um JSON
simples como Open-Meteo/IPMA/met.no — integrá-lo exigiria uma dependência
nova (`cfgrib`/`xarray` ou similar) e um parser dedicado, esforço de
engenharia bem maior que qualquer fonte já integrada nesta sessão (que são
todas fetch HTTP + JSON).

**Refutado [0-3]:** a "API REST não-oficial do SMN"
(`ws.smn.gob.ar/map_items/weather`, documentada só num gist comunitário no
GitHub) — quase todas as alegações sobre ela (sem-auth, formato JSON/CSV,
múltiplas estações) foram refutadas na verificação adversarial. Só
sobreviveu a alegação cética da própria fonte ("não é bem documentada, pode
ser difícil de usar"). **Não confiável sem teste direto.**

## Rodada 2 — aprofundamento (qualidade da verdade-terra + clima regional)

A primeira rodada deixou 3 de 5 ângulos vazios (estação/ICAO por cidade,
qualidade METAR/ERA5, padrões climáticos). Uma segunda rodada, com termos de
busca mais específicos e mirando fontes acadêmicas primárias em vez de
agregadores genéricos, confirmou achados sólidos em 2 dos 3 (qualidade
ERA5/reanálise e calibração climática regional); o terceiro (estação/ICAO)
permanece **estruturalmente impossível de resolver via pesquisa web** — ver
seção dedicada abaixo.

### Qualidade da verdade-terra (ERA5/ERA5-Land)

- **[alta confiança]** ERA5/ERA5-Land tem correlação geral forte com
  estações reais no Brasil (r=0.89 nacional vs. ISD; r>0.91 vs. INMET numa
  região costeira montanhosa do NE após correção de altitude) — mas **não é
  espacialmente uniforme**: degrada mensuravelmente no norte/nordeste
  (adjacente à Amazônia), e o ERA5 pode superestimar a consistência de
  tendências de calor extremo em comparação com o registro real de estações.
  Fontes: `doi.org/10.3390/cli14050098`, `link.springer.com/article/10.1007/s00704-025-05777-5`.
- **[alta confiança]** Entre ERA5 e ERA5-Land, **nenhum é uniformemente
  "melhor"** — ERA5 tem vantagem para temperatura MÁXIMA no semiárido
  nordestino brasileiro (RMSE<1.6°C, r>0.8-0.9); ERA5-Land às vezes é mais
  preciso para temperatura MÍNIMA em estações específicas, mas subestima
  extremos de calor na máxima. **A escolha ideal depende de qual extremo o
  mercado resolve** (mapeia direto no campo `temp_kind` já existente em
  `weather_edge_helpers.py` — "high" vs "low"). Fonte (2026, muito recente):
  `link.springer.com/article/10.1007/s00704-026-06057-6`.
- **[alta confiança]** A rede de estações hidrometeorológicas andina é
  genuinamente esparsa — só **451 estações registradas na OMM/WMO-OSCAR em 7
  países**, distribuição desigual mesmo dentro de um único país (ex.: Chile
  tem densidade bem menor no norte/sul que no centro; Bolívia é a mais
  esparsa, 39 estações). Isso cria risco de não-homogeneidade em qualquer
  produto gridded/reanálise usado como verdade-terra ali — o mesmo tipo de
  risco que motivou o piloto africano a usar METAR real em vez do arquivo
  ERA5. Fonte: `frontiersin.org/articles/10.3389/feart.2020.00092/full`.
- **[alta confiança]** A documentação técnica do próprio **ECMWF** nomeia o
  descasamento entre a elevação do grid do modelo e a elevação real da
  estação (corrigido via uma taxa de lapso fixa de 5.5°C/km, reafirmada até o
  upgrade IFS Cycle 49r1 de 2024) e inversões de temperatura vale/pico não
  resolvidas pela orografia suavizada do modelo como causas **ativas e
  atuais** de erro de previsão de temperatura a 2m em terreno montanhoso —
  implicando diretamente **Bogotá (~2600m), Quito (~2850m) e La Paz
  (~3600m)**, onde a grade global de ~9km não resolve a topografia real.
  Fonte: `confluence.ecmwf.int/.../Section+9.2.1...`.
- **[alta confiança, mas com ressalva de escopo]** O produto SAMeT (South
  American Mapping of Temperature — Rozante et al. 2022, Int. J. Climatology
  42(4):2135-2152) é um ERA5+estação+correção-de-lapso dedicado a 5km, e as
  taxas de lapso empíricas da região são consistentemente mais fracas que o
  padrão -6.5°C/km — uso ingênuo do ERA5 em terreno elevado carrega viés
  sistemático esperado. **Ressalva**: não confirmamos se as "4 regiões" do
  paper cobrem especificamente os Andes altos da Colômbia/Equador/Bolívia
  (pode ser Brasil/Cone-Sul-cêntrico). Fonte: `rmets.onlinelibrary.wiley.com/doi/10.1002/joc.7356`.

**Implicação prática**: se/quando a América do Sul for implementada, as 3
cidades andinas de altitude extrema (Bogotá/Quito/La Paz) deveriam receber o
mesmo tratamento de alto-risco do piloto africano — resolução via METAR real
(reusar `fetch_metar_daily_extremes` de `weather_edge_helpers.py`, já
construído para o piloto África) em vez de confiar cegamente no arquivo
Open-Meteo/ERA5, dado o viés de orografia documentado pelo próprio ECMWF.

### Padrões climáticos regionais

- **[alta confiança]** O skill sazonal do ECMWF SEAS5/ENSO sobre a América
  do Sul é **muito desigual regionalmente** — melhor nos trópicos no verão
  austral (70% de probabilidade de discriminação correta), ainda
  "considerável" a leste dos Andes, mas baixo no sul do Chile/Argentina — e
  esse skill do modelo dinâmico supera um preditor empírico ingênuo baseado
  só em Niño-3.4 nas mesmas regiões de alto skill (ENSO explica muito, mas
  não tudo, da previsibilidade sazonal da região). **Importante**: isso é
  skill SAZONAL (meses à frente), não skill diário de PNT — relevância
  direta para o bot (que opera em horizonte de dias) é limitada, mas dá
  contexto direcional. Fonte: `journals.ametsoc.org/view/journals/wefo/35/2/waf-d-19-0106.1.xml`.
- **[confiança média]** Na região do Chocó (costa Pacífica da
  Colômbia/Equador, uma das mais chuvosas do mundo), a literatura encontrada
  aborda **estrutura de sistemas convectivos de mesoescala e precipitação**,
  não temperatura diretamente — mesmo radar aerotransportado co-localizado
  com dropsondes in-situ (setup quase ideal, indisponível para previsão
  operacional de rotina) não conseguiu discriminar confiavelmente entre
  subtipos de MCS com intensidades de chuva muito diferentes. **Isso suporta
  a moldura geral de "baixa previsibilidade em trópicos dominados por
  convecção" só por analogia/proxy — não é um achado direto de mercado de
  temperatura.** Fontes: `journals.ametsoc.org/mwr/article/146/6/1763/...`,
  `agupubs.onlinelibrary.wiley.com/doi/10.1029/2024GL114186`.
- **Sem achado**: nenhuma literatura sobre previsibilidade de eventos de
  "friagem" (frente fria polar na Amazônia/sul do Brasil/Bolívia) sobreviveu
  à verificação em nenhuma das duas rodadas, apesar de termos de busca
  direcionados. Pergunta em aberto.

**Implicação prática**: ao contrário da África (onde a zona de
monção/ITCZ teve exclusão justificada por evidência direta e forte), **não
há evidência direta e forte o suficiente para excluir Amazônia/Chocó de um
piloto de temperatura** — a literatura encontrada é sobre precipitação, não
temperatura. Se implementado, tratar como incerteza a resolver com dados
próprios (backtesting), não como exclusão categórica pré-julgada.

### Estação/aeroporto ICAO por cidade — permanece NÃO respondível via pesquisa web

Confirmado **nas duas rodadas**: pesquisa web pode no máximo sugerir qual
aeroporto é o mais PROVÁVEL por cidade (ex. Guarulhos SBGR vs. Congonhas SBSP
para São Paulo; Ezeiza SAEZ vs. Aeroparque SABE para Buenos Aires), mas a
estação EXATA que um mercado real da Polymarket usa só está na seção "Rules"
do próprio mercado (Gamma API) — exatamente o método que `verify_eu_stations.py`
já usa para a Europa/África. **Não existe atalho de pesquisa para isso.**
Além disso, permanece em aberto se sequer existe um mercado ativo de
temperatura da Polymarket para qualquer cidade sul-americana hoje.

**Ação recomendada** (mecânica, já feita neste PR): estender a lista default
de `verify_eu_stations.py` com os 12 candidatos sul-americanos, para o
operador rodar no host dele (Gamma acessível) e descobrir (a) se existe
mercado ativo para essas cidades e (b) qual estação a Rules realmente cita —
resolvendo de vez o único ângulo que pesquisa alguma não resolve.

## Avaliação geral

Mais forte que a rodada 1 sozinha, mas ainda **bem mais fraca que a base de
evidência que sustentou os PRs #165-172 da Europa/África** — lá tínhamos 19
alegações 3-0 cobrindo 5 regiões inteiras E confirmação de estação via Gamma.
Aqui: nenhuma cidade tem estação confirmada, nenhum mercado ativo confirmado,
o único modelo regional real (SMN-WRF) exige engenharia GRIB nova, e a
exclusão de zonas de baixa previsibilidade (Amazônia/Chocó) não tem a mesma
força de evidência direta que a África teve. **Recomendação: não curar
`weather-cities.json` ainda.** Próximo passo de baixo risco: rodar
`verify_eu_stations.py --cities "Sao Paulo" "Rio de Janeiro" ...` (lista já
estendida) para descobrir se há mercado real — só então vale decidir sobre
o esforço de GRIB/METAR-Andes descrito acima.

## Fontes primárias citadas

- Open-Meteo: `github.com/open-meteo/open-meteo`, `open-meteo.com/en/features`
- SMN Argentina WRF: `registry.opendata.aws/smn-ar-wrf-dataset/`
- ERA5/ERA5-Land no Brasil: `doi.org/10.3390/cli14050098`,
  `link.springer.com/article/10.1007/s00704-025-05777-5`,
  `link.springer.com/article/10.1007/s00704-026-06057-6`
- Rede de estações andina: `frontiersin.org/articles/10.3389/feart.2020.00092/full`
- Viés de orografia ECMWF: `confluence.ecmwf.int/display/FUG/Section+9.2.1...`
- SAMeT (lapso térmico regional): `rmets.onlinelibrary.wiley.com/doi/10.1002/joc.7356`
- ENSO/SEAS5: `journals.ametsoc.org/view/journals/wefo/35/2/waf-d-19-0106.1.xml`
- Chocó/MCS: `journals.ametsoc.org/mwr/article/146/6/1763/...`,
  `agupubs.onlinelibrary.wiley.com/doi/10.1029/2024GL114186`
