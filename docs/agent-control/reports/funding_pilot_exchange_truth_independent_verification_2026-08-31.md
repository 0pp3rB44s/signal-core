# Funding pilot exchange-truth hardening — independent verification

## Scope

Strict independent verification of builder commit
`ddb374a8e5f4f51a7df99dbc66ee43c2fad4f315` against the owner's final
exchange-truth hardening mandate. The builder's files were not modified.

## Required verdicts

`CANONICAL_PATH_VERIFIED=NO`

`AUTHORITATIVE_STATE_VERIFIED=NO`

`DYNAMIC_COMPOUNDING_VERIFIED=NO`

`NATIVE_STOP_VERIFIED=NO`

`TIME_EXIT_VERIFIED=NO`

`POST_EXIT_ZERO_EXPOSURE_VERIFIED=NO`

`RESTART_RECOVERY_VERIFIED=NO`

`KILL_SWITCH_VERIFIED=NO`

`STRATEGY_OWNERSHIP_ISOLATION_VERIFIED=NO`

`ADAPTIVETREND_REGRESSION_PASS=YES`

`FROZEN_SPEC_UNCHANGED=YES`

`REAL_ORDERS_SENT=0`

## Final report fields

`FINAL_BLOCKER_RESOLVED=NO`

`BUILDER_SHA=ddb374a8e5f4f51a7df99dbc66ee43c2fad4f315`

`INDEPENDENT_VERIFICATION_SHA=RECORDED_BY_THIS_REPORT_COMMIT`

`AUTHORITATIVE_LEDGER_IMPLEMENTED=YES_COMPONENT_LEVEL`

`EXCHANGE_RECONCILIATION_VERIFIED=NO_PRODUCTION_ADAPTER_ABSENT`

`PILOT_STARTING_EQUITY=27.44`

`COMPOUNDING_ENABLED=YES_COMPONENT_LEVEL_NOT_PRODUCTION_WIRED`

`MAX_SINGLE_POSITION_FORMULA=10_PERCENT_X_CURRENT_RECONCILED_PILOT_NAV`

`MAX_GROSS_EXPOSURE_FORMULA=20_PERCENT_X_CURRENT_RECONCILED_PILOT_NAV`

`ADAPTIVETREND_ISOLATION_VERIFIED=NO_PRODUCTION_OWNERSHIP_ADAPTER_ABSENT`

`FROZEN_SPEC_HASH=cda7ecb21e6fd089ef98abb8047d409285825a37d6a2e87510d73cf653ea2e13`

`READY_FOR_FINAL_OWNER_LAUNCH_DECISION=NO`

`BLOCKERS=NO_PRODUCTION_RUNTIME_OR_EXCHANGEPORT_WIRING;POST_ACK_STOP_MISMATCH_DOES_NOT_FLATTEN;PARTIAL_STATE_RESTART_NOT_RECONSTRUCTED;AUTHORITATIVE_ECONOMICS_INGESTION_ABSENT`

`FINAL_STATUS=IMPLEMENTATION_BLOCKED`

## Evidence

### Improvements proven

- **PROVEN:** caller-supplied NAV, margin, exposure, position count,
  kill-switch state, and native-stop-availability fields were removed from
  `TradePlan`.
- **PROVEN:** `ExecutionService` now fails closed when its pilot state provider
  is absent or reconciliation raises.
- **PROVEN:** the isolated SQLite ledger persists starting equity, economics
  events, high-water mark, status, reconciliation snapshots, and canonical
  open/time-exit events.
- **PROVEN:** component sizing uses the reconciled NAV and enforces the 10% and
  20% formulas dynamically.
- **PROVEN:** `PositionManager` now checks a fresh position response, stop
  absence, and pilot-client-ID working-order absence before marking the local
  time-exit row closed.
- **PROVEN:** the STOP_ONLY_TIME_EXIT branch is additive and frozen-hash bound.
  Existing AdaptiveTrend/TP/SL/BE/recovery regression tests selected for this
  audit pass.

These improvements are real but do not establish a deployable production
pipeline.

### Canonical runtime remains unwired — PIPELINE INCOMPLETE

Repository-wide call-site inspection finds `CanonicalFundingPilot`
construction only in tests. No runner, scheduler, production signal path, or
startup path constructs it. Consequently no production path installs
`funding_pilot_state_provider`, calls `recover()`, emits plans, dispatches time
exits, or connects the ledger to the live position owner.

