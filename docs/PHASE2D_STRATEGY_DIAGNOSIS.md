# Phase 2D strategy diagnosis

Date: 2026-07-15

Frozen inputs: execution `2009a4a5`, Phase 2A `2352749`, Phase 2B `523fc77`,
Phase 2C `2ad6f73`, dataset `9053781e`, proxy `722bb696`, bootstrap seed
`20260715` with 5,000 resamples.

## Findings

Trend continuation is negative before and after costs. In 102 trades, 81.4% is
adverse-first; mean closes are already negative after 1, 2 and 4 candles and become
more negative through candle 16. Mean MFE is 1.35R, but mean MAE is 1.46R and every
permitted exit counterfactual remains materially negative. The primary diagnosis is
entry failure, not a single exit defect. Decision: reject the strategy as currently
defined.

Liquidity sweep has ten official trades, mean MFE 1.60R and mean MAE 0.72R. Nine
reach TP1 and seven reach the final-target distance over the diagnostic horizon.
Both directions are positive, but the bootstrap mean interval crosses zero widely.
The primary classification is insufficient sample.

Sweep scarcity is not detector silence or candidate competition: 238 raw hits are
single-detector snapshots. Selector intrinsic alignment rejects 184 (103 LONG, 81
SHORT); their independently reconstructed execution outcomes total -12.34 over 151
closed counterfactuals. Of 54 selected sweeps, 28 pass structural risk, 11 pass the
proxy, one order is invalid/rejected and ten close. The frozen entry-position gate
and TP1-cost floor each remove negative counterfactual groups.

Momentum breakdown confirms the adverse-first entry pattern: nine of ten trades
are adverse-first, mean MFE 0.52R versus MAE 1.37R, with negative gross PnL.

## Uncertainty

Trend continuation mean net trade PnL 95% bootstrap interval is approximately
[-0.1575, -0.0647]. Sweep is [-0.0397, 0.1107], PF interval approximately
[0.32, 21.63] and Wilson win-rate interval [31.3%, 83.2%]. Sweep is not proven.

## Single next experiment

Acquire one additional non-overlapping year of the same Bitget USDT-futures 15m
history and rerun only the frozen liquidity-sweep diagnostic contract unchanged.
