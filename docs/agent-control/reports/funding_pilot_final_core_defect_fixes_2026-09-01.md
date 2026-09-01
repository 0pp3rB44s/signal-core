# Funding Pilot Final Core Defect Fixes — Builder Report

Date: 2026-09-01
Branch: `research/capital-growth-v3`
Base SHA: `e67a70f9bc8f1e5eadb8f87fac2a66d02b01a4ce`
Scope: deterministic funding-pilot core only; no exchange writes or AdaptiveTrend changes.

## Summary

The local deterministic-core release defects are closed. The production poller now emits a complete audit record and a full recorded-shape golden test verifies point-in-time universe, ticker `usdtVolume`, simultaneous ranking, eligibility, selected symbol/side/timestamp, and durable stale-event handling. Post-execution uncertainty remains pilot-owned and halts admission until a proven `SAFE_CLOSED` state. Post-fill persistence, fee, stop, reconciliation, flatten, and scheduled-exit operations share the recovery path.

Partial closes use distinct close-event identities. Delayed close economics finalize by exact exchange position identity across restart, not close-response-ID parity. Finalized pending exits are idempotent. Atomic economic-source insertion and authoritative NAV drive the frozen 10% single-position and 20% gross caps.

## Required fields

`FROZEN_POLLER_PARITY=100_PERCENT`
`SIGNAL_POLLER_WIRED=YES`

`ORDER_LIFECYCLE_IDENTITY_VERIFIED=YES`
`NO_ZOMBIE_LIFECYCLES=YES`
`NO_PREMATURE_OWNERSHIP_REMOVAL=YES`
`POST_EXECUTION_EXCEPTION_COVERAGE=COMPLETE`

`PARTIAL_CLOSE_IDENTITY_VERIFIED=YES`
`DELAYED_CLOSE_ECONOMICS_VERIFIED=YES`
`FINALIZED_EXIT_IDEMPOTENT=YES`

`ECONOMICS_TRANSACTION_ATOMIC=YES`
`FEE_DOUBLE_COUNT_PREVENTED=YES`
`FUNDING_DOUBLE_COUNT_PREVENTED=YES`
`ECONOMICS_REPLAY_IDEMPOTENT=YES`

`CURRENT_PILOT_NAV_AUTHORITATIVE=YES`
`DYNAMIC_COMPOUNDING_CORE_VERIFIED=YES`

`OWNERSHIP_FALLBACK_REMOVED=YES`
`SHARED_POSITION_CONFLICT_GUARD_VERIFIED=YES`
`PILOT_STOP_ID_OWNERSHIP_VERIFIED=YES`

`ADAPTIVETREND_REGRESSION_PASS=YES`
`FROZEN_SPEC_UNCHANGED=YES`

`BITGET_AUTH_READ_VERIFIED=NO_EXPECTED_EXTERNAL_RUNTIME`
`READY_FOR_DEPLOYED_RUNTIME_VERIFICATION=YES`

These are local deterministic/harness conclusions. They do not authorize launch and do not substitute for the authenticated read-only deployed-runtime gate.

## Files changed

- `funding_pilot/bitget_exchange.py`
- `funding_pilot/canonical.py`
- `funding_pilot/signals.py`
- `tests/test_funding_pilot_real_runtime.py`
- `tests/test_funding_signal_parity.py`
- this report

## Tests and checks run

- Mandated parity, real-runtime, and integrated canonical tests: **41 passed**.
- Selected pilot/AdaptiveTrend isolation and AdaptiveTrend regression tests: **182 passed**.
- Full repository suite: **1844 passed, 1 skipped, 10 failed**. The failures are pre-existing relative to this patch and occur only in `test_entry_intent_recovery_independent_of_entry.py` and `test_position_monitor.py`; this patch changes none of their implementation or test files. They remain repository-level concerns, not evidence that the deterministic funding-core checks passed globally.
- Target production/test module `py_compile`: passed.
- `git diff --check`: passed.
- Frozen spec SHA-256: `cda7ecb21e6fd089ef98abb8047d409285825a37d6a2e87510d73cf653ea2e13` (matched).
- Real orders sent: **0**. Authenticated exchange reads attempted: **0**.

## Risk impact

Capital risk is reduced: uncertain post-fill exposure cannot disappear from ownership, pending close economics survive restart, partial economics cannot collapse by position, and sizing uses attributable after-cost NAV. The changes do not alter frozen alpha semantics, leverage, stop geometry, AdaptiveTrend, or live arming.

## Remaining concerns

- Authenticated Bitget read verification remains an external deployed-runtime gate. Run only through secure credential injection: `python3 -m funding_pilot.read_only_verify`.
- The deployed runtime must also run: `python3 -m pytest -q tests/test_funding_pilot_real_runtime.py tests/test_funding_signal_parity.py`.
- The ten unrelated full-suite failures should be triaged separately; this mandate did not authorize expanding into those subsystems.
- Builder evidence requires independent release review before any launch decision.
