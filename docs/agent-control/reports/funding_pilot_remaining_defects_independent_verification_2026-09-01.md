# Funding pilot remaining defects — independent verification

## Scope

Independent verification of builder commit
`20e17ddbd7250f15a6f08c77e8481b8a439fdb57`. No builder file, credential
file, production runtime, or exchange state was modified.

## Required verdicts

`FROZEN_POLLER_PARITY=FAIL_NOT_100_PERCENT`

`SIGNAL_POLLER_WIRED=NO_FROZEN_PARITY`

`OWNERSHIP_FALLBACK_REMOVED=YES_FOR_POSITION_AND_HISTORY_MATCHING`

`ORDER_LIFECYCLE_IDENTITY_VERIFIED=NO`

`SAME_SYMBOL_CONFLICT_COVERAGE=COMPLETE_IN_MOCKED_TRANSPORT`

`SHARED_POSITION_CONFLICT_GUARD_VERIFIED=YES_IN_MOCKED_TRANSPORT`

`OPEN_POSITION_FEES_WIRED=YES_BUT_LATER_DOUBLE_COUNTED`

`ACCRUED_FUNDING_WIRED=YES_BUT_LATER_DOUBLE_COUNTED`

`FEES_FUNDING_PNL_WIRED=NO`

`CURRENT_PILOT_NAV_AUTHORITATIVE=NO`

`BITGET_AUTH_READ_VERIFIED=NO`

`EXTERNAL_RUNTIME_CREDENTIAL_BLOCKER=YES`

`CANONICAL_PATH_VERIFIED=NO`

`NATIVE_STOP_VERIFIED=YES_MOCKED_TRANSPORT_BENEATH_REAL_ADAPTER`

`TIME_EXIT_VERIFIED=YES_MOCKED_TRANSPORT_BENEATH_REAL_ADAPTER`

`POST_EXIT_ZERO_PILOT_EXPOSURE_VERIFIED=YES_MOCKED_TRANSPORT_BENEATH_REAL_ADAPTER`

`RESTART_RECOVERY_VERIFIED=NO`

`KILL_SWITCH_VERIFIED=NO`

`DYNAMIC_COMPOUNDING_VERIFIED=NO`

`ADAPTIVETREND_ISOLATION_VERIFIED=YES_MOCKED_TRANSPORT_BENEATH_REAL_ADAPTER`

`ADAPTIVETREND_REGRESSION_PASS=YES`

`FROZEN_SPEC_UNCHANGED=YES`

`REAL_ORDER_ARMED=FALSE`

`REAL_ORDERS_SENT=0`

`READY_FOR_FINAL_OWNER_LAUNCH_DECISION=NO`

`BLOCKERS=FULL_POLLER_PARITY_MISSING;TURNOVER_SOURCE_DIFFERS_FROM_RESEARCH;OPEN_AND_CLOSED_ECONOMICS_DOUBLE_COUNT;REJECTED_ENTRY_INTENTS_NEVER_TERMINATE;AUTHENTICATED_BITGET_READ_UNAVAILABLE`

`FINAL_STATUS=IMPLEMENTATION_BLOCKED`

## Findings

### Frozen poller parity — DEFECT / insufficient test coverage

The new pure `frozen_funding_decision()` matches the pandas event formula for
the two monotonic synthetic direction cases, and its average tie-rank formula
is mathematically aligned with pandas. That is useful narrow evidence.

The mandate requires identical-input comparison of universe membership,
rankings, event selection, symbol, side, timestamp, and stale-event decision.
The new parity file does not instantiate the poller and tests none of universe
membership, cross-sectional ranking, selected symbol, timestamp, or five-minute
freshness behavior. It contains only three pure event-formula tests.

Code inspection also shows production universe turnover is the sum of 24
hourly quote-volume candles. The frozen research point-in-time universe uses
the ticker's 24-hour USDT turnover (`usdtVolume`). These values need not match
at the snapshot boundary. The new five-minute stale-event rule is operationally
sensible but has no corresponding research/production parity fixture.

Therefore `FROZEN_POLLER_PARITY=100_PERCENT` is not established and the actual
frozen poller cannot be approved as wired.

### Lifecycle identity — improved chronology, incomplete state machine

`_owned()` now merges events in database-ID chronology and keys intermediate
lifecycle state by pilot entry client OID. This fixes the specific older-kind
overwrites-newer-kind defect. Position/history ownership also requires an exact
exchange position ID.

It is not yet a complete lifecycle state machine:

- `CANONICAL_TIME_EXIT` carries no entry client OID and closes every lifecycle
  with the same symbol rather than one exact lifecycle.
