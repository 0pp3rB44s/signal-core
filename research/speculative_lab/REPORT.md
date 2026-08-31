# AgentC speculative-market alpha hunt — pilot report

Generated from public Bitget GET data on 2026-08-31. This is research evidence,
not deployment authority. No credential, private log, production checkout, or
order path was used.

## Material result

SPECULATIVE_LAB_COMPLETE=NO
TOTAL_MARKETS_SCANNED=768
ELIGIBLE_SPECULATIVE_MARKETS=236
ARCHITECTURES_TESTED=6
BEST_ARCHITECTURE=FUNDING_CROWDING_CONTINUATION_WITH_10PCT_CATASTROPHIC_STOP
MECHANISM=EXTREME_PUBLISHED_FUNDING_PLUS_SAME_DIRECTION_24H_PRICE_EXTENSION
BEST_MARKET_CLASS=HV_A_HIGH_VOLATILITY_STRONG_LIQUIDITY
RAW_EVENTS_DAY=2.04
TRADEABLE_EVENTS_DAY=2.04_AT_CURRENT_100_USDT_DEPTH
COST_CLEARING_EVENTS_DAY=0.91_REALIZED_WINNERS_NOT_FORECASTABLE_COUNT
RAW_EDGE_BPS=329.96_MEAN_TAIL_DISTRIBUTION
CAPTURABLE_EDGE_BPS=71.80_TRIMMED_AND_EX_TOP1PCT_PROXY
TOTAL_COST_BPS=17.05_MEDIAN_AT_100_USDT_CURRENT_BOOK
NET_EDGE_BPS=54.74_USING_TRIMMED_PROXY
MOVE_TO_COST=4.21_USING_TRIMMED_PROXY
BEST_HORIZON=24H
SPOT_OR_FUTURES=BITGET_USDT_PERPETUAL
EXECUTION_STYLE=TAKER_ASSUMPTION_WITH_EXCHANGE_NATIVE_CATASTROPHIC_STOP
MEAN_RETURN=329.96_BPS
MEDIAN_RETURN=-45.65_BPS
TRIMMED_MEAN=71.80_BPS
RETURN_EX_TOP1PCT=71.80_BPS
TOP_SYMBOL_SHARE=32.26_PERCENT
TOP_5_SYMBOL_SHARE=78.93_PERCENT
TOP_WEEK_SHARE=36.63_PERCENT
CAPACITY_AT_100_USDT=SUPPORTED_BY_CURRENT_DEPTH_SNAPSHOT_ONLY
MAX_CAPACITY_BEFORE_IMPACT=AT_LEAST_1000_USDT_PROVISIONAL_CURRENT_DEPTH
ROBUSTNESS=TAIL_EDGE; POSITIVE_EX_TOP_EVENT_SYMBOL_AND_WEEK; NEGATIVE_MEDIAN;
CURRENT_UNIVERSE_LOOKAHEAD; NO_EVENT_TIME_BOOKS; GAP_STOP_UNMODELLED
STATUS=PROMISING
LIQUIDATION_COLLECTOR_STATUS=UNKNOWN_NO_MATCHING_PROCESS_OBSERVED_NOT_TOUCHED
PRODUCTION_TOUCHED=NO
ADAPTIVETREND_MODIFIED=NO
REAL_ORDERS_SENT=0

## Universe classifier

The current Bitget USDT-futures universe contained 768 normal contracts; 763
had at least 100 hourly bars. The forward-feature-inspired snapshot classifier
uses realized volatility, range, turnover, spread, price acceleration, volume
acceleration, current funding, observed history, and current depth:

- HV_A: 82 in the final run (high volatility, stronger liquidity/tighter spread).
- HV_B: 113 (high volatility, moderate liquidity).
- HV_C: 41 (extreme volatility, thinner but potentially executable).
- HV_D: 527 (not eligible under this snapshot).

The counts are a current snapshot, not a point-in-time historical membership
series. Applying them backward is a known classification look-ahead defect.

## Architecture outcomes

1. **Funding crowding continuation, 24h, HV_A — PROMISING / TAIL EDGE.**
   With a predeclared 10% catastrophic stop: 171 non-overlapping events across
   62 symbols over 83.8 effective days; 330.0 bps mean, -45.7 bps median,
   71.8 bps trimmed and ex-top-1% mean. First/second chronological halves were
   +156.3/+505.6 bps. Removing the top symbol leaves +179.5 bps; removing the
   top week leaves +187.9 bps. The 24.6% modelled stop-hit rate and negative
   median make this explicitly a tail architecture.