There is also no concrete production implementation of the `ExchangePort`
contract. The integrated test's stateful `Harness` and `MagicMock` jointly
simulate two exchange clients. They exercise real class methods but do not
prove a production Bitget adapter, deterministic position/order filtering, or
one shared exchange-truth view.

### Authoritative economics are incomplete — OBSERVABILITY GAP / CAPITAL RISK

NAV uses durable `ECONOMICS` events plus exchange-provided unrealized PnL, but
no production call-site writes actual realized PnL, fees, or funding into that
ledger. The harness manually appends economics. Therefore the formulas are
correct for supplied records, but current pilot NAV and compounding are not
authoritatively produced in runtime.

### Stop post-reconciliation can leave exposure open — CAPITAL RISK

ExecutionService retains its existing immediate fail-safe flatten if initial
native-stop placement/verification fails. However,
`CanonicalFundingPilot.process_signal()` performs a second required exchange
reconciliation after persistence. If the acknowledged stop is then absent or
its identity mismatches, the code only raises `FailClosed`; it does not invoke
a canonical flatten/recovery path. An entry can therefore remain open after
the adapter detects that native stop protection is not reconciled.

All native-stop evidence in this commit is from mocked/harness responses. The
required acknowledgement/persistence/reconciliation chain is not production
verified.

### Restart does not reconstruct partial lifecycle state — DEFECT

`recover()` reloads ledger events, checks that each exchange position has a
schedule, and returns a schedule dictionary. It does not recreate a missing or
partially written `executed_trades` row for `PositionManager`, repair persisted
stop identity, reconcile canonical-open events against closed positions, or
restore scheduling into an active production dispatcher. Thus the mandated
partial-local-state recovery is not implemented end to end.

The ledger high-water mark and HALTED status do persist, which is useful
component-level evidence, but not full lifecycle reconstruction.

### Kill switch is complete only behind an abstract fake port — INSUFFICIENT

The isolated runtime computes the 5% threshold, latches HALTED, cancels
reported pilot working orders, flattens reported pilot positions, cancels
stops, and checks its next abstract truth snapshot. The test proves this using
`FakeExchange`. Since there is no production exchange adapter or startup/runtime
wiring, the canonical production stack cannot currently trigger this path.

### Ownership isolation is asserted, not derived in production — OPERATIONAL RISK

The core rejects rows presented in its pilot collections unless their client
IDs use `cgc-fcp-`. But the absent production adapter is the component that
would have to classify aggregate exchange positions and orders into those
pilot-only collections. No production reconciliation proves the association
between a Bitget position and its originating pilot entry. Therefore the
implementation cannot yet prove it excludes AdaptiveTrend PnL, reserved
margin, positions, stops, and orders from pilot accounting/mutation.

### Time exit strongest-harness result

The harness proves the new PositionManager checks can pass when mocked clients
mutate one shared in-memory state correctly. It does not prove production
call-site wiring. Because no production adapter/runtime schedules or invokes
the canonical wrapper, and no real adapter unifies the two exchange views,
TIME_EXIT and POST_EXIT_ZERO_EXPOSURE remain NO for release purposes.

## Tests and checks run

- `python3 -m pytest -q tests/test_funding_pilot.py tests/test_funding_pilot_canonical.py tests/test_entry_path_audit.py tests/test_position_lifecycle_safety.py tests/test_position_model_migration.py tests/test_hostile_b3_startup_recovery_gate.py tests/test_position_manager_close_economics.py tests/test_multisymbol_isolation.py tests/test_forward_paper_only_runtime.py` — 36 passed.
- `python3 -m pytest -q tests/test_portfolio_selection.py` — 11 passed.
- `python3 -m py_compile funding_pilot/core.py funding_pilot/canonical.py execution/execution_service.py execution/position_manager.py risk/risk_manager.py` — passed.
- Builder-range `git diff --check` — passed.
- Frozen-spec SHA-256 verification — matched.
- Repository-wide production call-site and ownership/economics wiring search —
  completed.

## Risk impact

No real order was submitted. No exchange, production service, credentials,
configuration, leverage, stop, position, capital, or AdaptiveTrend state was
touched. This verifier added only this report.

## Remaining concerns

Implement and independently verify a concrete pilot-only Bitget exchange
adapter and runner/startup/scheduler wiring; ingest actual pilot economics;
flatten on the canonical post-ack stop reconciliation failure; reconstruct
PositionManager lifecycle rows from exchange plus ledger truth on restart; and
prove ownership, kill, exit, and residual checks through that single wired
adapter. Until then the owner should not be asked for a launch decision.
