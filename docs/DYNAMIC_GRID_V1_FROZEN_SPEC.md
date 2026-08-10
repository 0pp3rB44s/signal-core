# dynamic_grid_v1 frozen pilot specification

Status: frozen for shadow qualification. Parameter changes require a new code
revision, review, CI run, and deployment SHA; parameters must not be optimized
during a pilot.

## Scope and cutover

- Symbols: `BTCUSDT`, `SOLUSDT`.
- At most one active grid across both symbols.
- Long-only, isolated margin, exactly 1x leverage.
- Exactly three equal-notional entry levels. No martingale.
- Management timeframe: 5m. Context: 15m and 1h.
- Existing non-grid positions remain under the existing PositionManager. A grid
  never overlays any existing exchange position. In LIVE,
  `OLD_STRATEGIES_NEW_ENTRIES_ENABLED=false` is mandatory.

## Selection and geometry

The deterministic opportunity score is the sum of bounded volatility (30),
depth (25), spread (20), trend (15), and expected-excursion (10) components.
Only `GRID_ALLOWED` candidates above the configured score floor may be selected;
ties are deterministic. The reference is the mean of rolling 5m VWAP and EMA20.
Spacing is `max(0.50 * ATR14, 5 bps * center)`. Levels are reference minus one,
two, and three spacings. The reference is never moved while inventory or working
orders exist.

## Economics

The runtime queries `/api/v2/common/trade-rate` with authenticated account
credentials independently for BTCUSDT and SOLUSDT. Gross TP capture is the
larger of `0.60 * ATR bps` and the fee hurdle plus 1 bp. The hurdle is actual
maker entry + actual maker exit + configured execution drag + safety margin.
Every decision logs gross capture, each cost component, and expected net capture.

## Regimes and safety

- `GRID_ALLOWED`
- `GRID_PAUSED_TREND`
- `GRID_PAUSED_VOLATILITY`
- `GRID_PAUSED_SPREAD`

Paused regimes cancel unresolved entry orders and leave filled inventory paired
with maker exits. Hard invalidation is three ATR below the frozen reference: all
known grid orders are cancelled and the remaining long is flattened by the
emergency market-close path. Daily/weekly RED mode pauses new accumulation.
Stale data, invalid fee responses, submit ambiguity, unknown fills, order errors,
and recovery ambiguity fail closed. Deterministic client order IDs are reconciled
before every POST. Intent is persisted before the first order call.

## Shadow-to-LIVE gate

LIVE is prohibited until all of the following hold for the exact integrated SHA:

1. CI and the full local regression suite are green.
2. The Runner records the exact deployed SHA.
3. Shadow has at least 48 complete selection cycles spanning at least four hours.
4. Both symbols have authenticated fee observations and decision observations.
5. Shadow contains no `GRID_STOP`, `GRID_ORDER_ERROR`, or malformed event rows.
6. Every allowed decision has three levels and positive expected net capture.
7. At least one allowed opportunity was observed without forcing a regime.
8. The state is flat and recovery is unambiguous immediately before promotion.

Completed-cycle events carry actual gross capture, exchange-reported fees, net
capture, duration, per-level fills, maker hit rate, unfilled-level opportunity
cost, reset count, and whether emergency behavior occurred.

Run `python scripts/evaluate_dynamic_grid_shadow.py <events.jsonl>` to produce the
machine-readable gate verdict. A failing gate means no LIVE orders.
