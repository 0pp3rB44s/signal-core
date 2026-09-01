# Funding pilot `5eb910d` — independent verification

## Scope

Third strict re-audit of builder commit
`5eb910db5bfb68a5524617257f2c429df94d87a4`. No builder file, credential
file, production runtime, or exchange state was modified.

## Required verdicts

`FROZEN_POLLER_PARITY=FAIL_MINIMUM_HISTORY_AND_DEPTH_BOUNDARIES`

`UNIVERSE_PARITY=FAIL`

`TURNOVER_PARITY=100_PERCENT_FOR_TESTED_FIXTURE`

`RANKING_PARITY=100_PERCENT_FOR_TESTED_MATURE_FIXTURE`

`SYMBOL_PARITY=100_PERCENT_FOR_TESTED_MATURE_FIXTURE`

`SIDE_PARITY=100_PERCENT_FOR_TESTED_FIXTURE`

`TIMESTAMP_PARITY=100_PERCENT_FOR_TESTED_FIXTURE`

`STALE_EVENT_PARITY=100_PERCENT_FOR_TESTED_300001_MS_BOUNDARY`

`SIGNAL_POLLER_WIRED=NO_FROZEN_PARITY`

`ORDER_LIFECYCLE_IDENTITY_VERIFIED=YES_FOR_TESTED_OID_TRANSITIONS`

`NO_ZOMBIE_LIFECYCLES=YES_FOR_TESTED_ZERO_AND_ARMED_FLATTEN_PATHS`

`NO_PREMATURE_OWNERSHIP_REMOVAL=YES_FOR_EXPOSURE_ZERO_PROOF`

`POST_EXECUTION_EXCEPTION_COVERAGE=INCOMPLETE_ECONOMIC_FINALIZATION`

`UNCERTAIN_LIVE_EXPOSURE_RECOVERY=YES_MOCKED_TRANSPORT`

`PARTIAL_CLOSE_IDENTITY_VERIFIED=YES_MOCKED_TRANSPORT`

`DELAYED_CLOSE_ECONOMICS_VERIFIED=YES_FOR_NORMAL_TIME_EXIT_FIXTURES`

`FINALIZED_EXIT_IDEMPOTENT=YES_FOR_NORMAL_TIME_EXIT_FIXTURE`

`ECONOMICS_TRANSACTION_ATOMIC=YES`

`FEE_DOUBLE_COUNT_PREVENTED=YES_FOR_TESTED_SOURCES`

`FUNDING_DOUBLE_COUNT_PREVENTED=YES_FOR_TESTED_SOURCES`

`ECONOMICS_REPLAY_IDEMPOTENT=YES_FOR_TESTED_FRESH_SCHEMA`

`CURRENT_PILOT_NAV_AUTHORITATIVE=NO_UNCERTAIN_RECOVERY_CLOSE_RACE`

`DYNAMIC_COMPOUNDING_CORE_VERIFIED=YES_ARITHMETIC_ONLY`

`OWNERSHIP_FALLBACK_REMOVED=YES_FOR_EXACT_POSITION_AND_HISTORY_MATCHING`

`SHARED_POSITION_CONFLICT_GUARD_VERIFIED=YES_MOCKED_TRANSPORT`

`PILOT_STOP_ID_OWNERSHIP_VERIFIED=YES`

`ADAPTIVETREND_ISOLATION_VERIFIED=YES_MOCKED_TRANSPORT`

`ADAPTIVETREND_REGRESSION_PASS=YES_SELECTED_TESTS`

`FROZEN_SPEC_UNCHANGED=YES`

`BITGET_AUTH_READ_VERIFIED=NO_EXPECTED_EXTERNAL_RUNTIME`

`READY_FOR_DEPLOYED_RUNTIME_VERIFICATION=NO`

`BLOCKERS=PRODUCTION_EXCLUDES_100_TO_168_BAR_MARKETS_ACCEPTED_BY_FROZEN_RESEARCH;MINIMUM_DEPTH_RULE_REMOVED_FROM_POLLER;UNCERTAIN_RECOVERY_SAFE_CLOSES_BEFORE_DELAYED_CLOSE_ECONOMICS;GLOBAL_POST_EXECUTION_ECONOMIC_RECOVERY_INCOMPLETE;CURRENT_NAV_CAN_OMIT_RECOVERY_CLOSE_COSTS`

`REAL_ORDER_ARMED=FALSE`

`REAL_ORDERS_SENT=0`

`FINAL_STATUS=IMPLEMENTATION_BLOCKED`

## Findings

### Classifier formula alignment — improved, boundary parity still fails

For markets with at least 169 completed bars, production now matches the key
research feature definitions:

