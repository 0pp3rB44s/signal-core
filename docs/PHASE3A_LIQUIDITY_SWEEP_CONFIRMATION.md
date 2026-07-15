# Phase 3A liquidity-sweep confirmation-entry hypothesis

Date: 2026-07-15

## Frozen hypothesis

Control `liquidity_sweep_reversal_v1` is the unchanged Phase 2E strategy.
Experimental `liquidity_sweep_reversal_confirmation_v1` is offline research
tooling only and is absent from production, paper and live registries.

After an already valid frozen candidate and unchanged risk acceptance, the
variant inspects only the next two closed 15-minute candles. LONG confirms on a
close above the signal high; SHORT confirms on a close below the signal low.
It enters at the following candle open through the existing market execution
contract. Otherwise it expires. No other window was inspected.

The original signal-time invalidation and absolute 0.8R/1.5R targets remain
unchanged. They are not re-anchored to the later execution price. Fees, spread,
slippage, partial exit, break-even, sizing and constraints are unchanged.

## Funnel

| Cohort | Detector | Selected | Risk accepted | Confirmed | Expired | Variant filled/closed |
|---|---:|---:|---:|---:|---:|---:|
| Prior | 238 | 54 | 11 | 6 | 5 | 5/5 |
| Independent | 222 | 30 | 7 | 5 | 2 | 5/5 |
| Combined | 460 | 84 | 18 | 11 | 7 | 10/10 |

Counts before the confirmation boundary are identical. One triggered prior
candidate fails the unchanged executable-order contract, as did one control
candidate.

## Performance

| Cohort | Variant trades | Control net / PF / expectancy | Confirmation net / PF / expectancy |
|---|---:|---:|---:|
| Prior | 5 | +0.346101 / 2.065 / +0.034610 | -0.143874 / 0.153 / -0.028775 |
| Independent | 5 | -0.498993 / 0.606 / -0.071285 | -0.843561 / 0.032 / -0.168712 |
| Combined | 10 | -0.152892 / 0.904 / -0.008994 | -0.987435 / 0.052 / -0.098744 |

Combined control gross price PnL is `+1.314301` against costs `1.467193`.
Confirmation gross price PnL becomes `-0.131832` against costs `0.855604`.
The gross-edge/cost ratio falls from `0.8958` to `-0.1541`. Removing the best
confirmation trade leaves `-1.015320`; removing the worst leaves `-0.599162`.

## Entry quality

Combined mean MFE falls from `1.6275R` to `1.1812R`; MAE improves from
`0.9943R` to `0.7456R`, but adverse-first worsens from 70.59% to 80.00% and
favourable-first falls from 29.41% to 20.00%. Mean close displacement at one,
two and four candles changes from `+0.2100R/+0.1130R/+0.1749R` to
`-0.0107R/-0.0449R/-0.0331R`. TP1-capable falls from 70.59% to 60.00%; actual
final-target rate falls from 23.53% to 10.00%. The delayed entry therefore
reduces both risk excursion and available reward, without improving realised
entry quality.

Average adverse entry displacement from signal close rises from `6.44` to
`47.55` bps, while average TP1 reward still available after execution falls
from `60.16` to `25.33` bps.

## Opportunity cost and uncertainty

Seven expired candidates reconstruct: four control losers are avoided, three
control winners are missed. Avoided loss is `0.953737`; missed profit is
`0.214916`; mechanical selection effect is `+0.738821`. This benefit is more
than reversed by worse outcomes among confirmed candidates.

The ten paired confirmed candidates have mean confirmation-minus-control PnL
`-0.157336`; paired bootstrap 95% interval `[-0.281097, -0.064616]`.
The unpaired combined mean-difference interval is `[-0.235524, +0.044805]`.
Confirmation mean PnL bootstrap interval is `[-0.193432, -0.022650]`, PF
interval `[0.0000, 0.2959]`, Wilson win interval `[10.78%, 60.32%]`, and
trade-order drawdown interval `[0.987435, 1.041352]`.

## Verdict

`HYPOTHESIS REJECTED`

The fixed confirmation rule lowers frequency and transaction costs but destroys
gross price edge, worsens independent expectancy, worsens paired outcomes and
does not improve edge-to-cost efficiency. It must not be promoted or tuned in
this phase.

Two complete offline runs are byte-identical with artifact hash
`ddb74fedb1cdae6b1a0b4f603434aa714b886f60b1634bc7a3d162bd59244336`.
