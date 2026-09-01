# Funding pilot final core fixes — independent verification

## Scope

Strict independent verification of builder commit
`c8d06e5b93c39853038930e15816a7070e730ea9`. No builder file, credential
file, production runtime, or exchange state was modified.

## Required verdicts

`FROZEN_POLLER_PARITY=FAIL_NOT_RESEARCH_VS_PRODUCTION`

`SIGNAL_POLLER_WIRED=NO_FROZEN_PARITY`

`ORDER_LIFECYCLE_IDENTITY_VERIFIED=NO`

`NO_ZOMBIE_LIFECYCLES=NO`

`NO_PREMATURE_OWNERSHIP_REMOVAL=YES_FOR_HALTED_UNCERTAIN_STATE`

`POST_EXECUTION_EXCEPTION_COVERAGE=INCOMPLETE`

`PARTIAL_CLOSE_IDENTITY_VERIFIED=YES_IN_ISOLATED_FIXTURE`

`DELAYED_CLOSE_ECONOMICS_VERIFIED=NO_PARTIAL_CLOSE_INTERACTION_DEFECT`

`FINALIZED_EXIT_IDEMPOTENT=YES_IN_FIXTURE`

`ECONOMICS_TRANSACTION_ATOMIC=YES`

`FEE_DOUBLE_COUNT_PREVENTED=YES_FOR_TESTED_SOURCES`

`FUNDING_DOUBLE_COUNT_PREVENTED=YES_FOR_TESTED_SOURCES`

`ECONOMICS_REPLAY_IDEMPOTENT=YES_FOR_TESTED_FRESH_SCHEMA`

`CURRENT_PILOT_NAV_AUTHORITATIVE=NO`

`DYNAMIC_COMPOUNDING_CORE_VERIFIED=NO`

`OWNERSHIP_FALLBACK_REMOVED=YES_FOR_EXACT_POSITION_AND_HISTORY_MATCHING`

`SHARED_POSITION_CONFLICT_GUARD_VERIFIED=YES_MOCKED_TRANSPORT`

`PILOT_STOP_ID_OWNERSHIP_VERIFIED=YES`

`ADAPTIVETREND_REGRESSION_PASS=YES_SELECTED_TESTS`

`FROZEN_SPEC_UNCHANGED=YES`

`BITGET_AUTH_READ_VERIFIED=NO_EXPECTED_EXTERNAL_RUNTIME`

`READY_FOR_DEPLOYED_RUNTIME_VERIFICATION=NO`

`BLOCKERS=GOLDEN_TEST_IS_PRODUCTION_ONLY_NOT_RESEARCH_COMPARISON;UNCERTAIN_LIFECYCLES_HAVE_NO_ZERO_PROOF_FINALIZER;POST_EXECUTION_GUARD_NOT_GLOBAL;PRIOR_PARTIAL_PNL_PREMATURELY_FINALIZES_FINAL_EXIT;CURRENT_NAV_CAN_OMIT_FINAL_CLOSE_ECONOMICS`

`REAL_ORDER_ARMED=FALSE`

`REAL_ORDERS_SENT=0`

`FINAL_STATUS=IMPLEMENTATION_BLOCKED`

## Findings

### “Golden parity” is production-only, not parity

The new recorded-shape test is more complete than the prior pure-formula tests:
it drives the production poller and asserts its audit output for universe,
ticker turnover, eligibility, ranking, selected symbol/side/timestamp, and a
durable seen-event decision.

It does not run the research implementation or a shared canonical full-pipeline
function. It constructs synthetic market data, executes only
`FrozenFundingCrowdingSignalPoller`, and compares `last_audit` against manually
written constants. Thus it proves deterministic production output for one
fixture, not research-versus-production equality. Research could disagree while
the test remains green.

It also labels the second invocation “stale” even though rejection occurs
because the ledger marks the signal `SEEN`; it does not test the five-minute
wall-clock stale-event boundary. No adversarial snapshot compares ties around
universe thresholds or simultaneous ranking between two independently computed
research candidates.

The mandate explicitly requires research-versus-production full recorded-
snapshot parity. `FROZEN_POLLER_PARITY=100_PERCENT` is not proven.

### Post-execution ownership is retained, but recovery is incomplete

`HALTED_UNCERTAIN` no longer closes ownership, and the pilot status latches
HALTED. This fixes premature local ownership removal for the newly wrapped
failure paths. Exact SAFE_CLOSED removes ownership only in the tested explicit
case.

