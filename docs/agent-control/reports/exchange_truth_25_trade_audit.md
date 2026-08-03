# Exchange-truth 25-trade audit

## Evidence boundary

- LIVE SHA and supervisor pin: `817bc72f5b7b85f12b23fd6d7b03b09542addaed` (PROVEN)
- Deployment boundary: `2026-08-02T18:59:48Z` (PROVEN from runtime state, heartbeat and pin)
- Exchange source: Bitget authenticated GET endpoints through the normal Settings loader
- Economic authority: unique Bitget position-history `positionId`; local telemetry was used only for lifecycle/entry diagnostics
- Open window: `2026-08-02T19:40:34.147Z` through `2026-08-03T16:05:11.014Z`
- Close window: `2026-08-02T21:32:13.794Z` through `2026-08-03T16:39:30.796Z`
- Formula check: maximum absolute difference for gross - fees + funding = net was `0.00000002` USDT

## Frozen lifecycles

Exactly the first 25 qualifying exchange lifecycles, in open-time order:

```text
1467896146305904643
1467901535709720580
1467927626247729154
1467934090622300163
1467938181981302786
1467959137051242498
1467964397912223747
1467976509195710466
1467993066248499378
1468015708963762178
1468019380137390082
1468022234587754499
1468038301754224642
1468062469220626435
1468067455333724163
1468090404895879171
1468112574288130050
1468124349016469504
1468128990068371463
1468143920570068995
1468184385747054595
1468193300702457863
1468195742546554881
1468197756538089483
1468204330623070211
```

## Current exchange state

At the audit read: equity `51.63431373` USDT, available `34.66354040`, unrealized `-0.02932`; AVAXUSDT LONG 3.9 and BTCUSDT LONG 0.0004, both isolated, hedge-mode, leverage 3. Pending entries: 0. Active plan orders: four, exactly one SL and one TP per position. Orphan protection: 0. Ambiguous/blocking local intents: 0. This state was PROVEN protected at the read time; it is not a perpetual guarantee.

## Economics

Classification: ECONOMIC EDGE DEFECT (PROVEN for this window; not a universal strategy estimate).

- Trades 25; wins 12; losses 13; win rate 48.00%
- Gross PnL `-0.32703000`; fees `0.64942725`; funding `+0.00131929`; net `-0.97513810` USDT
- Expectancy `-0.03900552` USDT/trade
- Average/median winner `+0.01947244` / `+0.01417636`
- Average/median loser `-0.09298519` / `-0.08461040`
- Payoff ratio `0.20941446`; gross PF `0.63150719`; net PF `0.19330566`
- Break-even win rate `82.684641%`; max drawdown `1.00556656` USDT
- Max consecutive wins/losses: 4 / 5
- Average/median hold: 58.84795 / 63.17437 minutes
- Fees/trade `0.02597709`; fees/gross profit `115.876%`; fees/notional `0.119928%`
- LONG n=11: net `-0.77576005`, win rate 27.27%, net PF 0.0545 (PROVEN worse)
- SHORT n=14: gross `+0.1607`, net `-0.19937805`, win rate 64.29%, net PF 0.4865 (gross edge overwhelmed by costs)

Equity-change reconciliation for identical start/end boundaries is UNKNOWN because no matching historical account-equity snapshots were available.

## Entry, MFE and MAE

Coverage: planned/fill/MFE/MAE/TP1/SL 25/25; time-to-MFE 23; time-to-MAE 24. Movement at 10/30/60 seconds was UNKNOWN.

- Direction-adjusted plan-to-fill: average 19.11088 bps adverse; median 17.29356
- Actual-fill MFE: average 17.1924 bps; median 13.63
- Planned-entry MFE: average 36.2870 bps; median 31.7547
- Actual-fill MAE: average 23.3912 bps; median 19.34
- MFE minus round-trip costs: average 5.1995 bps; median 1.6549
- 12/25 had MFE <= costs; another 2 had MFE > costs but still lost net
- Median capture ratio 0.3457; individual ratios are noisy where realized movement is negative

Mutually exclusive categories (count 25, net sum exactly `-0.97513810`): DIRECT_FAILURE 3 (`-0.36615465`), FULL_QUALITY_WINNER 9 (`+0.19127775`), MFE_GT_COSTS_NET_LOSS 2 (`-0.14844254`), MFE_LE_COSTS 5 (`-0.53222888`), PROFITABLE_SCRATCH_PROTECTION_CLOSE 1 (`+0.02356860`), TIMEOUT_WITHOUT_EDGE 3 (`-0.16198134`), TIMEOUT_WITH_PROFIT_LOCK 2 (`+0.01882296`).

## Funnel, routing and strategy

- All 25 have detector, selector, score, risk, planner, executable, submit, fill, protection and close evidence.
- 53 executable cycles: 40 single-candidate and 13 multi-candidate; portfolio-ranking events: 0 (OBSERVABILITY GAP).
- Maker attempts 25; full fills 0; partial fills 0; no-fill 25; market fallback 25; protected 25.
- Fallback delay average/median 5.5473/5.3886 seconds. Pre-submit spread/quote telemetry: UNKNOWN.
- All 25 maker intents remained stale `SUBMITTED` despite zero pending exchange orders (DEFECT; fixed in this release).
- `low_vol_reclaim` n=19, net `-0.81177373`, average `-0.042725`, wins 8: negative window (PROVEN).
- Other strategies and every symbol: INSUFFICIENT SAMPLE.
- All scores were >=90, providing no observed discrimination. Entry-quality buckets showed weak/no useful separation.

## Backtest decision

The existing output is INVALID for parameter approval: it is mixed historical log analysis, not a reproducible simulation of current strategy/risk/routing code with maker/fallback, funding, intrabar ordering, portfolio caps, out-of-sample and walk-forward evidence. No strategy or risk parameters were optimized or changed.

## Proven defects and release response

1. Provisional/percentage close data could contaminate economics or block the true exchange row: DATA INTEGRITY DEFECT; fixed with exchange reconciliation, economic-source gates and dedup.
2. Recovery helper lacked startup/periodic production wiring: PIPELINE INCOMPLETE; fixed on the real PositionManager sync path, bounded to 20 recent closes.
3. Cancelled maker intents remained recoverable `SUBMITTED`: OPERATIONAL RISK/DATA DEFECT; terminalized only after successful cancel path plus absent-position verification.
4. launchd exited successfully when the authorised launcher-created engine already existed: OPERATIONAL RISK; fixed by adopting and monitoring that PID.
5. Emergency-flatten economics: FUTURE PATCH. No production caller exists in this base, so it is not claimed WIRED.
6. Portfolio ranking and decision-time quote capture: OBSERVABILITY GAP/FUTURE PATCH; excluded because the current audit does not justify critical-path or selection changes.

## Release and rollback

Base `817bc72f5b7b85f12b23fd6d7b03b09542addaed`; branch `codex/exchange-truth-integrity-release`. Rollback is to leave the draft PR unmerged, or revert the release commits after owner review. No LIVE checkout, process, configuration, order or protection was mutated during this work.
