# Phase 4C — basis and mark–index divergence discovery

## Freeze and scope

Phase 4B is frozen at `aaeb856e8eb2375b20fd1452afdb74f8c360acd7`.
Its feasibility artifact (`5d579c800126fd3f8268e8e5f4a45667f030f461ce137bb0a2103e4108588bc2`),
funding manifest (`329e9f4bcce403bca38ab66ab6dfda5089f79811f753289983d4dbd90011721a`)
and conclusion `NO REPLICATED FUNDING/OI EDGE FAMILY FOUND` remain unchanged.
This phase creates no strategy, trades, trade PnL, or runtime changes.

## Source feasibility

The official Bitget USDT-futures endpoints are `/api/v2/mix/market/history-candles`,
`/api/v2/mix/market/history-mark-candles`, and
`/api/v2/mix/market/history-index-candles`. All support 15m, at most 200 records,
a maximum 90-day query window, and 20 requests/second/IP. Timestamps are UTC
candle-open timestamps; historical responses contain finished candles. Market
candles include genuine volume; mark and index candles do not. No volume was
copied between price types. A separate premium history is unnecessary because
the transparent premium series is computed from synchronized prices.

| Dataset | Common coverage | Granularity | Symbols | Limitation | Verdict |
|---|---|---:|---:|---|---|
| Market/last-price | 2024-07-15–2026-07-15 | 15m | 8/8 | Market volume only | FULLY AVAILABLE |
| Mark-price | 2024-07-15–2026-07-15 | 15m | 8/8 | No volume | FULLY AVAILABLE |
| Index-price | 2024-07-15–2026-07-15 | 15m | 8/8 | No volume | FULLY AVAILABLE |

Acquisition uses explicit ranges, chronological 200-candle pages, bounded
retries, an 18 rps shared limiter, raw-page resume, atomic writes, deterministic
gzip, duplicate/gap/OHLC/alignment checks, SHA-256 files and a manifest. It does
not interpolate, forward-fill, substitute prices, use credentials, or read live
runtime data.

## Dataset and no-lookahead contract

ADA, AVAX, BTC, ETH, LINK, SOL, SUI, and WIF each have 70,080 MARKET, 70,080
MARK, and 70,080 INDEX candles: 1,681,920 canonical price records. The common
window is `[2024-07-15T00:00:00Z, 2026-07-15T00:00:00Z)`. All 24 series have
zero gaps, duplicates, invalid OHLC, non-positive prices, or alignment errors.
Exact synchronization yields 560,640 observations, 100% synchronized, with no
symbol exclusions. Dataset hash:
`21bcbc83ebdc26430bbc259c4db9d1929a99d70bad8dcc1272a58692bce57b6a`.

The join requires identical closed 15m candle-open timestamps for all three
prices. Missing data rejects the timestamp; future mark/index data cannot attach
to an earlier market candle. The frozen split is development
`[2024-07-15, 2025-07-15)` and replication `[2025-07-15, 2026-07-15)`.

## Feature, bin, and outcome contracts

Features are `10000*(market-index)/index`, `10000*(mark-index)/index`, and
`10000*(market-mark)/mark`. Basis changes use 1/4/16/32/96-candle offsets.
Intracandle extrema are unavailable because OHLC high/low event times are
unknown; pairing them would invent simultaneity. Development alone freezes the
six quantile bins and replication never re-estimates them.

Forward outcomes use only future MARKET candles over 1, 2, 4, 8, 16, and 32
candles. They include directional close return, MFE, MAE, ordering/time, and
24/48/72/100-bps reach. These are price outcomes, never trade PnL.

## Primary registry and effects

Eight hypotheses were frozen: positive/negative market-basis convergence;
positive/negative mark-basis convergence; market above/below both references;
rapid absolute 1h basis expansion; and basis sign reversal. Each has one fixed
direction/horizon, at least 500 observations, a contradiction rule, and a
24-bps economic floor. No exploratory hypotheses or interactions were opened.

| ID | Feature/direction | Dev mean % / q / fav-first | Rep mean % / q / fav-first | Result |
|---|---|---|---|---|
| B1 | positive market basis / SHORT | -.0394 / .6582 / .379 | +.1274 / .1717 / .422 | sign reversal |
| B2 | negative market basis / LONG | +.1226 / .3183 / .417 | +.0254 / .6518 / .446 | too small |
| B3 | positive mark basis / SHORT | -.0716 / .6009 / .368 | +.1062 / .4837 / .411 | sign reversal |
| B4 | negative mark basis / LONG | +.0258 / .7603 / .394 | +.0600 / .4837 / .430 | too small |
| B5 | market above references / SHORT | +.0117 / .6009 / .391 | +.0427 / .0551 / .441 | too small |
| B6 | market below references / LONG | +.0243 / .1173 / .410 | +.0036 / .6518 / .415 | too small |
| B7 | rapid expansion / against sign | +.0300 / .1173 / .367 | +.0101 / .6518 / .422 | too small |
| B8 | sign reversal / new sign | -.0195 / .0028 / .344 | -.0193 / .0014 / .392 | wrong sign; uneconomic |

B8 is the only adjusted result in both periods, but contradicts its expected
direction, is about 2 bps versus the 24-bps screen, and is adverse-first
dominated. It is evidence against the registry, not an opposite strategy.

## Events, funding overlay, stability, and decision

Extreme entries, sign reversals, rapid expansion and compression use a frozen
32-candle cooldown. At 16 candles, development mean drifts range +0.0080% to
+0.1405%; replication ranges -0.1786% to -0.0099%. Effects generally reverse
or collapse and none has economic favourable-first timing.

The 2026-04-17–2026-07-14 funding tail is `SUPPLEMENTAL_ONLY`. Its manifest is
available, but canonical funding records are outside this portable dataset, so
the overlay remains empty and fail-closed. Missing funding is never zero and
cannot define thresholds or justify a strategy.

Zero candidates passed sign, adjusted evidence, economics and timing, so none
was eligible for conditional stability exclusions. The empty stability artifact
means “not opened,” not “passed.” All primaries span 8 symbols and 11–13 months.

There are 8 primary and 0 exploratory hypotheses. Development has 1 q≤.05
result, replication has 1, and replicated economic families total 0.

**NO REPLICATED BASIS / MARK–INDEX EDGE FAMILY FOUND**

The single next direction is **test a higher timeframe**. Complete clean data
removes availability as the explanation; short-horizon effects are small,
adverse-first dominated, or unstable. Aggregation is the narrowest next test.

## Reproducibility

Two complete builds are byte-identical with artifact hash
`6072ac1e14e6cbc69990fa5774e20e464bb7267a27d1587f3f6a17fd6ed19d92`.
Versioned artifacts are under
`reports/analysis/phase4c_basis_edge_discovery_run2/`; run3 is the independent
witness. The focused suite has exactly the 22 required contracts.