There is no canonical reconciler that advances a HALTED_UNCERTAIN lifecycle to
SAFE_CLOSED after fresh truth proves position, orders, and stops are all zero.
Such lifecycles remain active indefinitely and block later same-symbol work.
They are no longer invisible zombies, but they are still unrecoverable zombie
state.

The post-execution wrapper is not global. Examples outside it include failure
while appending the HALTED_UNCERTAIN event or status latch, exceptions while
evaluating the execution report before `terminal_failed` is defined, and the
SAFE_CLOSED terminal append itself. More importantly, post-execution
reconciliation exceptions mark HALTED_UNCERTAIN but do not attempt a safely
owned reconciliation/flatten when identity is available. Complete exception
coverage plus recovery ownership is not established.

The requested restart-during-every-intermediate-state transition suite was not
added. The implementation does not model the full required state graph.

### Partial-close identities — isolated fixture passes

The new A/B test provides two history rows for one exchange position with
distinct close-order IDs. Both realized-PnL and closing-fee source IDs are
persisted and counted exactly once across repeated truth calls. This is valid
local evidence for partial-close source separation, subject to authenticated
Bitget schema verification.

### Delayed final close is incorrect after an earlier partial close

The restart test proves a simple delayed close with no prior realized event can
remain EXIT_PENDING, ingest later history by exchange position ID, finalize
once, and avoid duplicate CANONICAL_TIME_EXIT events.

The production finalization condition is too broad: it treats the exit as
economically reconciled when any ECONOMICS row for the position ID has an item
ID beginning `realized_pnl:`. If partial close A occurred before the final time
exit, its existing `realized_pnl:partial-A` satisfies that condition
immediately. The final lifecycle then closes before the final close event and
closing fee arrive. Once CLOSED, `_owned()` drops it and later exchange history
cannot be attributed.

The new partial-close and delayed-close tests run separately and miss this
interaction. Delayed economics and authoritative final NAV therefore remain NO.

### Economics, NAV, and compounding

Atomic event/source insertion remains correct. The tested fee/funding overlap
and fresh-schema restart replay remain idempotent. The normal arithmetic fixture
correctly computes 27.44 + 2.00 - 0.20 - 0.10 - 0.10 = 29.04 and derives the
10%/20% caps.

That fixture manually inserts already-correct economics and cannot prove their
authoritative production completeness. Because final-close economics can be
omitted after a partial close, current/final NAV, the 5% kill threshold, and
dynamic compounding are not authoritative across supported lifecycles.

### Ownership, AdaptiveTrend, frozen hash, and hard disarm

Exact position/history identity, foreign same-symbol conflict guards, and exact
pilot-stop cancellation do not regress in selected mocked-transport tests.
Selected AdaptiveTrend, TP/SL/BE, recovery, portfolio, runner, and monitor tests
pass. The builder reports ten failures in its full repository run; although
described as pre-existing, that full run is not globally green.

The frozen specification still hashes to
`cda7ecb21e6fd089ef98abb8047d409285825a37d6a2e87510d73cf653ea2e13`.
Production remains hardcoded disarmed, and this audit sent no orders.

### External runtime handoff

Authenticated reads remain unavailable here. The existing commands remain:

`python3 -m funding_pilot.read_only_verify`

`python3 -m pytest -q tests/test_funding_pilot_real_runtime.py tests/test_funding_signal_parity.py`

The second command is mocked local verification, not authenticated adapter
classification. Run only on the authorized Runner with secure credential
injection and do not expose secrets.

## Tests and checks run

- Selected full-poller, pilot, real-adapter, canonical lifecycle, partial-close,
  delayed-close, ownership, economics, AdaptiveTrend, TP/SL/BE, recovery,
  portfolio, monitor, and runner suites — 61 passed.
- Production module `py_compile` — passed.
- Builder-range `git diff --check` — passed.
- Frozen-spec SHA-256 verification — matched.
- Independent research-versus-production parity, uncertain-state recovery,
  post-execution exception, partial-plus-final close, NAV, and compounding code
  inspection — completed.

## Risk impact

No real order or exchange mutation was attempted. The runtime remained
hard-disarmed. No credentials, `.env` files, production process, position,
order, stop, leverage, capital, or AdaptiveTrend state was touched. This
verifier added only this report.

## Remaining concerns

Build one shared canonical full-selection function used by both research and
production, then compare its output against independently frozen recorded
research results. Add a zero-proof reconciler for every HALTED_UNCERTAIN state
and wrap the entire post-execution phase. Key delayed finalization to the exact
final-close economic identity/checkpoint, not any prior realized event for the
position. Add the combined partial-A/B plus delayed-final-close/restart test
before declaring NAV and compounding authoritative.
