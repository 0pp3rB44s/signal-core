# Funding pilot `b4317e0` — independent verification

## Scope

Strict independent audit of builder commit
`b4317e01c9e06c2c9338072e9712df4becf318ce`, focused on the three previously
open deterministic-core blockers. No builder file, credential file, production
runtime, or exchange state was modified.

## Required verdicts

`FROZEN_POLLER_PARITY=FAIL_RESEARCH_AND_PRODUCTION_DIFFER`

`UNIVERSE_PARITY=FAIL`

`TURNOVER_PARITY=PASS_TICKER_USDTVOLUME_SOURCE`

`RANKING_PARITY=UNPROVEN_OUTSIDE_ONE_SYNTHETIC_FIXTURE`

`SYMBOL_PARITY=UNPROVEN_OUTSIDE_ONE_SYNTHETIC_FIXTURE`

`SIDE_PARITY=PASS_FOR_TESTED_FIXTURE`

`TIMESTAMP_PARITY=PASS_FOR_TESTED_FIXTURE`

`STALE_EVENT_PARITY=PASS_AT_300001_MS_BOUNDARY_IN_TESTED_FIXTURE`

`SIGNAL_POLLER_WIRED=NO_FROZEN_PARITY`

`ORDER_LIFECYCLE_IDENTITY_VERIFIED=YES_FOR_TESTED_OID_TRANSITIONS`

`NO_ZOMBIE_LIFECYCLES=NO_UNCERTAIN_NONZERO_RECOVERY_OWNER_ABSENT`

`NO_PREMATURE_OWNERSHIP_REMOVAL=YES`

`POST_EXECUTION_EXCEPTION_COVERAGE=INCOMPLETE`

`UNCERTAIN_ZERO_PROOF_SAFE_CLOSED=YES_MOCKED_TRANSPORT`

`PARTIAL_CLOSE_IDENTITY_VERIFIED=YES_MOCKED_TRANSPORT`

`DELAYED_CLOSE_ECONOMICS_VERIFIED=YES_FOR_TESTED_RESTART_AND_PARTIAL_CHECKPOINT`

`FINALIZED_EXIT_IDEMPOTENT=YES_FOR_TESTED_FIXTURE`

`ECONOMICS_TRANSACTION_ATOMIC=YES`

`FEE_DOUBLE_COUNT_PREVENTED=YES_FOR_TESTED_SOURCES`

`FUNDING_DOUBLE_COUNT_PREVENTED=YES_FOR_TESTED_SOURCES`

`ECONOMICS_REPLAY_IDEMPOTENT=YES_FOR_TESTED_FRESH_SCHEMA`

`CURRENT_PILOT_NAV_AUTHORITATIVE=NO_FULL_PIPELINE_NOT_PROVEN`

`DYNAMIC_COMPOUNDING_CORE_VERIFIED=YES_ARITHMETIC_ONLY`

`OWNERSHIP_FALLBACK_REMOVED=YES_FOR_EXACT_POSITION_AND_HISTORY_MATCHING`

`SHARED_POSITION_CONFLICT_GUARD_VERIFIED=YES_MOCKED_TRANSPORT`

`PILOT_STOP_ID_OWNERSHIP_VERIFIED=YES`

`ADAPTIVETREND_REGRESSION_PASS=YES_SELECTED_TESTS`

`FROZEN_SPEC_UNCHANGED=YES`

`BITGET_AUTH_READ_VERIFIED=NO_EXPECTED_EXTERNAL_RUNTIME`

`READY_FOR_DEPLOYED_RUNTIME_VERIFICATION=NO`

`BLOCKERS=PRODUCTION_UNIVERSE_USES_DIFFERENT_VOLATILITY_AND_SPREAD_DEFINITION_THAN_RESEARCH;PARITY_REFERENCE_IS_TEST_LOCAL_NOT_ORIGINAL_RESEARCH_CODE;UNCERTAIN_LIVE_EXPOSURE_HAS_NO_CANONICAL_RECOVERY_FLATTEN_OWNER;POST_EXECUTION_GUARD_NOT_GLOBAL;AUTHORITATIVE_NAV_NOT_PROVEN_END_TO_END`

`REAL_ORDER_ARMED=FALSE`

`REAL_ORDERS_SENT=0`

`FINAL_STATUS=IMPLEMENTATION_BLOCKED`

## Findings

### Full-pipeline parity — definitive mismatch

The new test now has a separately written pandas function and explicitly tests
the `> 300000 ms` stale boundary. It is materially stronger than the prior
production-only constants test.

It is not a replay of the actual research implementation. The test-local
`research_full_pipeline()` was written to mirror current production and differs
from the repository's research universe code:

- Actual `research/speculative_lab/run_hv_pilot.py::symbol_features()` computes
  volatility from log returns, uses seven days (`24 * 7`) and pandas sample
  standard deviation, then annualizes by `sqrt(24)`.
