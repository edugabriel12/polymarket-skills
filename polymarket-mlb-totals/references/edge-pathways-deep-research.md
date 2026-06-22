# Deep Research: Where is the edge in MLB betting?

5-angle synthesis (market-bias, props/derivatives, situational, CLV/execution, ML models).
Motivates the model's re-anchoring to the sharp price (`sharp_odds.py`) and the CLV metric
(`clv_vs_sharp.py`).

> Method caveat: `WebFetch` was 403-blocked on academic/practitioner hosts; figures come from
> corroborated search extractions. Directions are high-confidence; exact numbers should be
> confirmed against the primary PDFs.

## Central verdict
A predictive run-total model is **not a viable way to beat MLB**. The durable edges are
**execution**-based, not prediction-based.

| Path | Viable? |
|---|---|
| Build a better predictive model | ❌ The market already ingests all public projections (THE BAT X/ZiPS/Statcast); models *match* the close, don't beat it. Confirmed by our 10-season backtest (Brier ≈ coin-flip, ROI ~0). |
| "Just the edge" (Over/UNDER bias) | ⚠️ Real at the OPEN, **dies at the CLOSE** (Harvard 2023; Woodland 1994 / Gandar 2002 — MLB close is efficient after vig). Our +3% UNDER finding is an open-line artifact unless it survives CLV. |
| Execution: **CLV + line-shopping + speed-to-info + cross-venue** | ✅ The only durable, validated edge. |
| Model as a **divergence detector** vs the sharp price | ✅ How a model adds value — flag when Polymarket ≠ sharp consensus, don't out-predict. |

## Key findings by angle
1. **Market bias.** Over-bias and favorite-longshot exist at the open and are >55% corrected by
   close ("disappeared by closing time"). Fade-the-public only profits in narrow, fragile slices.
2. **Props/derivatives** (NRFI, F5, K-props) are structurally softer (opener-pegged, less sharp
   money) but **6–10% hold** eats the edge; CLV is a weaker benchmark in thin prop markets.
3. **Situational** (weather/umpire/park/lineups): real effects but **priced by close**; park factors
   fully priced; the residual is a timing/speed play, and lineups leak to partner books first.
4. **CLV / execution.** Beating the sharp (Pinnacle, devigged) close validates edge in ~50 bets
   (variance ≪ P&L). Line-shopping is the cleanest retail edge. The open is inefficient; the close
   is efficient. Arb/middle margins are 1–2%, decay in seconds, and self-destruct via account limits.
5. **ML models.** No public, replicated evidence of a projection model beating the MLB *closing*
   total after vig; reported "profits" are in-sample, vig-free, or bet stale opening lines. Realistic
   sharp ceiling: **~1–3% ROI / +1–3% CLV**.

## Implication for this Polymarket skill
- **Polymarket is the right venue**: ~0% overround (vs ~4.76% vig) and it **does not limit winners**
  (sportsbooks limit/ban exactly the CLV-positive accounts). But its microstructure is efficient
  (arXiv NBA: 7 arbs / 173 games), so the edge is *cross-venue divergence*, not out-prediction.
- **Re-anchor the model to the sharp price** (`sharp_over_price`): the edge becomes Polymarket-price
  vs sharp-fair (a mispricing detector), not model-vs-Polymarket. → `sharp_odds.py`.
- **Validate with CLV vs the sharp close**, the only metric that confirms edge. → `clv_vs_sharp.py`.

## Key sources
Woodland & Woodland 1994 (J. Finance); Gandar et al. 2002 (Applied Economics); Harvard "Swing and a
Miss" 2023 (DASH); Simon et al. *Management Science* 2024 (line overreaction, 3,681 games); Buchdahl
via Pinnacle Odds Dropper (CLV); arXiv:2605.00864 (Polymarket NBA microstructure); arXiv:2410.21484
(ML-betting review); FanGraphs projection showdowns; ESPN/WaPo (sportsbook limiting).

*Research synthesis — not financial advice. Real trading involves risk of loss.*
</content>
