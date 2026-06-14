# Edge classification, odds filter, sizing & validation

## Edge classification → risk cap

Rule #1 of the constitution requires every trade to be one of {arbitrage, momentum,
mean-reversion, news-driven}. This is a **statistical fair-value / model edge**: we estimate
`P(Over)` from a run model and trade the gap to the market price.

**Classified as `news-driven` → 2% per-trade cap** (CLAUDE.md §2), and **1% on the first trades**
of this new strategy. Rationale: an unproven, input-dependent forecast edge has the same epistemic
risk profile as a news edge — model-dependent and easily wrong — and MLB totals are near-efficient
at close (research §3/§5), so the conservative cap is the honest choice. Framing it as
"mean-reversion → 10%" would over-size an edge that is not yet proven to exist.

```
cap = 0.02                       # model / news-driven
if first_trade_new_strategy: cap = min(cap, 0.01)
if confidence < 0.7:          cap = min(cap, 0.05)   # non-binding vs 0.02/0.01
size_pct = min(half_kelly_fraction, cap)
size_usd = portfolio_value * size_pct
if size_usd < 10: skip          # CLAUDE.md minimum trade size
```

`first_trade_new_strategy` is detected from the paper DB (`--portfolio-db`); with no DB it defaults
to **True** (most conservative). Confidence is kept **below 0.7** (≈0.5 under the zero-edge
fallback) so sizing stays small until the strategy is paper-validated.

## The 1.60×–3.0× payout filter

Polymarket price = implied probability, so **decimal odds = 1 / price**. The operator's
1.60×–3.0× payout band maps to an **entry-side price in `[1/3.0, 1/1.6] = [0.3333, 0.625]`**.
A side is only eligible if its own price is inside this band (and its edge is positive). This
deliberately excludes heavy favorites (price > 0.625, payout < 1.60×) and longshots (price < 0.333,
payout > 3.0×).

## Side selection

For each game: compute `edge_side = P_model_eff(side) − price(side) − fee(price)`
(`fee = fee_rate · min(p, 1−p)`, default 0 for sports). A side is a **candidate** iff `edge > 0`
**and** its price is in the odds band. One candidate → take it; both → larger post-fee edge;
neither → no suggestion (logged reason). Betting Over/Under = **BUY YES on that side's own token**,
so the chosen side is always a YES bet and `kelly_half(P_model, price, "YES")` applies directly.

## Entry decision tree (all must pass; every skip logged)

1. totals-market 24h volume > `--min-volume` ($10K)
2. spread < 10% (true book spread if fetched, else `|1 − (p_over + p_under)|` proxy)
3. end date > 24h away
4. accepting orders
5. edge classifiable → yes (news/model)
6. edge > `--min-edge` (5%) after fees
7. half-Kelly > 0
8. risk caps pass (size ≥ $10, per-trade cap)

## Validation before any real capital

Per the research, judge the model by **calibration and CLV, not raw ROI**:
- **Brier score**, **log-loss**, and a **reliability diagram** on the Over/Under probabilities.
- **Closing Line Value** vs the closing Polymarket price (the fastest skill signal).
- A meaningful sample: **~1,000+ entries** before distinguishing skill from variance.
- Realistic ceiling: **ROI ~2–5%, win rate ~53–55%** (break-even ~52.4% on −110-equivalent pricing).

CLAUDE.md §4 live-readiness gates (20+ closed paper trades, win > 55%, Sharpe > 0.5, max drawdown
< 15%) still apply **on top** of the above before live is even considered. Live remains opt-in
(rule #2); this skill never escalates on its own.