- log returns;
- trailing 168 observations;
- sample standard deviation times `sqrt(24)`;
- ticker `usdtVolume`;
- ticker `bidPr`/`askPr` spread;
- pandas-compatible average percentile ranks.

The independent pandas fixture now reflects those definitions, and selection,
ranking, side, timestamp, and the explicit 300001 ms stale boundary match in
that mature synthetic snapshot.

Full point-in-time universe parity still fails:

- The frozen specification requires a minimum of 100 completed hourly bars.
- Actual `run_hv_pilot.symbol_features()` computes seven-day volatility from
  whatever trailing observations are available; with 100 bars it produces a
  valid sample standard deviation and the classifier can admit the market.
- Production first permits `len(completed) >= 100`, but then requires
  `len(returns) >= 168`. It therefore silently drops all markets with 100–168
  completed bars.
- The golden client supplies 1,000 bars for every market and does not test the
  mandated minimum-history boundary.

The production change also removed the order-book depth check. Frozen
`FROZEN_SPECS.json` requires `minimum_current_depth_usdt = 1000`; the current
poller does not query or enforce it. The research classifier itself did not own
that separate eligibility gate, but the frozen full-selection pipeline does.

Thus point-in-time universe membership and downstream rankings can differ even
though the mature fixture passes. `FROZEN_POLLER_PARITY=100_PERCENT` remains
false.

### Uncertain exposure recovery — exposure safety passes

The armed mocked-transport test exercises the real adapter path: exact pilot
working orders are canceled, exact owned positions are reduce-only/full closed
through the canonical client method, exact pilot stops are canceled, and fresh
truth must show zero positions/orders/stops before exact-OID SAFE_CLOSED.

When disarmed, ownership remains HALTED_UNCERTAIN and no mutation occurs. The
zero-exposure case is restart-safe and idempotent. This closes the previously
identified exposure-zombie defect in the tested ownership model.

### Uncertain recovery prematurely ends economic ownership — CAPITAL RISK

The recovery close immediately appends SAFE_CLOSED after zero-exposure proof.
Unlike normal time exit, it creates no EXIT_PENDING/economics checkpoint and
does not wait for delayed close history.

If Bitget position history is eventually consistent, the immediate `truth()`
calls after close may not yet contain the recovery close fee or realized PnL.
SAFE_CLOSED then removes the lifecycle from `_owned()`, so later truth calls no
longer query/attribute that history. Recovery-close economics can be lost
permanently.

The new armed test verifies mutations and zero exposure but supplies no delayed
history and asserts no final NAV. Therefore post-execution safety is not
complete and pilot NAV is not authoritative.

### Normal delayed close and partial-close checkpoint

The normal time-exit path retains the checkpoint fix: only same-position
realized events after the exit checkpoint can finalize ownership. The combined
prior-partial plus delayed-final fixture passes, restart finalizes once, and
partial A/B identities remain distinct and replay-safe.

Those results apply to normal time exit, not the separate uncertain-recovery
close path described above.

### NAV and compounding

Atomic economic insertion, tested fee/funding deduplication, partial A/B,
normal delayed close, and arithmetic sizing remain green. The formula test
correctly derives 10% single and 20% gross caps.

Because recovery-close economics can be omitted, after-cost NAV, high-water
mark, kill threshold, and dynamic compounding are not authoritative across all
canonical close paths. Compounding is YES only as isolated arithmetic.

### Preserved safety

Exact position/history ownership, same-symbol foreign conflict guards, exact
pilot-stop cancellation, AdaptiveTrend isolation tests, frozen hash, and hard
disarm do not regress. No authenticated read or real order occurred.

## Tests and checks run

- Selected parity, pilot, real-adapter, canonical lifecycle, uncertain armed
  recovery, partial-close, delayed-close, ownership, economics, AdaptiveTrend,
  TP/SL/BE, recovery, portfolio, monitor, and runner suites — 64 passed.
- Production modules compiled successfully.
- Builder-range `git diff --check` passed.
- Frozen-spec SHA-256 matched.
- Original research feature/classifier code was compared directly with
  production and the independent test implementation, including minimum-history
  and depth eligibility boundaries.

## Risk impact

No real order or exchange mutation was attempted. The runtime remained
hard-disarmed. No credentials, `.env` files, production process, position,
order, stop, leverage, capital, or AdaptiveTrend state was touched. This
verifier added only this report.

## Remaining concerns

Honor the frozen 100-bar minimum while computing research-equivalent volatility
over available observations, and restore the frozen depth eligibility check.
Route uncertain-recovery closes through the same delayed economic checkpoint as
normal time exits before SAFE_CLOSED. Then rerun complete NAV/compounding and
external authenticated Runner verification.
