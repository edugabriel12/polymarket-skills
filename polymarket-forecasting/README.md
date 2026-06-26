# polymarket-forecasting

Shared, sport-agnostic forecasting cores reused by the prediction models (soccer, tennis) and
the dashboard. **Pure stdlib, no skill imports** — the soccer/tennis skills put this `scripts/`
dir on `sys.path` via their `_bootstrap.py` and import these modules directly.

| Module | Purpose |
|--------|---------|
| `run_distribution.py` | Negative-Binomial / totals distribution, P(Over), market-implied μ, odds filter, market anchor |
| `forecast.py` | Distributional forecast helpers (pmf → cdf / quantile / interval / entropy) + per-prediction confidence |
| `scoring.py` | Proper scoring rules — CRPS, Brier, log-loss |
| `calibration_core.py` | ECE / reliability / Brier decomposition primitives |
| `calibration.py` | Calibration report over the `model_log` shadow log (`--sport soccer\|tennis`) |
| `congruence.py` | Cross-source agreement scoring |
| `audit_log.py` | Dump the full math audit (`stats_log`) of every prediction (`--sport soccer\|tennis`) |

## Tests

```bash
cd polymarket-forecasting/scripts
for t in test_*.py; do python "$t"; done
```

All offline, no network. Covers the distribution math, scoring rules, calibration report
(self-contained sqlite seeding), forecast helpers, and congruence.