2. **Volatility expansion continuation, 24h, HV_A — PROMISING.** 5,315
   non-overlapping events across 82 symbols; 70.4 bps mean, 0 median, 48.0 bps
   trimmed, 33.7 bps ex top 1%, and 55.6 bps ex top week versus 17.8 bps median
   current cost. The breadth is better than funding crowding, but current-tier
   look-ahead remains.
3. **Attention/volume-shock continuation, 24h, HV_A — PROMISING / SECONDARY.**
   4,150 events across 81 symbols; 54.9 bps mean, -9.0 bps median, 37.1 bps
   trimmed, 27.3 bps ex top 1%, and 39.6 bps ex top week versus 17.6 bps cost.
4. **Pump-exhaustion reversal — WEAK/DEAD as a primary architecture.** The
   4h HV_B variant produced 14.1 bps gross versus 22.0 bps cost and turned
   negative after removing its top week. Longer stopped reversal variants fail.
5. **Funding-crowding reversal — DEAD.** Continuation, not reversal, owns the
   economically large 12h/24h response in this sample.
6. **Short-horizon generic continuation/expansion — DEAD/WEAK.** Most HV_A/B
   1h and 4h variants fail after current cost estimates.

Liquidation cascade, cross-venue lead/lag, and post-listing effects were mapped
but not claimed tested: real liquidation delivery is unproven, synchronized
historical books are absent, and a point-in-time listing universe is absent.

## Size-aware current execution snapshot

For the 62 symbols contributing funding events, all candidate rows had current
depth coverage. Median estimated round-trip costs (12 bps taker fees plus
spread plus measured depth slippage) were:

| Notional | Median cost | 90th-percentile cost |
|---:|---:|---:|
| $10 | 15.83 bps | 18.85 bps |
| $25 | 16.32 bps | 18.91 bps |
| $50 | 16.72 bps | 19.46 bps |
| $100 | 18.06 bps | 21.31 bps |
| $250 | 20.41 bps | 25.66 bps |
| $500 | 22.62 bps | 29.16 bps |
| $1,000 | 25.69 bps | 33.16 bps |

These are current snapshots, not event-time fills. They prove neither fill
probability nor gap-through-stop behavior. Maker execution was not credited.

## Evidence classification and blockers

- **PROVEN:** public-data code cannot send orders; 768 markets were scanned;
  the saved calculations and unit tests reproduce the stated sample results.
- **SUGGESTIVE / PROMISING:** funding-crowding continuation and broad HV_A
  24-hour continuation/expansion have after-current-cost statistical edge.
- **INSUFFICIENT SAMPLE:** funding events have only ~84 effective days after
  warm-up and only 171 non-overlapping risk-modelled observations.
- **OBSERVABILITY GAP:** historical event-time spread, depth, fill probability,
  OI interaction, and gap-through-stop execution are unavailable.
- **DEFECT:** current universe/tier membership is applied backward; delisted
  failures and historical HV_D/HV_A transitions are not reconstructed.
- **CAPITAL RISK:** a 10% catastrophic stop is very large for a ~$100 account;
  position risk sizing and cluster exposure cannot be approved from this pilot.

The genuine external blocker to `VALIDATION_READY`/`SHADOW_READY` is the lack
of an untouched prospective sample with point-in-time universe classification
and event-time executable books. Building shadow execution now would violate
the authorization condition `YES_AFTER_VALIDATION`.

## Updated architecture ranking

1. Funding/OI crowding continuation (funding-only pilot promising; OI pending).
2. Broad HV_A volatility-expansion continuation.
3. HV_A attention/volume-shock continuation.
4. Real liquidation cascades once event delivery is proven.
5. Cross-venue small-cap lead/lag after synchronized sample exists.
6. Post-listing discovery after point-in-time listing reconstruction.
7. Exhaustion reversal (demoted: current pilot weak/dead).

NEXT_AUTONOMOUS_STEP=FREEZE_RULES_AND_COLLECT_PROSPECTIVE_POINT_IN_TIME_SIGNALS_BOOKS_FUNDING_OI_WITHOUT_ORDERS
