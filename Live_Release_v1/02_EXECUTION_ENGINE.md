# 02 — Execution Engine

> **The live order path has never been executed.** Across the entire forward-paper
> campaign: 0 private endpoints, 0 order calls, 9 892 public requests. Everything below
> describes code that is implemented and unit-tested but **never exercised against the
> exchange**. Treat it as unvalidated.

## Entry gate

`ExecutionService.execute(plans)` returns immediately when `execution_enabled` is
false. In strict forward-paper mode the service is never constructed at all
(`execution_service = None`), so the module cannot be reached.

## Layered safety gates

Order of evaluation in `execution/execution_service.py`:

| # | Gate | Setting | Behaviour |
|---|---|---|---|
| 1 | Execution enabled | `EXECUTION_ENABLED` | returns `[]` if false |
| 2 | Symbol confirmation | `EXECUTION_REQUIRE_CONFIRMATION`, `EXECUTION_CONFIRM_SYMBOLS` | plan skipped unless the symbol is on the allow-list |
| 3 | Plans per cycle | `EXECUTION_MAX_PER_CYCLE`, `EXECUTION_PLAN_LIMIT` | caps orders per scan |
| 4 | Notional cap | `EXECUTION_MAX_LIVE_NOTIONAL_PER_TRADE_USDT` | capped to the lesser of requested, hard cap and configured cap |
| 5 | Notional floor | `EXECUTION_MIN_LIVE_NOTIONAL_USDT` | blocked below the exchange minimum |
| 6 | Balance precheck | — | blocks when capped notional is below the floor |
| 7 | Open-position gate | exchange truth | rejects a duplicate symbol |
| 8 | Symbol cooldown | `SYMBOL_COOLDOWN_MINUTES` | blocks re-entry after a recent trade |

The journal (`state/live_trade_journal.json`) is **analytics only** and never blocks a
trade; the position gate reads exchange truth.

## Order lifecycle

1. Plan arrives with `verdict=EXECUTABLE`, entry, stop, targets, notional.
2. Gates above; `runtime_lock` and `trading_state_lock()` serialise execution.
3. Entry submitted (`execution/maker_entry.py` for maker placement).
4. Protective orders attached — `execution/tp_sl_lifecycle.py`.
5. Position tracked by `execution/position_manager.py`; reconciled against the exchange
   by `execution/position_reconciler.py`.
6. Close recorded by `execution/closed_trade_writer.py`.

**Mandatory stop:** every plan carries a non-zero stop before it can become executable.
In the validation run 3/3 trades had a non-zero `initial_stop`.

## Restart behaviour

| Situation | Behaviour | Evidence |
|---|---|---|
| Process restart, forward paper | open position reconstructed from the event log; restored **exactly once**; no duplicate open | verified repeatedly (91 s, 121 s detached recovery) |
| Persisted terminal intent | completed before newer candles are evaluated, so a crash cannot strand a trade | `forward_paper/service.py` pending-terminal handling |
| Duplicate open suppression | deterministic `candidate_id` from the closed-candle timestamp; store rejects a second `TRADE_OPENED` for the same candidate | observed: strategy re-emitted a plan after restart, store deduped it |
| Process restart, live | `position_reconciler` is expected to rebuild from exchange truth | **never tested** |
| Host reboot | **nothing restarts** — see [07_RECOVERY_GUIDE.md](07_RECOVERY_GUIDE.md) | 7.82 h dead after the 2026-07-27 reboot |

## Accounting model

- Entry fill: simulated at the latest close in paper; real fill in live.
- Fees: `FORWARD_PAPER_ROUNDTRIP_FEE_BPS` (12 bps default), applied half on entry and
  half on exit.
- Slippage: entry slippage is real (`simulated_fill − planned_entry`); **exit slippage
  is modelled as 0.0** in paper — exits fill exactly at stop or target.
- Net PnL = gross − fees + funding.

Verified: both closed trades in the validation reconcile independently to < 1e-6, with
exit prices inside the traded candle range (no impossible fills).

## Protective-stop guard

`_protective_stop()` returns the fee break-even stop **only while it is still on the
protective side of the mark** (below for LONG, above for SHORT). Without this guard a
break-even move could place the stop beyond the current price, firing `SL_TOUCH` on the
same candle and booking an exit at a price the market never traded — observed once
before the guard existed (stop 74.004931 against a candle high of 73.947).

## Kill path

There is no exchange-side automated kill switch. Stopping means stopping the process;
open exchange positions and resting orders are **not** cancelled by process exit. See
[09_EMERGENCY_PROCEDURES.md](09_EMERGENCY_PROCEDURES.md).
