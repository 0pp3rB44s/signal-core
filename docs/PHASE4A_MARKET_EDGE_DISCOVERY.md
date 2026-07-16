# Phase 4A — market edge discovery map

## Frozen predecessor

Phase 3C is frozen at commit
`cad0693d24ee245245d479a5d23a066d32ed50f4`. Its locked validation hash is
`8bc58ecbe1143e898dd50045837f6d0faca5a72a44e646a2498f82dda364116d`
and its evaluation hash is
`32d6fac939ab2c8ad0e7d21fe87e6343ba1fc956ba16236fce4b734b1a3f4d56`.
No production, paper or live file changed. The archived status of
`failed_range_escape_reversal_v1` is **PERMANENTLY REJECTED — NEGATIVE LOCKED
GROSS EDGE**; implementation history remains intact.

## Discovery contract

The analysis uses exactly the eight frozen Bitget USDT-futures symbols at 15m:
ADA, AVAX, BTC, ETH, LINK, SOL, SUI and WIF. Development is
2024-07-15T00:00:00Z through 2025-07-15T00:00:00Z; replication is the disjoint
2025-07-15T00:00:00Z through 2026-07-15T00:00:00Z year. Each symbol/year has
35,040 contiguous, unique, valid candles.

For every eligible closed candle, LONG and SHORT views cover 1, 2, 4, 8, 16
and 32 candles. Outcomes include close return, MFE, MAE, MFE-minus-MAE,
time-to-extremes, ±0.25/0.50/0.75/1.00% reach and first-reach ordering. No fee,
execution, trade or PnL model is invoked.

The transparent feature set covers EMA trend and slope, momentum, ATR and
realised volatility, range and Bollinger compression, participation volume,
rolling/session/day location, candle structure, UTC calendar/session and
synchronised BTC/broad-market context. Candle volume is participation, never
orderbook liquidity. Continuous boundaries are development-only
10/25/50/75/90 percentiles and are frozen unchanged for replication.

All source candles are closed. The 1h context samples only the fourth closed
15m candle of each UTC hour and forward-fills that completed context. Cross-
symbol values are joined only on identical historical timestamps. Forward
arrays are built after features and never enter feature construction. Rejected
strategy results are not imported or used as labels.

Inference is deliberately conservative: normal-test standard errors use UTC
daily aggregates, bootstrap confidence intervals resample whole UTC-day
blocks, and Benjamini–Hochberg is applied independently to every year's full
single-factor hypothesis family. This prevents overlapping horizons and
synchronised symbols from masquerading as independent samples.

## Unconditional baselines

Mean LONG close return changes sign between years at every horizon:

| Horizon | Development mean | Replication mean | Development MFE / MAE | Replication MFE / MAE |
|---:|---:|---:|---:|---:|
| 1 | +0.0023% | -0.0020% | 0.3528 / 0.3536% | 0.2693 / 0.2772% |
| 2 | +0.0045% | -0.0042% | 0.5060 / 0.5072% | 0.3878 / 0.4029% |
| 4 | +0.0090% | -0.0084% | 0.7246 / 0.7261% | 0.5548 / 0.5834% |
| 8 | +0.0179% | -0.0170% | 1.0386 / 1.0394% | 0.7914 / 0.8445% |
| 16 | +0.0355% | -0.0344% | 1.4942 / 1.4909% | 1.1279 / 1.2230% |
| 32 | +0.0714% | -0.0691% | 2.1621 / 2.1360% | 1.6147 / 1.7764% |

SHORT close returns are the exact orientation mirror. The year-level drift
reversal is a material warning against interpreting pooled two-year movement
as a stable directional edge.

## Single-factor results

There are 3,528 tests in each year. Development has 24 BH-adjusted discoveries
at q ≤ 0.05; replication has zero. Consequently, zero single-factor effects
meet sign agreement plus adjusted evidence in both years, and zero meet the
economic and stability contract.

The strongest development-only large-movement states fail replication:

| State | Horizon | Development mean | Replication mean | Replication q |
|---|---:|---:|---:|---:|
| top-10% realised volatility | 32 | +0.6534% | -0.1937% | 0.9149 |
| top-10% ATR% | 32 | +0.6283% | -0.0670% | 0.9225 |
| top-10% candle range | 32 | +0.4870% | -0.0603% | 0.9149 |
| top-10% broad dispersion | 16 | +0.2384% | +0.0590% | 0.9149 |
| bottom-10% relative-to-BTC return | 2 | +0.0514% | +0.0150% | 0.9149 |

Some replication raw confidence intervals exclude zero, but after the frozen
3,528-test BH family the minimum replication q is 0.5749. They are raw clues,
not replicated discoveries.

## Pairwise interactions

No factor survived the required single-factor replication screen. Therefore
the causal pair-selection gate selected zero interactions, well below the
maximum of ten. Pairwise development and replication output files are empty by
design: calculating combinations after their prerequisite evidence failed
would be an unregistered brute-force search. Five causal candidate pairs remain
declared in code but were not promoted into the official selected registry.

## Economic relevance and stability

No statistically replicated state reached the economic screen. In particular,
a high MFE is not treated as directional edge when mean close return is below
24 bps or adverse-first dominates. With zero replicated effects there is no
honest symbol/month/session/exclusion stability claim to make; the machine
artifacts report empty stability and economic-shortlist tables rather than
manufacturing labels.

## Verdict and next investigation

**NO REPLICATED ECONOMIC EDGE FAMILY FOUND**

The one recommended next step is to add synchronized historical funding and
open-interest data. The OHLCV-only state map produced substantial movement but
no year-replicated direction after dependency-aware inference; positioning and
carry data are the most direct way to test whether that movement has an
observable directional cause.

## Reproducibility and artifacts

The official outputs are in
`reports/analysis/phase4a_market_edge_discovery_run5/`. A second complete run
in `phase4a_market_edge_discovery_run6/` is byte-identical. Both artifact hashes
are `b3759b5ab034616b7b7e192caa5a593552633d56ed77dea90edd4d0172302286`.
The first and second complete runs took 506.05 and 524.43 seconds. The tracked
run contains the feature dictionary, frozen bins, unconditional baselines,
single-factor results, stability results, interaction registry/results,
economic screen, FDR report, shortlist/rejections and manifest.