- Production uses simple percentage returns, only the trailing 72 returns, and
  `statistics.pstdev`.
- The test-local “research” function also uses simple percentage returns and 72
  observations, so it reproduces production rather than the original research.
- Actual research spread uses ticker `bidPr`/`askPr`. Production and the test
  use the current order book's best bid/ask.

These differences can change cross-sectional volatility/spread percentiles,
HV_A universe membership, downstream eligible events, ranking, and selected
symbol. The one synthetic fixture was not adversarial near percentile
thresholds, so it masks the drift.

Ticker `usdtVolume`, selected side/timestamp, and the explicit 300001 ms stale
boundary match within the fixture. They do not make overall parity 100%.

The parity reference is also test-local rather than a shared canonical function
used by research validation and production, contrary to the mandate's preferred
anti-drift architecture.

### Uncertain lifecycle reconciliation

`HALTED_UNCERTAIN` now retains ownership. `reconcile_uncertain_lifecycles()`
queries adapter truth and appends exact-OID SAFE_CLOSED only when pilot
positions, working orders, and stops are all absent. It runs during restart and
before signals, and the zero case is restart-safe and idempotent in the fixture.

If exposure remains, the method merely leaves the lifecycle HALTED_UNCERTAIN.
That is safer than premature removal, but there is no canonical recovery owner
that verifies protection or safely cancels/flatttens the exact owned exposure
and advances it to a resolved state. It can remain indefinitely stuck. Under
the mandate's `NO_ZOMBIE_LIFECYCLES` requirement, this remains NO.

The post-execution handling is broader, including ambiguous exceptions from
ExecutionService itself. It is still not a single global exception boundary:
failure while writing/latching HALTED_UNCERTAIN, evaluating a malformed report,
or writing SAFE_CLOSED/CANONICAL_OPEN can escape or recursively fail. More
importantly, the uncertain state has preservation but not complete recovery.

### Partial and delayed close economics

The partial A/B fixture proves distinct close IDs produce distinct realized-PnL
and closing-fee sources exactly once.

The delayed-close logic now stores the maximum economics event ID at exit and
requires a same-position realized-PnL event with a later event ID. This prevents
an earlier partial A event from finalizing the later time exit. The restart
fixture intentionally uses a history ID different from the close response and
proves one eventual exact-OID CANONICAL_TIME_EXIT plus idempotence on another
cycle. These three previously identified local defects are addressed in the
tested model.

The checkpoint remains an event-order heuristic rather than an explicit final-
close source relationship. Any unrelated post-checkpoint realized event for the
same exchange position could satisfy it. Authenticated Bitget identity and
partial/final history behavior remain external. This is acceptable as local
fixture proof but not enough for authoritative deployed NAV.

### NAV and compounding

Atomic source insertion, fee/funding deduplication, partial A/B replay, and the
delayed checkpoint tests pass. The arithmetic test correctly derives NAV 29.04
and 10%/20% caps from manually inserted economic truth.

The arithmetic is verified, but end-to-end NAV authority is not: production
signal parity fails, uncertain nonzero exposure has no final recovery owner,
and authenticated exchange economic identities remain unverified. Therefore
`CURRENT_PILOT_NAV_AUTHORITATIVE=NO`, while the narrow compounding formula is
YES only at core arithmetic level.

### Preserved safety

Exact position/history ownership, foreign same-symbol conflict guards, exact
pilot-stop cancellation, atomic economics, selected AdaptiveTrend regressions,
and hard disarm do not regress. The frozen specification hashes to
`cda7ecb21e6fd089ef98abb8047d409285825a37d6a2e87510d73cf653ea2e13`.
No authenticated read or real order occurred.

## Tests and checks run

- Selected parity, pilot, real-adapter, canonical lifecycle, uncertain-state,
  partial-close, delayed-close, ownership, economics, AdaptiveTrend, TP/SL/BE,
  recovery, portfolio, monitor, and runner suites — 63 passed.
- Production modules compiled successfully.
- Builder-range `git diff --check` passed.
- Frozen-spec SHA-256 matched.
- Actual research universe implementation was compared directly with production
  and the test-local parity function.

## Risk impact

No real order or exchange mutation was attempted. The runtime remained
hard-disarmed. No credentials, `.env` files, production process, position,
order, stop, leverage, capital, or AdaptiveTrend state was touched. This
verifier added only this report.

## Remaining concerns

Extract the actual research universe/event selection into one shared canonical
function and use it in production. Add threshold-adversarial recorded fixtures.
Give HALTED_UNCERTAIN a canonical exact-ownership recovery workflow that reaches
SAFE_CLOSED only after protection/flatten and zero proof. Then rerun complete
NAV/compounding verification before the external authenticated Runner gate.
