# Funding Pilot Exchange-Truth Hardening — Builder Report

## Summary

Removed caller-supplied pilot risk truth. The frozen pilot now installs an
authoritative state provider on `ExecutionService`; that provider reconciles
the isolated SQLite pilot ledger with a freshly queried, deterministically
owned exchange view before every admission. Added an integrated stateful
exchange-adapter harness covering entry, native-stop identity reconciliation,
restart schedule recovery, time exit, fresh zero-exposure proof and ledger
closure. No real order was sent.

Builder status: **READY FOR INDEPENDENT VERIFICATION**. This is not builder
self-approval.

## Files changed

- `funding_pilot/core.py`
- `funding_pilot/canonical.py`
- `clients/schemas.py`
- `execution/execution_service.py`
- `execution/position_manager.py`
- `tests/test_entry_path_audit.py`
- `tests/test_funding_pilot.py`
- `tests/test_funding_pilot_canonical.py`
- this report

## Risk impact

- Trade plans no longer contain NAV, margin, exposure, position-count,
  kill-switch or native-stop-availability inputs.
- Missing reconciliation provider, unknown economics/margin/ownership, foreign
  order identity, missing stop, orphan stop, stale schedule, residual position,
  residual pilot order or residual stop all fail closed.
- Pilot NAV compounds from starting equity plus pilot-only realized/unrealized
  PnL less fees, funding and other costs; exchange position/order state overrides
  stale local snapshots.
- Ownership requires the `cgc-fcp-` namespace. Foreign/AdaptiveTrend state is
  rejected if presented as pilot state and is never mutated by pilot cleanup.
- Existing strategies retain the original SL+TP path.

## Tests/checks run

- Production-module `py_compile`.
- Stateful integrated canonical harness: signal, reconciliation, dynamic size,
  RiskManager, ExecutionService, native stop, persistence, restart recovery,
  PositionManager time exit, zero position/order/stop proof and ledger update.
- Kill-switch cancel/flatten/verify/latch test.
- Foreign ownership rejection and AdaptiveTrend non-mutation tests.
- Existing entry, TP/SL/BE, recovery, position migration, portfolio selection
  and paper-only runtime regressions.
- `git diff --check` and frozen-spec SHA256 verification.

## Remaining concerns

- The harness exercises production classes and call-sites without contacting
  Bitget. Real exchange latency, acknowledgement and fill behavior remain
  UNKNOWN until the separately authorized capped pilot.
- Independent verification is mandatory before requesting the final owner
  launch decision.
