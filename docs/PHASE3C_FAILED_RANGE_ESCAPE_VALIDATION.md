# Phase 3C — failed range escape reversal v1 locked validation

## Contract integrity and freeze

The frozen Phase 3B commit was
`924e2a4789eb60555c7d08734893c7b4758f305c`. Before implementation, the
canonical preregistration validated without modification and reproduced hash
`e7117eefbf5e387646f2a5bceb444d5125a46c56b438eb6f2c8d2e6f69077da9`.

The research-only implementation was frozen before validation in commit
`dbfafe6`. Its manifest hash is
`6acdcfb183e4a94c590070a74c1801b57d609828bbe3e67246dcaf03c03006bb`.
Detector hash is `db4887970bbf4d0f38db124237531f6b557f5d5870437f28e343e16def5b8ee7`;
the unchanged generic execution-contract hash is
`fd93cd8900ea4e36c68bda878bde895484ce32ee62e479b7f623245f8b04f0eb`.

No production registry, selector, live route, paper route, risk rule or runtime
configuration can import this strategy. Validation was opened once only after
the implementation commit.

## Exact implementation

ATR14 is the arithmetic mean of fourteen true ranges ending at the fully
closed re-entry candle. The 20-candle range ends immediately before the escape.
The escape and immediate body-led re-entry follow the frozen 0.10 ATR, 0.15 ATR
and 0.50 body/range tests. Fully closed 1h EMA20/EMA50 is metadata only.

Entry is the next 15m open with 4 bps spread, 2 bps adverse entry slippage and
6 bps taker fee. Stop is the escape extreme and cannot exceed 2 ATR. TP1 is
1.2R for 40%, final target 2R, fee-adjusted break-even is 12 bps and time exit
is exactly candle 8. The generic contract has a known branch where a first TP1
on candle 8 returns the remainder open; the research runner closes only that
remainder at candle 8 using the same adverse exit, rounding and fee formulas.
The generic contract itself remains byte-unchanged.

## Development reconciliation

| Stage | Development |
|---|---:|
| Snapshots | 280,320 |
| Raw escape-threshold events | 24,945 |
| Valid body-led re-entries | 4,349 |
| LONG candidates | 1,180 |
| SHORT candidates | 1,134 |
| Stop-distance rejected | 401 |
| Cost-distance rejected | 1,634 |
| Duplicate suppressed | 0 |
| Active-overlap suppressed | 3 |
| Execution attempted | 2,311 |
| Execution rejected | 72 |
| Filled / closed / unresolved | 2,239 / 2,239 / 0 |

The 72 execution rejects are all `ZERO_EXECUTABLE_QTY` under the frozen
minimum quantity, quantity step and 35 USDT notional cap. Two reconciliation
runs were byte-identical before freeze. The tracked manual file contains three
LONG and three SHORT examples, an ADA stop-distance rejection, an ADA active
overlap, a WIF TP1-plus-break-even, a WIF full stop and an AVAX maximum-hold
exit, including every requested range, threshold, timestamp and price field.

## Locked validation funnel

| Stage | Development | Validation |
|---|---:|---:|
| Snapshots | 280,320 | 280,320 |
| Raw escape-threshold events | 24,945 | 22,592 |
| Valid body-led re-entries | 4,349 | 3,723 |
| LONG candidates | 1,180 | 749 |
| SHORT candidates | 1,134 | 761 |
| Stop-distance rejected | 401 | 426 |
| Cost-distance rejected | 1,634 | 1,787 |
| Duplicate suppressed | 0 | 0 |
| Active-overlap suppressed | 3 | 4 |
| Execution attempted | 2,311 | 1,506 |
| Execution rejected | 72 | 67 |
| Filled | 2,239 | 1,439 |
| Closed | 2,239 | 1,439 |
| Unresolved | 0 | 0 |

All validation execution rejects are also `ZERO_EXECUTABLE_QTY`. Candidate
attrition is fully reconciled: `3,723 - 426 - 1,787 = 1,510` candidates, minus
four overlaps = 1,506 attempts, minus 67 exchange-constraint rejects = 1,439
closed trades.

## Performance

