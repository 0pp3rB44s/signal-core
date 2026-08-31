# Speculative-market mechanism map

This is the pre-optimization ranking. Values are hypotheses to test, not results.
All costs are round-trip and must be replaced by measured, size-aware estimates.

## 1. Attention/flow-cascade continuation

NAME=ATTENTION_FLOW_CASCADE_CONTINUATION
WHO_CREATES_EDGE=Late retail attention, cross-venue followers, short covering
WHY_EDGE_EXISTS=Information and attention diffuse across a fragmented speculative universe
WHY_IT_PERSISTS=Capacity limits and mandate constraints deter larger systematic capital
EXPECTED_RAW_MOVE_BPS=80-400
EXPECTED_CAPTURE_BPS=35-180
EXPECTED_EVENTS_DAY=3-30 across the full universe
EXPECTED_HOLD=5m-4h
EXPECTED_COST=20-80bps
DATA_REQUIRED=OHLCV, trades, spread, depth, cross-sectional breadth
MAIN_FAILURE_MODE=Buying a completed pump; jackpot concentration
CONFIDENCE=MEDIUM

## 2. Pump exhaustion / failed continuation

NAME=PUMP_EXHAUSTION_REVERSAL
WHO_CREATES_EDGE=Exhausted late buyers, profit takers, deleveraging longs
WHY_EDGE_EXISTS=Attention bursts can overshoot executable near-term demand
WHY_IT_PERSISTS=Shorting constraints and violent squeeze risk limit arbitrage capital
EXPECTED_RAW_MOVE_BPS=100-600
EXPECTED_CAPTURE_BPS=40-220
EXPECTED_EVENTS_DAY=1-15
EXPECTED_HOLD=15m-12h
EXPECTED_COST=25-100bps
DATA_REQUIRED=OHLCV, aggressive flow, spread, depth, funding, OI
MAIN_FAILURE_MODE=Fading the first leg of a multi-wave narrative move
CONFIDENCE=MEDIUM

## 3. Liquidation-cascade continuation/reversal

NAME=FORCED_FLOW_CASCADE
WHO_CREATES_EDGE=Leveraged liquidations and exchange risk engines
WHY_EDGE_EXISTS=Forced orders are price-insensitive and can propagate across venues
WHY_IT_PERSISTS=Events are sparse and require reliable real-event data
EXPECTED_RAW_MOVE_BPS=100-800
EXPECTED_CAPTURE_BPS=50-300
EXPECTED_EVENTS_DAY=0.5-10
EXPECTED_HOLD=1m-4h
EXPECTED_COST=25-120bps
DATA_REQUIRED=Real liquidation events, OI, volume, depth, cross-venue prices
MAIN_FAILURE_MODE=No proven event delivery; arriving after forced flow completes
CONFIDENCE=MEDIUM_MECHANISM_LOW_DATA

## 4. Funding extreme plus price/OI crowding

NAME=LEVERAGED_CROWDING_STATE
WHO_CREATES_EDGE=One-sided leveraged speculators and subsequent squeezes
WHY_EDGE_EXISTS=Funding, OI, and price extension jointly expose unstable positioning
WHY_IT_PERSISTS=Direction is state-dependent; static carry tests miss the interaction
EXPECTED_RAW_MOVE_BPS=60-350
EXPECTED_CAPTURE_BPS=25-140
EXPECTED_EVENTS_DAY=1-20
EXPECTED_HOLD=1h-24h
EXPECTED_COST=18-70bps
DATA_REQUIRED=Funding history, OI history, OHLCV, spread, depth
MAIN_FAILURE_MODE=Extreme funding persists while price trends further
CONFIDENCE=MEDIUM_LOW

## 5. Volatility expansion after compression

NAME=VOLATILITY_REGIME_EXPANSION
WHO_CREATES_EDGE=Option-like convex demand and stop/trigger orders
WHY_EDGE_EXISTS=Volatility clusters and threshold orders amplify transitions
WHY_IT_PERSISTS=Direction must be inferred and false starts are common
EXPECTED_RAW_MOVE_BPS=50-250
EXPECTED_CAPTURE_BPS=20-100
EXPECTED_EVENTS_DAY=5-40
EXPECTED_HOLD=15m-12h
EXPECTED_COST=18-70bps
DATA_REQUIRED=OHLCV, ATR/range, volume, spread, depth
MAIN_FAILURE_MODE=Expansion candle contains the full move
CONFIDENCE=MEDIUM_LOW

## 6. Cross-venue small-asset lead/lag

NAME=FRAGMENTED_LIQUIDITY_LEAD_LAG
WHO_CREATES_EDGE=Venue-specific informed or urgent flow
WHY_EDGE_EXISTS=Thin fragmented books update unevenly
WHY_IT_PERSISTS=Low capacity and infrastructure burden
EXPECTED_RAW_MOVE_BPS=20-180
EXPECTED_CAPTURE_BPS=10-80
EXPECTED_EVENTS_DAY=5-100
EXPECTED_HOLD=1s-5m
EXPECTED_COST=15-80bps
DATA_REQUIRED=Synchronized trades/books on two venues
MAIN_FAILURE_MODE=Latency and adverse selection erase the observed lead
CONFIDENCE=LOW_UNTIL_SYNCHRONIZED_SAMPLE

## 7. Post-listing price discovery

NAME=POST_LISTING_DISCOVERY
WHO_CREATES_EDGE=Initial inventory holders, market makers, and delayed retail access
WHY_EDGE_EXISTS=Price and liquidity equilibrate over hours to days
WHY_IT_PERSISTS=Each listing is unique and historical universes are hard to reconstruct
EXPECTED_RAW_MOVE_BPS=150-1500
EXPECTED_CAPTURE_BPS=50-400
EXPECTED_EVENTS_DAY=0.05-1
EXPECTED_HOLD=15m-7d
EXPECTED_COST=40-200bps
DATA_REQUIRED=Point-in-time listing universe, timestamps, OHLCV, book history
MAIN_FAILURE_MODE=Survivorship bias and untradeable initial liquidity
CONFIDENCE=LOW_DATA

## Pilot order

1. Attention/flow continuation and its matched exhaustion reversal.
2. Volatility expansion continuation versus reversal.
3. Funding/OI crowding once historical coverage is verified.
4. Liquidation cascades only after real-event delivery is proven.
5. Cross-venue lead/lag after synchronized sample accumulation.
6. Listing effects only with point-in-time listing history.

Selection criterion: robust after-cost expectancy, `MOVE_TO_COST > 1.5`,
independent-symbol/week contribution, and plausible execution at $10-$1,000.
