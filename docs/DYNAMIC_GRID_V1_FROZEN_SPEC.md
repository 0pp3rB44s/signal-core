# dynamic_grid_v1.1 frozen pilot specification

Status: v1.1 pre-LIVE economic correction, frozen for shadow qualification.
Parameter changes require a new code
revision, review, CI run, and deployment SHA; parameters must not be optimized
during a pilot.

## Scope and cutover

- Symbols: `BTCUSDT`, `SOLUSDT`.
- At most one active grid across both symbols.
- Long-only, isolated margin, exactly 1x leverage.
- Exactly three equal-target-notional entry levels. Executable quantities are
  rounded down to the live contract increment, so actual notionals may differ
  slightly. No martingale.
- Total grid notional: min(30 USDT, 3% of account equity, 10 USDT per level,
  and the notional implied by a 0.25% equity loss at hard invalidation);
  minimum practical size is 5 USDT per level.
- Each level must also clear the current Bitget contract minimum. The effective
  floor is `max(strategy minimum, exchange minimum executable notional)`, where
  exchange quantity, increment and minimum-notional metadata are queried at
  runtime. USD equivalents are never hardcoded.
- Strategy drawdown stop: 0.5% of current account equity.
- Repeated-order-error stop: three recorded order-path errors.
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
volatility-supported `0.60 * ATR bps`; it is not inflated to manufacture edge.
Expected all-in cost is actual maker entry + actual maker exit + configured
execution drag + safety margin. A candidate is economically eligible only when
gross capture is at least twice that dynamic cost, equivalently expected net
capture is at least the cost. Failure emits `GRID_PAUSED_ECONOMICS`. The spacing
floor is never tighter than the same dynamic minimum-gross hurdle. Every
decision logs gross capture, each cost component, the economic hurdle, and
expected net capture.

The allocation cap remains 3% of equity. The 0.25% hard-invalidation cap uses
the exact three-level worst-case loss, summing
`quantity × max(entry − hard invalidation, 0)` across all filled levels. (For
an eligible ladder, hard invalidation is below all three entries and this is
identical to `quantity × (entry − hard invalidation)`.) If the equal
three-level ladder cannot clear the effective exchange/strategy minimum within
both caps, it emits `GRID_PAUSED_SIZE`. Leverage remains exactly 1x. One- and
two-level variants are outside v1.1 because they change the ladder and inventory
thesis.

Both reported minimum-equity thresholds use the exchange-rounded minimum
quantity separately at each frozen entry. Allocation uses the sum of the three
actual minimum notionals divided by 3%; hard risk uses the exact summed loss at
hard invalidation divided by 0.25%. These are runtime calculations, never
hardcoded USD equivalents.

## Regimes and safety

- `GRID_ALLOWED`
- `GRID_PAUSED_ECONOMICS`
- `GRID_PAUSED_SIZE`
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

SHADOW maintains a separate persisted lifecycle and emits hypothetical level
fills, mapped TPs, regime kill-switches, hard kills, resolved cycles, deferred
resets, and material flat-state resets. This state has no exchange transport.

Funding is queried from Bitget position history after exchange-confirmed
flatness and included only when the existing lifecycle matcher identifies one
unambiguous position-history row. Ambiguous funding is logged as incomplete and
is never fabricated or silently attributed.

Run `python scripts/evaluate_dynamic_grid_shadow.py <events.jsonl>` to produce the
machine-readable gate verdict. A failing gate means no LIVE orders.
