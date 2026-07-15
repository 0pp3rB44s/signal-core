# Historical risk contract

Date: 2026-07-15

## Production blocker

`market_features.engine.build_market_snapshot` records `orderbook_available=false`,
`orderbook_risk_off=true` and `risk_off_reason=orderbook_context_unavailable` when no
live depth snapshot exists. `RiskManager.evaluate` treats those markers as an
immediate fail-closed production rejection. Live depth supplies spread, bid/ask
notional, total depth, depth imbalance and a continuation bias. OHLCV history
cannot reproduce those fields.

This gate combines live availability/safety with execution economics. Missing or
stale depth is operational safety; observed spread and depth are execution-quality
inputs. Neither is an intrinsic strategy condition.

## Explicit modes

- `PRODUCTION`: unchanged, orderbook absence fails closed and operational account
  state remains active.
- `HISTORICAL_STRUCTURAL_ONLY`: removes only the three unavailable-orderbook
  markers after selection and excludes operational account/portfolio-state gates.
  Scoring plus all intrinsic strategy, direction, trend, HTF, volume, momentum,
  alignment and execution-cost checks remain active.
- `HISTORICAL_CONSERVATIVE_PROXY`: structural-only plus the frozen proxy below.

The mode is a required typed constructor/CLI value. It has no Settings or `.env`
binding and cannot silently activate in production.

## Frozen conservative proxy

Configuration hash:
`722bb6962e575931e5d4b2ee58ce175413729c587f9eed5a796b69930a349cbc`

All values come from the signal-time closed snapshot:

- `volume_ratio_20 >= 0.50`;
- signal candle `range_pct <= 5.00`;
- `volatility_rank <= 90.00`;
- TP1 reward distance must be at least `2.0 * configured_round_trip_cost_bps`.

For the frozen market-order execution contract, round-trip cost is:

`2*spread + entry_slippage + exit_slippage + entry_taker_fee + exit_taker_fee`

This is `24 bps`; minimum TP1 distance is therefore `48 bps`. TP1 remains the
existing `0.8R` execution target. The proxy uses no future/fill candle, orderbook
imbalance, invented depth, absolute candle volume, current live snapshot or PnL
calibration. Candle volume is only a relative activity proxy and does not prove
executable depth.

## Condition classification

| Condition | Source | Category | Historically available | Required for outcome | Historical treatment |
|---|---|---|---|---|---|
| Detector pattern, direction and regime | strategies/selector | Strategy validity | Yes | Yes | Unchanged |
| Score, alignment, HTF, momentum and continuation volume | scorer/risk manager | Strategy validity | Yes | Yes | Unchanged |
| Spread and entry-position execution checks | risk manager | Execution economics | Configured spread only | Yes | Existing checks plus frozen costs |
| Orderbook depth and observed spread | live orderbook analyzer | Execution economics | No | Proxy only | Conservative OHLCV proxy |
| Orderbook availability/freshness | snapshot/risk manager | Live market safety | No | No | Production fail-closed; excluded offline |
| Weekly/daily freeze, expectancy, coach, equity | risk manager/runtime reports | Operational account state | Not part of dataset | No | Excluded from isolated research |
| Open-position cluster exposure | executed-trade state | Operational account state | No | No | Excluded from isolated research |

## Limitations

The proxy estimates whether candle conditions and configured costs are compatible;
it does not reconstruct spread history, queue priority, depth, impact, imbalance or
participation capacity. Structural-only is diagnostic. Conservative-proxy is the
official Phase 2C baseline, but remains a candle-based research estimate.
