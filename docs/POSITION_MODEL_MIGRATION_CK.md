# Position-model migration — C–K implementation report

Status: implemented and tested in `codex/position-model-migration`. This
tranche does not deploy, restart LIVE, edit `.env.live`, change trading risk,
or restore multi-symbol operation.

## C. Final position-field contract

| Field | Meaning | Authority |
|---|---|---|
| `planned_avg_entry` | Ladder average before execution | Planning only |
| `avg_entry` | Compatibility alias of `planned_avg_entry` | Legacy planning only |
| `exchange_avg_entry` | Bitget-confirmed average fill | Required execution truth |
| `actual_entry` | Fill telemetry retained for old reports | Never authoritative |
| `exchange_avg_entry_source` | Provenance such as open position or order detail | Required with executed entry |
| `exchange_avg_entry_confirmed_at` | UTC confirmation time | Execution provenance |
| `exchange_entry_order_id` / `exchange_entry_client_oid` | Entry identity | Lifecycle validation |
| `position_lifecycle_id` | Stable, non-secret lifecycle identity | Prevents cross-trade reuse |
| `confirmed_fill_quantity` | Confirmed entry fill | Quantity fallback |
| `confirmed_position_size` | Confirmed lifecycle quantity | Quantity fallback |
| `confirmed_remaining_size` | Latest persisted exchange remainder | Preferred persisted quantity |
| `confirmed_opening_fee_usdt` | Confirmed opening fee | Monetary BE input |
| `confirmed_stop` | Last exchange-verified stop | Protection truth |
| `protection_state` | Monotonic exchange-confirmed lifecycle state | Protection truth |

Legacy restoration copies only `avg_entry` to `planned_avg_entry`. It never
copies `actual_entry`, `expected_entry`, a mark, or a planned price into
`exchange_avg_entry`. Critical management waits for a validated Bitget
read-through and retains the existing confirmed stop when that is unavailable.

## D. Consumer classification and migration

Classification:

- A — planned price: pre-fill ladder construction, notional-to-order sizing,
  maker anchor, plan diagnostics, and the compatibility alias.
- B — executed exchange price: BE+fees, near-TP protection, profit-lock,
  failed-continuation protection, live price return, margin ROI, estimated net
  return, cooldown inputs, reconciliation, restart recovery, close reporting,
  and dashboard live economics.
- C — another named value: current exchange mark for legality; exchange
  remaining quantity for coverage; confirmed stop for monotonicity; TP/SL
  geometry for re-anchoring; `actual_entry` only for slippage telemetry.

Migrated critical consumers:

- execution construction and post-fill provenance;
- restart and missing-state reconciliation;
- protection repair and replacement sizing;
- BE, near-TP, profit-lock, TP1/TP2, and failed-continuation paths;
- price-return, margin-ROI, estimated-fee, and net-return calculations;
- dead-trade and cooldown decisions with explicitly named metrics;
- closed-trade reconciliation and reporting;
- dashboard position economics, risk coverage, and protection display;
- execution CSV, position updates, journal, and v1/v2 trade datasets.

No trailing algorithm currently exists in the repository. The state machine
reserves typed `TRAILING_PENDING` and `TRAILING_CONFIRMED` states so a future
trailing implementation must use the same executed-entry and verification
contract.

## E. Remaining legacy reads

No critical production consumer reads raw `avg_entry` or `actual_entry`.
Remaining compatibility surfaces are:

- `migrate_planned_entry`, which reads legacy `avg_entry` solely to populate
  `planned_avg_entry`;
- reconciliation writes `avg_entry` only as the planned alias;
- `ExecutionReport.avg_entry`, old CSV columns, and `actual_entry` remain
  labeled compatibility/telemetry fields;
- `PositionUpdate.unrealized_pnl_pct`, persisted `realized_pnl_pct`, and the
  optional `append_closed_trade_row(pnl_pct=...)` input remain compatibility
  boundaries; new internal calls use `price_return_pct`, `margin_roi_pct`, or
  `estimated_net_return_pct`;
- backtesting models retain their own historical `pnl_pct` vocabulary and do
  not drive live protection.

`legacy_avg_entry()` emits a rate-limited diagnostic with module, function,
symbol, lifecycle, exchange-entry presence, and call location. Development
assertions can make a critical legacy/planned read fatal, but are suppressed
for LIVE and disabled by default.

## F. Decimal BE+fees model

The itemised model uses:

```text
required recovery =
  actual-or-fallback opening fee
  + expected closing taker fee on expected exit notional
  + spread allowance on expected exit notional
  + slippage allowance on expected exit notional
  + extra safety allowance on expected exit notional
```

Because exit-dependent costs depend on the target itself, the implementation
solves the LONG/SHORT equation algebraically, then rounds LONG upward and
SHORT downward to the exchange tick. The resulting expected net at the target
is checked as non-negative. This is a configured execution-cost assumption,
not protection against gaps or extreme slippage.

Opening-fee precedence is `EXCHANGE_ACTUAL`, persisted confirmed execution
fee, `EXCHANGE_RATE`, `CONFIGURED_FALLBACK`, then mutually exclusive
`LEGACY_FALLBACK`. The legacy percentage is never combined with itemised
costs. Expected stop execution uses a conservative 6 bps taker-rate default.

