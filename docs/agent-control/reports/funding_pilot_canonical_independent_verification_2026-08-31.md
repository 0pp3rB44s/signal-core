# Funding pilot canonical integration — independent verification

## Scope

Independent verification of builder commit
`c8c81d3823113ce25f2a76172adc89334e0de479` against the owner's final blocker
requirements. No builder file, production configuration, runtime state, or
exchange state was modified.

## Required verdict

`CANONICAL_PATH_VERIFIED=NO`

`STOP_ONLY_MODE_VERIFIED=YES`

`TIME_EXIT_VERIFIED=NO`

`NATIVE_STOP_VERIFIED=NO`

`RESTART_RECOVERY_VERIFIED=NO`

`KILL_SWITCH_VERIFIED=NO`

`DYNAMIC_COMPOUNDING_VERIFIED=NO`

`ADAPTIVETREND_REGRESSION_PASS=YES`

`FROZEN_SPEC_UNCHANGED=YES`

`REAL_ORDERS_SENT=0`

`READY_FOR_FINAL_OWNER_LAUNCH_DECISION=NO`

`FINAL_STATUS=IMPLEMENTATION_BLOCKED`

## Findings

### Canonical path — DEFECT / PIPELINE INCOMPLETE

The change adds stop-only branches to the real `RiskManager`,
`ExecutionService`, and `PositionManager`, and `ExecutionService.execute()`
does invoke the new risk admission gate. This is meaningful canonical entry
wiring. It is not the required end-to-end pilot adapter:

- No production signal/plan builder populates the new pilot fields. Repository
  call-sites populate them only in tests.
- Pilot authorization, NAV, available margin, gross notional, open-position
  count, kill-switch state, and native-stop availability arrive as mutable
  `TradePlan` fields supplied by the caller. The canonical stack does not
  derive or reconcile them from the isolated ledger and exchange truth before
  admission.
- There is no integrated test driving one lifecycle through RiskManager,
  ExecutionService, and PositionManager. The tests exercise entry and exit as
  separate mocked paths.

Therefore the complete canonical production pipeline is not proven.

### STOP_ONLY_TIME_EXIT — PROVEN, additive code semantics

The explicit mode permits the exact authorized strategy/hash to omit take
profit, while the default remains `STOP_AND_TP`. The entry-path test proves the
pilot selects `move_futures_stop_loss()` and does not call the existing SL/TP
placement method. No frozen take-profit was introduced.

### Native stop — INSUFFICIENT PRODUCTION EVIDENCE

ExecutionService uses the established Bitget-client native-stop method and
retains the existing fail-safe flatten path when protection is unverified.
However, acknowledgement and verification are supplied by a `MagicMock` in the
new test. No sandbox or exchange-backed native-stop lifecycle was verified, so
`NATIVE_STOP_VERIFIED` cannot be YES under the owner's strict requirement.

### Time exit and zero residual — DEFECT

`PositionManager.process_stop_only_time_exits()` calls the canonical close and
cancels remaining stops, but it treats a returned `status == CLOSED` as
sufficient. It does not query exchange positions before/after closure and does
not verify zero residual position exposure, despite its docstring and the
owner requirement. Its post-check covers residual stops only. The test uses a
mocked close response and mocked empty stop list.

### Restart recovery — DEFECT

The restart test loads a preconstructed local JSON row from a mock store. It
does not reconstruct the lifecycle from exchange positions, stop orders,
fills, entry timestamp, or exchange truth. No missing/stale local-state
recovery is implemented or tested for this mode.

### Kill switch — DEFECT / CAPITAL RISK

The new RiskManager gate blocks an entry when a caller says the switch is
latched. It does not compute 5% drawdown from authoritative pilot NAV/high-water
mark, latch durable state, cancel pilot orders, flatten pilot positions, or
verify zero residual exposure. The earlier isolated `funding_pilot` helper has
such behavior only against its abstract/fake exchange and is not wired into
the canonical stack.

### Dynamic compounding — DEFECT / CAPITAL RISK

RiskManager correctly evaluates 10%/20% mathematical caps for a supplied NAV,
but the canonical path trusts `plan.pilot_nav`, `pilot_current_gross_notional`,
and `pilot_current_position_count`. There is no production owner deriving
current pilot NAV from starting equity 27.44 USDT plus actual realized PnL,
fees, funding, and unrealized PnL. Dynamic compounding from exchange/ledger
truth is therefore not verified.

### AdaptiveTrend regression — PROVEN within available automated coverage

The mode is opt-in and identity/hash bound. Non-pilot plans retain the existing
TP requirement, and `PositionManager` ignores AdaptiveTrend rows. Existing
TP/SL, break-even, position-model/recovery, entry-path, close-economics, and
multi-symbol tests selected for this audit passed.

### Frozen specification and real-order safety — PROVEN

`research/validation/FROZEN_SPECS.json` hashes to
`cda7ecb21e6fd089ef98abb8047d409285825a37d6a2e87510d73cf653ea2e13`.
The builder diff does not alter that file. This audit used only mocked/no-order
tests and sent no real orders.

## Tests and checks

- `python3 -m pytest -q tests/test_funding_pilot_canonical.py tests/test_entry_path_audit.py tests/test_funding_pilot.py tests/test_position_lifecycle_safety.py tests/test_position_model_migration.py tests/test_hostile_b3_startup_recovery_gate.py tests/test_position_manager_close_economics.py tests/test_multisymbol_isolation.py` — 31 passed.
- SHA-256 verification of the frozen specification — matched.
- Production call-site search for pilot identity, mode, NAV, kill switch, and
  PositionManager time-exit ownership — completed.
- Builder diff and commit-scope inspection — completed.

## Risk impact

No production service, AdaptiveTrend behavior, credentials, exchange position,
order, leverage, or capital was touched. Real orders sent: zero. This verifier
added only this report.

## Remaining concerns

Before another independent release review, implement an authoritative pilot
state adapter that feeds canonical admission from ledger plus exchange truth;
wire the durable 5% kill/flatten lifecycle; verify time-exit position flatness
as well as stop absence; reconstruct restart state from exchange truth; and add
one full canonical-stack dry run covering entry through exit, restart, kill,
compounding, and telemetry. Sandbox/exchange-backed native-stop evidence is
also still required by the owner's stated verification standard.