| Metric | Development | Locked validation |
|---|---:|---:|
| Closed trades / trading days | 2,239 / 359 | 1,439 / 319 |
| Gross price PnL | +19.253319 | -1.797002 |
| Execution-adjusted gross PnL | -78.836827 | -68.880076 |
| Entry / TP1 / final fees | 46.876144 / 5.899959 / 40.970020 | 30.105639 / 3.265432 / 26.845240 |
| Spread / slippage impact | 65.985163 / 32.104983 | 45.144778 / 21.938296 |
| Total costs | 191.836270 | 127.299386 |
| Net PnL / return | -172.582951 / -17.2583% | -129.096388 / -12.9096% |
| Ending equity | 827.417049 | 870.903612 |
| PF | 0.661952 | 0.566146 |
| Gross / net expectancy | +0.008599 / -0.077080 | -0.001249 / -0.089713 |
| Expectancy R | -0.011266 | -0.012796 |
| Win rate / payoff ratio | 43.23% / 0.8692 | 41.70% / 0.7917 |
| Average win / loss | +0.349116 / -0.401674 | +0.280768 / -0.354657 |
| Median / best / worst | -0.149411 / +2.013497 / -1.957354 | -0.149492 / +1.259382 / -1.291801 |
| Max drawdown | 173.328942 (17.3200%) | 129.177735 (12.9178%) |
| Average hold | 5.064 candles | 5.469 candles |
| TP1 / BE / final / stop / time | 31.58% / 8.80% / 13.00% / 43.37% / 34.84% | 27.24% / 6.95% / 9.45% / 41.14% / 42.46% |
| Average MFE / MAE | 1.1351R / 1.2591R | 0.9966R / 1.2966R |
| Adverse first | 76.06% | 79.15% |

## Breakdown findings

Both directions lose: LONG `-59.851064` net at PF `0.5910`; SHORT
`-69.245324` at PF `0.5421`. Every traded symbol loses net. BTC has 67 valid
candidates but zero executable quantity; the other seven symbols have 114–291
trades. Only June 2026 is marginally positive (`+0.090867`, PF `1.0039`); all
other months lose.

Asia, Europe and US lose respectively `-35.825970`, `-43.064277` and
`-50.206140`. Both EMA contexts lose. Low, medium and high volatility regimes
all lose. Every stop-distance and escape-distance bucket loses net. These
segments are diagnostic and none is used to rescue the official result.

## Cost, dependency and uncertainty

Validation gross price edge per trade is `-0.001249` versus average transaction
cost `0.088464`; edge/cost is `-0.0141`. Costs are 65.41% of positive
execution-adjusted gross profit. Net without the best trade is `-130.355769`,
without the best three `-132.642977`, and without the worst `-127.804586`.
No symbol produces positive net profit; the sole positive month supplies 100%
of positive monthly net profit.

With seed 20260715 and 5,000 resamples, validation mean-net CI is
`[-0.109307, -0.070501]`, mean-R CI `[-0.015589, -0.010046]`, PF CI
`[0.496175, 0.641433]`, and Wilson win-rate CI `[39.17%, 44.26%]`. Monte Carlo
ending-equity q05/median/q95 is `847.283/870.629/894.433`; maximum-drawdown
q05/median/q95 is `107.113/130.463/153.631`.

## Immutable acceptance matrix

| Criterion | Actual | Result |
|---|---:|---|
| ≥30 closed trades | 1,439 | PASS |
| Gross expectancy >0 | -0.001249 | FAIL |
| Net expectancy >0 | -0.089713 | FAIL |
| PF >1.15 | 0.566146 | FAIL |
| Positive without best trade | -130.355769 | FAIL |
| Symbol positive-profit share ≤50% | unavailable: no profitable symbol | FAIL CLOSED |
| Month positive-profit share ≤40% | 100% | FAIL |
| Costs/gross profit <70% | 65.41% | PASS |
| Drawdown ≤5% | 12.92% | FAIL |
| Drawdown ≤1.5× positive net | net is negative | FAIL |
| Development/validation gross signs agree | positive / negative | FAIL |
| Bootstrap mean-R lower bound ≥-0.05R | -0.015589R | PASS |
| Three symbols with ≥5 trades and positive gross price result | 4 | PASS |

Nine of thirteen mandatory criteria fail. The sample is ample and the failures
are substantive, so `INCONCLUSIVE` does not apply.

## Verdict

**REJECTED — FAILED LOCKED VALIDATION**

This result does not permit production, forward-paper or live promotion. The
only permitted next action is to archive v1 as rejected and return to a new,
separately preregistered causal hypothesis; no subset or parameter rescue of v1
is allowed.

Official hashes:

- development evaluation: `8931ae609540439fa2f750c81d45858e2bce40271271266f0735cd490eaec9ca`
- locked validation: `8bc58ecbe1143e898dd50045837f6d0faca5a72a44e646a2498f82dda364116d`
- final evaluation: `32d6fac939ab2c8ad0e7d21fe87e6343ba1fc956ba16236fce4b734b1a3f4d56`

The complete locked candidate and trade records are retained as deterministic
gzip artifacts in `research/artifacts/phase3c_failed_escape_v1/`. The official
uncompressed run and its diagnostic JSON remain under
`reports/analysis/phase3c_locked_validation_official/`.