## G. Legality, retry repair, and state machine

Every initial, repair, and replacement stop is checked against a fresh mark
and tick safety distance. Illegal targets are not submitted, weakened, or
labeled BE. They record `BE_PLUS_FEES_NOT_LEGAL` or `BE_WINDOW_MISSED`, retain
the confirmed stop, persist the retry intent, and reassess in a later cycle.

Each replacement attempt refreshes the open position, mark, side, remaining
size, executed entry, active stop, metadata, and lifecycle, then recalculates
the target. A rejected absolute trigger is not reused.

The old stop remains active during placement. Local stop state advances only
after the new stop is visible through exchange verification; known old stop
IDs are cancelled afterward. Cleanup failure truthfully leaves
`old_stop_loss_removed=false` and may leave both protective stops active.

States:

```text
INITIAL_PROTECTION_CONFIRMED
BE_PLUS_FEES_PENDING -> BE_PLUS_FEES_CONFIRMED
PROFIT_LOCK_PENDING  -> PROFIT_LOCK_CONFIRMED
TRAILING_PENDING     -> TRAILING_CONFIRMED
any pending failure  -> PROTECTION_UPDATE_FAILED
```

LONG confirmed stops cannot decrease; SHORT confirmed stops cannot increase.
Unverified replacement payloads never advance state.

## H. Dashboard and reporting

The dashboard and reporting surfaces expose planned entry, exchange entry,
entry delta, provenance, current mark, gross unrealized PnL, estimated fees,
estimated net unrealized PnL, explicit return metrics, confirmed protection
state, confirmed stop, and the calculated BE+fees level.

Dashboard live PnL uses Bitget `openPriceAvg` and live size. If the live API is
unavailable, the fallback view does not manufacture PnL from planned entry.
BE is displayed as active only for a confirmed BE/profit/trailing state.

## I. Restart recovery

State restoration preserves provenance and protection state, then reconciles
with Bitget before modifying protection. Symbol-only execution-log protection
fallback is forbidden because it may belong to a previous lifecycle. A local
fallback is accepted only when its persisted lifecycle ID matches exactly.

Conflicting entry/lifecycle/order/client truth fails closed and emits a
critical event. A missing local position is reconstructed from the current
exchange position with a new lifecycle identity. Missing or unverified
protection proceeds through the verified repair path or the existing
fail-safe close.

## J. Forensic replay

The sanitized deterministic fixture combines the actual Bitget position
history/state fields with public Bitget one-minute futures candles.

| Trade | Planned / exchange entry | Old request | Corrected BE+fees | Legality at replay decision | Later market reachability | Expected net at target |
|---|---:|---:|---:|---|---|---:|
| 2 · ATOM LONG | 1.59255 / 1.5959 | 1.5951 | 1.5988 | Legal at 2026-07-11 16:49Z, mark 1.5999 | Low 1.5982 at 16:54Z crossed target | +0.00028680 USDT |
| 3 · FIL LONG | 0.79315 / 0.7927 | 0.79441904 | 0.7942 | Old stale target illegal; recalculated target legal at 17:34Z, mark 0.7945 | Low 0.7939 at 17:36Z crossed target | +0.001847706 USDT |
| 4 · TRX SHORT | 0.32921 / 0.3286 | 0.328683264 | 0.32800 | Correct target illegal at 08:25Z, mark 0.32876; repaired logic submits nothing | Later minimum 0.32855 never reached the target | +0.000443520 USDT if target were fillable |

This establishes formula correctness, legality, and candle-level market
reachability separately. Candle crossings do not prove that a stop was live
or filled at its trigger; no hypothetical fill is claimed.

Test evidence: 39 dedicated position-model migration/replay tests, 24 focused
position-lifecycle tests, and the complete repository suite all pass. The
final full-suite result is `427 passed`.

## K. Configuration, deployment, and rollback

No environment file was edited. Proposed operator-reviewed `.env.live` diff:

```diff
+BREAK_EVEN_EXPECTED_CLOSE_FEE_RATE=0.0006
+BREAK_EVEN_SPREAD_BUFFER_PCT=0.02
+BREAK_EVEN_SLIPPAGE_BUFFER_PCT=0.03
+BREAK_EVEN_EXTRA_BUFFER_PCT=0.01
+BREAK_EVEN_MARK_SAFETY_TICKS=2
```

The code defaults already match these values. `BREAK_EVEN_FEE_BUFFER_PCT`
remains a legacy-only fallback.

Deployment risks requiring operator review:

- Bitget precision/contract metadata must be available or stop updates fail
  closed;
- exchange plan-order visibility can lag, so replacement verification may
  retain both stops temporarily;
- legacy open state without verifiable exchange provenance will defer
  protection calculations and may require operator attention;
- schema-aware CSV rotation will start new files when old reporting headers do
  not contain the explicit position fields;
- configured spread/slippage allowances are assumptions, not fill guarantees.

Multi-symbol restoration remains explicitly out of scope and must be handled
in a separate qualification tranche after this repair is reviewed and
approved.
