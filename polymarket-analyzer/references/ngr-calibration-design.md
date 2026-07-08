# Design — NGR por cidade / estação-do-ano / lead-time (`ngr_calibration.py`)

> Status: **DESIGN (não implementado).** Especificação aprovada como Fase 4 do
> plano "Europa" (2026-07). Este documento define o projeto; a implementação e a
> ativação por-cidade ficam gated em backtest holdout (não ligar sem provar
> melhora de CRPS vs o NGR global).

## Problema

Hoje a calibração do ensemble usa **coeficientes NGR globais fixos** — `NGR_ALPHA=1.5`,
`NGR_BETA_C=0.5`, `SIGMA_FLOOR_C=1.0` (`weather_edge_helpers.py:58-65`), aplicados
igualmente a todas as cidades em `compute_ensemble_calibration`
(`helpers.py:284+`): `mu = média dos membros`, `sigma = max(α·σ_raw + β, floor)`.
O único ajuste por-cidade é o `temp_bias_f` manual do `weather-cities.json`
(aditivo em `_forecast_probability_raw`, `helpers.py:1085-1086`).

A pesquisa (ECMWF Newsletter 157) comprovou que os vieses de 2m do IFS na Europa
são **regionais, sazonais e diurnos**: no verão europeu o IFS subestima a
amplitude do ciclo diurno em 1-2K → **máximas previstas frias demais, mínimas
quentes demais** — exatamente as grandezas que os mercados liquidam. Coeficientes
globais fixos são portanto mal-especificados. O comentário em `helpers.py:56-57`
já antecipa o fit por-estação ("Once we have ≥200 paired (forecast, observed) log
entries per station we can fit a, b properly per spec.city").

Com a Fase 3 (D) adicionando modelos regionais na Europa, o número de membros do
ensemble mudou (3 → 4-5), o que **também** desloca o σ_raw — reforçando a
necessidade de recalibrar α/β por-cidade em vez de herdar o valor pensado para 3
modelos globais.

## Objetivo

`ngr_calibration.py`: fitar coeficientes NGR por **(cidade × estação-do-ano ×
lead-time × {max,min})**, emitir `references/ngr-coefficients.json`, e fazer
`compute_ensemble_calibration` lê-los com **fallback ao global** (retrocompatível).

## 1. Dados (pares forecast↔observado)

Cada amostra = (μ_ensemble e σ_raw previstos para um dia-alvo com um dado
lead-time, temperatura observada). O `forecast_history` local (`db.py:220`) é
esparso (só dias de operação) → o fit precisa de histórico amplo:

- **Previsão histórica**: **Open-Meteo Historical Forecast API**
  (`/v1/forecast` archive, modelos selecionáveis, ~2021+) e **Previous Runs API**
  (lead-times fixos 1-7d) — reconstroem, por cidade (lat/lon da station) e
  lead-time, o que cada modelo do `om_models` teria previsto. Reusar/estender
  `fetch_open_meteo_archive` (`helpers.py:331`).
- **Observado (verdade)**: **Open-Meteo archive** (`fetch_open_meteo_archive`)
  por lat/lon da station — a mesma fonte que a resolução usa
  (`force_resolution_sweep.py:53-78`), garantindo consistência forecast↔truth.
- Janela recomendada: **2-4 anos** por cidade (cobre as 4 estações-do-ano ×
  lead-times com n suficiente).

## 2. Fatiamento e fit

- Slices: `city × season(DJF/MAM/JJA/SON) × lead∈{1..5} × kind∈{max,min}`.
- Para cada slice, com os pares {(μ_i, σraw_i, obs_i)}:
  - **β (viés aditivo)**: mediana de `(obs - μ)` — corrige o viés de máxima/mínima.
  - **α (inflação do spread)** e **β_σ (floor aditivo)**: ajustar
    `σ = α·σ_raw + β_σ` minimizando o **CRPS** do gaussiano
    `N(μ+β, σ)` sobre o slice (CRPS fechado para gaussiana; reusar
    `polymarket-forecasting/scripts/calibration_core.py` se aplicável, ou o
    `crps_pmf` já citado no repo).
  - **Gate de amostra**: `n ≥ 150` no slice, senão o slice **não** é emitido
    (cai no global). Evita overfit em cidades/estações raras.
- Saída `references/ngr-coefficients.json`:
  ```json
  {"Paris": {"JJA": {"1": {"kind_max": {"alpha":1.2,"beta":-0.8,"beta_sigma":0.4},
                           "kind_min": {"alpha":1.4,"beta":0.6,"beta_sigma":0.5}},
                     "2": {...}}, "DJF": {...}}, ...}
  ```

## 3. Integração (retrocompatível)

- `compute_ensemble_calibration(om_data, threshold_unit, temp_kind, *, city=None,
  season=None, lead=None)`: se houver coef para (city, season, lead, kind) no
  JSON → usa α/β/β_σ do slice; **senão usa os globais atuais** (comportamento de
  hoje, byte-idêntico onde não há coef). O `temp_bias_f` manual vira o β derivado
  (o JSON tem precedência quando presente; `temp_bias_f` permanece como fallback).
- Chamador (`_compute_mae_for_market`, `bot.py:617`) passa `spec.city`, a
  estação-do-ano derivada de `target_date`, e o lead-time (`target_date - hoje`).
- Carregar o JSON uma vez (cache de módulo, como `load_cities`).

## 4. Validação (obrigatória antes de ativar)

- **Holdout temporal**: treinar em N-1 anos, testar no ano retido.
- Ativar o coef por-cidade **só se o CRPS holdout melhorar** vs o NGR global no
  mesmo slice. Um flag por-cidade (ou simplesmente a presença/ausência do slice
  no JSON) controla a ativação — nunca ligar cego.
- Reusar o padrão de reliability_diagram/ece/brier do repo para reportar a
  qualidade por slice.

## 5. Operação

- Idealmente o **advisor semanal** (Opus, read-only, `weather_strategy_advisor`)
  roda o fit com os dados acumulados na VPS e propõe o JSON atualizado; o operador
  revisa e aplica (a constituição §5 mantém o advisor como suggestion-only).
- CLI: `python ngr_calibration.py --fit --cities Paris London ... --years 3
  --out references/ngr-coefficients.json` (dry-run/report por default; `--write`
  para gravar). Teste `--test` hermético com pares sintéticos.

## 6. Riscos / notas

- **Dependência de dados históricos**: sem 2-4 anos de forecasts arquivados o fit
  não tem n suficiente → mantém global. É o motivo de E ser projeto separado.
- **Custo de API**: a Historical Forecast API é free-tier mas volumosa; fazer o
  fit offline/batch, não no loop de discovery.
- **Não acoplar ao discovery**: o JSON é lido (barato); o fit roda fora de banda.
- **Precedência**: JSON por-slice > `temp_bias_f` manual > NGR global. Documentar
  para o operador não configurar os dois em conflito.

## Sequência sugerida
1. `fetch_open_meteo_archive` estendido p/ Historical Forecast + Previous Runs.
2. Coletor de pares (city×season×lead×kind) → dataset local.
3. Fit CRPS por slice + gate n≥150 → `ngr-coefficients.json`.
4. Leitura no `compute_ensemble_calibration` com fallback global.
5. Holdout + relatório; ativação por-cidade só com melhora comprovada.