- `ENTRY_INTENT` is appended before `ExecutionService.execute()`. If admission
  rejects, execution errors, or no protected position results, no rejected or
  abandoned terminal event is persisted.
- A later signal creates a different entry OID for the same symbol. `_owned()`
  then sees two active lifecycles and fails indefinitely with `multiple active
  pilot lifecycles`, even though the first never opened an exchange position.

Chronological merging is proven, but complete order-lifecycle identity and
restart behavior are not.

### Same-symbol conflicts and AdaptiveTrend isolation — harness proof passes

The adapter now rejects foreign same-symbol regular entry orders, reduce-only
orders, stops, conditional/plan orders, multiple orders, and unowned positions
without making exchange mutations. Pilot position/history attribution requires
the exact persisted exchange position ID. PositionManager cancels only the
exact pilot stop ID. These behaviors justify COMPLETE/YES at the mocked
transport boundary and demonstrate the requested AdaptiveTrend collision
isolation within the tested adapter model.

Authenticated exchange behavior and identifier availability remain unverified,
so this evidence does not make the full canonical path ready.

### Open economics are wired but double-counted — CAPITAL RISK

The canonical entry now persists the confirmed opening fee immediately, and
the real adapter ingests exact-position funding bills while the position is
open. Unrealized PnL is taken from the exact exchange position. Those individual
wires exist.

The close path then duplicates those costs:

- `_ingest_closed_economics()` adds both `openFee` and `closeFee`, even though
  the same opening fee was already appended under `opening_fee:<entry_oid>`.
- It also adds `totalFunding`, even though exact funding bills were already
  appended and deduplicated under `funding:<bill_id>`.
- The close-summary dedupe key is independent of the opening-fee and funding
  bill keys, so no cross-source prevention exists.

A completed lifecycle therefore overstates fees and funding costs. Subsequent
fill fees are also not independently reconciled before close. Current and
post-close pilot NAV cannot be called authoritative, so kill-switch level and
dynamic compounding remain unverified.

### Canonical lifecycle consequences

Native-stop acknowledgement, exact pilot-stop time exit, orphan cleanup, and
post-exit zero checks pass through production classes with mocked transport
beneath the real adapter. These retain YES at that explicitly limited evidence
level.

The full canonical path, restart, kill switch, and compounding remain NO due to
the unterminated intent lifecycle and incorrect economics. In particular, a
risk-rejected intent can poison subsequent restart/admission without any
exchange exposure to reconcile.

### Authenticated read-only Bitget verification — external blocker

The secure runtime check reported credentials unavailable. This verifier had no
separate authorized secure access, did not inspect forbidden credential files,
and did not expose any secret. Mocked transport is not authenticated exchange
proof. The read-only rehearsal must be executed on the authoritative Runner
environment through its existing secure credential injection mechanism.

### Hard disarm, regression, and frozen hash — PROVEN

Production construction remains hardcoded to `armed_live=False`; adapter writes
fail closed while disarmed; this audit sent zero real orders. Selected
AdaptiveTrend, TP/SL/BE, recovery, portfolio, runner, and monitoring tests pass.
The frozen specification still hashes to
`cda7ecb21e6fd089ef98abb8047d409285825a37d6a2e87510d73cf653ea2e13`.

## Tests and checks run

- Selected signal-parity, funding-pilot, real-adapter, canonical lifecycle,
  ownership, AdaptiveTrend isolation, TP/SL/BE, recovery, portfolio, monitor,
  and runner suites — 54 passed.
- Production module `py_compile` — passed.
- Builder-range `git diff --check` — passed.
- Frozen-spec SHA-256 verification — matched.
- Research-versus-production universe/event implementation inspection —
  completed.
- Chronological ownership, rejection lifecycle, economics deduplication,
  conflict, restart, kill, and compounding call-site inspection — completed.

## Risk impact

No real order or exchange mutation was attempted. The runtime remained
hard-disarmed. No credentials, `.env` files, production process, position,
order, stop, leverage, capital, or AdaptiveTrend state was touched. This
verifier added only this report.

## Remaining concerns

Use one shared frozen poller implementation for validation and runtime and add
full recorded-snapshot parity tests covering universe, ranking, selection, and
freshness. Add explicit lifecycle terminal events keyed by entry OID for every
reject/error/exit. Reconcile opening fee and funding bills against close-summary
totals without duplication and cover a full open-to-close NAV fixture. Then
repeat restart, kill, and compounding verification and perform the authenticated
read-only rehearsal on the authorized Runner.
