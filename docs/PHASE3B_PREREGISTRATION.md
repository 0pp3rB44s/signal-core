# Phase 3B — frozen preregistration

## Frozen research state

This branch starts at Phase 3A commit
`4c44d3fbeeb694091fdd523293ffe3980edd8517`. The reproduced Phase 3A artifact
hash is `ddb74fedb1cdae6b1a0b4f603434aa714b886f60b1634bc7a3d162bd59244336`;
control and confirmation remain unchanged. Phase 3B changes no production
registry, detector, runner, live path or paper path.

Rejected research statuses live only in `research/strategy_status.json`.
Implementations remain present: continuation, breakdown and both sweep versions
are rejected; breakout has insufficient sample and low-vol reclaim had no
candidates in the frozen sample.

## Evidence inventory

The inventory reads only canonical 15m OHLCV candles for eight Bitget
USDT-futures symbols. It never imports the backtest engine, constructs a
strategy, executes an order or reads a trade/report artifact. Future close
displacement, MFE and MAE are direction-normalized in ATR and describe price
behaviour—not trades, returns or PnL.

Development is 2024-07-15 00:00 UTC to 2025-07-15 00:00 UTC. The separately
reported descriptive validation year is 2025-07-15 00:00 UTC to 2026-07-15
00:00 UTC. Each has 35,040 gap-free candles for BTC, ETH, SOL, ADA, AVAX, LINK,
SUI and WIF. Two runs are byte-identical with evidence hash
`662f35b959914e4f2d308a6b48513def7af1911cbcdabba1ceb4047b1ac1e68f`.

| Family | Development observations | Locked-year descriptive stability | Decision |
|---|---|---|---|
| Compression breakout | 3,239; 63.66% failed within four candles; close-8 +0.080 ATR | 2,821; 64.34% failed; close-8 -0.080 ATR | Reject: false breaks dominate and sign reverses |
| Trend pullback continuation | 43,052; close-4/8/16 +0.006/+0.018/+0.033 ATR | 43,714; -0.009/-0.035/-0.063 ATR | Reject: negligible and contradictory |
| Failed breakout reversal | 9,428; median close-4/8/16 +0.036/+0.028/+0.023 ATR | 8,114; +0.068/+0.086/+0.054 ATR | Select family, with adverse-path caveats |
| Volatility expansion after consolidation | 3,331; close-8/16 +0.087/+0.102 ATR | 3,780; -0.081/-0.094 ATR | Reject: contradictory years |
| Extreme mean-reversion reclaim | 532; directional close generally negative | 653; directional close generally negative | Reject: reclaim did not precede reversion |

Breakout diagnostics show high retest/failure frequency. Generic mean-reversion
medians are mildly positive, but means and validation are inconsistent and MAE
exceeds MFE. Session transitions change volatility, not direction. Failed-
breakout direction, symbol, session and HTF strata justify no filter: the better
direction switches by year and HTF alignment is not consistently superior.

## Selected hypothesis: failed_range_escape_reversal_v1

A close outside a 20-candle range can trap continuation participants when the
immediately following candle closes materially back inside with an opposing
body. Their unwind may carry price away from the failed boundary; the escape
extreme supplies causal invalidation. This is not the rejected liquidity sweep:
it requires a **closed range escape** and immediate **closed body-led re-entry**,
not an intrabar wick/reclaim construction.

The selected descriptive screen—not an executed strategy—has 2,312 development
and 1,506 locked-year observations across both directions, all symbols and all
sessions. Median modeled TP1 distance is 113.54 and 105.09 bps; median 8-candle
MFE is 1.167 and 1.087 ATR. TP1-equivalent price was touched in 38.41% and
34.93%. Validation mean close-8 is -0.148 ATR and mean MAE exceeds MFE. These
are preregistration risks, not permission to tune.

## Frozen deterministic specification

The canonical formulas are in
`research/preregistrations/failed_range_escape_reversal_v1.json`.

- Setup: 15m range over 20 fully closed candles before escape. Escape close at
  least 0.10 ATR14 outside; immediate next closed candle at least 0.15 ATR14
  inside, body/range at least 0.50 and body in reversal direction. ATR must be
  finite and positive. Latest fully closed 1h EMA20/50 must exist and is recorded
  but not gated. TP1 distance must be at least 72 bps. Volume ratio is recorded.
- Entry: LONG after failed downside escape, SHORT after failed upside escape;
  market at exactly the next 15m open, else expire. No entry/later data is used.
- Invalidation: escape low for LONG or escape high for SHORT; entry-to-stop at
  most 2.0 ATR14. Invalid geometry or incomplete data fails closed.
- Exit: 40% at 1.2R, remainder at 2.0R; after completed TP1, fee-adjusted
  break-even using frozen 12 bps entry-plus-exit allowance; exit after 8 candles.
- Risk: frozen 0.75% equity risk, 5x leverage cap, 35% maximum-notional rule and
  frozen exchange/execution constraints.
- Suppression: identity is hypothesis, symbol, direction and failure-candle open
  timestamp. Same-candle duplicates and active same-symbol overlap are blocked;
  no global cooldown; existing selector resolves conflicts.

There are five setup parameters, one entry-timing parameter, one stop parameter,
two target parameters and one maximum-hold parameter. No grid search or variant
comparison occurred.

## Cost viability

Frozen round-trip execution cost is 24 bps. Eligibility requires modeled TP1
of at least `3 × 24 = 72` bps. Screen medians of 113.54 and 105.09 bps pass this
mechanical condition. This does not prove net viability: next-open movement,
path ordering, stops, partials and costs await the single Phase 3C test.

## Locked Phase 3C protocol

Phase 3C must implement and test v1 on development data first. Validation
execution unlocks only when JSON hash
`e7117eefbf5e387646f2a5bceb444d5125a46c56b438eb6f2c8d2e6f69077da9`
matches, candidate reconciliation passes and tests are green. Phase 3B read no
validation PnL; tooling rejects report/trade/execution paths and performance
fields.

Locked validation accepts only if **all** hold:

1. at least 30 closed trades;
2. gross price expectancy and net expectancy are positive;
3. profit factor is above 1.15;
4. net result without the best trade is positive;
5. no symbol exceeds 50% and no month 40% of positive profit;
6. costs are below 70% of gross profit;
7. drawdown is at most 5% of starting equity and 1.5 times total net profit;
8. development and validation gross-expectancy signs agree;
9. bootstrap 95% mean-net-expectancy lower bound is at least -0.05R;
10. three symbols each have at least five trades and positive gross expectancy.

Any false criterion rejects v1. Leakage, hash change, overlapping periods,
reconciliation failure or unresolved outcomes technically invalidates it without
interpreting performance. Criteria cannot be weakened. Any rule, threshold,
cost or semantic change creates a new version and invalidates v1.

## Minimal Phase 3C plan and limitations

Phase 3C may add one research-only detector, deterministic tests, one development
reconciliation and one hash-gated validation run. It must not register in
production or change selector, risk or execution.

Risks are path ordering, overlapping observations, weak validation mean,
similar MFE/MAE and a descriptive touch measured from failure close rather than
executable next open. These are why the hypothesis is preregistered, not promoted.

Document hash: `e7117eefbf5e387646f2a5bceb444d5125a46c56b438eb6f2c8d2e6f69077da9`.
