# Phase 2E independent liquidity-sweep validation

Date: 2026-07-15

## Frozen boundary

Phase 2D is frozen in commit `2405e8244c9eced57864561ed80b1cdbd7b6d52e`.
Its diagnosis hash reproduced as
`c42283851c0a0c04a57a75e3a385a12297475274dbac3863c80b4449d2936382`.
This experiment retains execution commit `2009a4a5`, proxy configuration hash
`722bb6962e575931e5d4b2ee58ce175413729c587f9eed5a796b69930a349cbc`,
seed `20260715`, 5,000 resamples and every frozen detector, selector, gate,
entry, stop, target, sizing, cost and execution setting. Only
`liquidity_sweep_reversal` is interpreted as the performance experiment.
`trend_continuation` remains rejected and is excluded from every decision.
The opt-in research execution filter was applied after the unchanged selector;
its seven sweep records and PnL are byte-identical to the unfiltered diagnostic
run. Two filtered replays have result hash `8a6f13a1199dcf0a54a4a2755b46762265a833985cd8aabafea88e8f4a2ea53a`
and core-artifact hash `0175c9821237c544bc07bd0ba09a157d23f87ee8f1cd6e1efcd129a5cc61f222`.

## Independent data

The public Bitget `USDT-FUTURES` history endpoint supplied 15-minute market
candles for the inclusive/exclusive interval 2024-07-15 00:00Z through
2025-07-15 00:00Z. ADA, AVAX, BTC, ETH, LINK, SOL, SUI and WIF each contain
35,040 candles from 2024-07-15 00:00 through 2025-07-14 23:45 UTC. Every
canonical series has zero gaps, duplicates, invalid OHLC rows, zero or negative
volume rows and alignment errors. Raw backward-page ordering is expected and is
canonicalised deterministically. Dataset hash:
`d7d7a7670b6bd5723cc5f0b7b279b099c3b0258659f2cfd384c9b9179b0953fb`.

All eight contracts have complete history for the requested interval. Universe
A (all-symbol common window), Universe B (full-year core) and every Universe C
per-symbol maximum window therefore share the exact requested year. There are
no exclusions. The current contract endpoint reports WIF open time as
2024-03-09 07:00Z; older contracts return no listing timestamp, recorded as
unknown rather than inferred.

Reproduction commands:

```text
PYTHONPATH=. python3 scripts/acquire_bitget_history.py --start 2024-07-15T00:00:00Z --end 2025-07-15T00:00:00Z --output data/historical/bitget/usdt-futures/15m/20240715_20250715_acq20260715
PYTHONPATH=. python3 scripts/acquire_bitget_contract_availability.py --output data/historical/bitget/usdt-futures/15m/20240715_20250715_acq20260715/contract_availability.json
```

## Funnel stability

| Stage | Prior year | Independent year |
|---|---:|---:|
| Snapshots | 279,680 | 279,680 |
| Detector hits | 238 | 222 |
| Selected | 54 | 30 |
| Selector alignment rejects | 184 (77.3%) | 192 (86.5%) |
| Structural accepted | 28 | 17 |
| Entry-position rejects | 26 (48.1% of selected) | 13 (43.3%) |
| Proxy accepted | 11 | 7 |
| TP1/cost-floor rejects | 16 (57.1% of structural) | 5 (29.4%) |
| Execution attempted | 11 | 7 |
| Rejected orders | 1 | 0 |
| Fills / closed | 10 / 10 | 7 / 7 |
| Unresolved | 0 | 0 |

Scarcity repeats and worsens: the second full year produces only seven frozen
trades. Detector frequency is similar, while alignment acceptance is lower.

## Independent performance

The seven closed trades lose `-0.498993` net (`-0.049899%`, ending equity
`999.501007`). Gross price movement before modeled execution costs is positive
`+0.111832`, but execution-adjusted gross PnL is `-0.205299`. Entry fees are
`0.146827`, exit fees `0.146867`, spread impact `0.210042`, slippage impact
`0.107089`, and total transaction cost `0.610825`.

Profit Factor is `0.605700`; expectancy is `-0.071285` or `-0.009530R`; win
rate is 42.86%; payoff ratio is 0.8076. Average win/loss are `+0.255508` and
`-0.316379`; median `-0.293440`; best `+0.535491`; worst `-0.343297`; maximum
drawdown `0.636738`. TP1, break-even, final-target and full-stop rates are
42.86%, 14.29%, 14.29% and 42.86%. Mean MFE is `1.6627R`, mean MAE `1.3873R`,
71.43% is adverse-first and mean holding time is 3.43 candles.

Both directions are negative: LONG `-0.153357` (2), SHORT `-0.345636` (5).
Only ADA, LINK and SOL trade; their net results are `-0.636738`, `+0.725432`
and `-0.587688`. Asia loses `-0.638565`; Europe gains `+0.139572`; no US
trade occurs. Every symbol, month, hour, session and regime cell is a tiny
sample and is not interpreted as segment edge.

## Uncertainty and combined evidence

Independent 95% intervals: bootstrap mean `[-0.273009, +0.171538]`, bootstrap
PF `[0.0210, 3.1116]`, Wilson win rate `[15.82%, 74.95%]`, and resampled maximum
drawdown `[0.626952, 1.265517]`. PnL without the best trade is `-1.034485`,
without the best three `-1.265517`, and without the worst `-0.155696`.
Trade-order ending equity is the degenerate additive interval
`[999.501007, 999.501007]`; ordering changes drawdown, not the sum of frozen
realised trade PnLs.

Combined, the two frozen years have only 17 trades. Gross price PnL is
`+1.314301`, execution-adjusted gross PnL `+0.556446`, transaction costs
`1.467193`, and net PnL `-0.152892`. PF is `0.903867`, expectancy
`-0.008994`, win rate 52.94%, mean MFE `1.6275R`, mean MAE `0.9943R`, and
maximum drawdown `0.636738`. The combined bootstrap mean interval is
`[-0.114512, +0.102528]`, PF interval `[0.2126, 3.5647]`, Wilson interval
`[30.96%, 73.83%]`, and drawdown interval `[0.543576, 1.382109]`.

The prior year's positive result does not replicate. Independent net expectancy
is negative, PF is below one, both directions are negative, and removal of the
best trade deepens the loss. Combined profit is concentrated in LINK
(`+0.883755`) while the overall portfolio is negative. Costs consume more than
the complete combined price edge.

## Decision

Independent classification: `FAILED INDEPENDENT VALIDATION`.

Final liquidity-sweep decision:

`FAILED INDEPENDENT VALIDATION — REJECT CURRENT STRATEGY`

This is a research status only and changes no production behavior. Phase 3 is
not permitted for the current sweep or continuation definitions. The next
permitted research step is a separately pre-registered new hypothesis; it may
not tune or subset these rejected results.
