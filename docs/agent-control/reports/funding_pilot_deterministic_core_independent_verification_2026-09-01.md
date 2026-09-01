# Funding pilot deterministic core — independent verification

## Scope

Independent verification of builder commit
`64eab83a3261c4b334e7657a12ee4dcc58ede088`. No builder file, credential
file, production runtime, or exchange state was modified.

## Required verdicts

`FROZEN_POLLER_PARITY=FAIL_NOT_100_PERCENT`

`SIGNAL_POLLER_WIRED=NO_FROZEN_PARITY`

`ORDER_LIFECYCLE_IDENTITY_VERIFIED=NO`

`NO_ZOMBIE_LIFECYCLES=NO`

`TIME_EXIT_ENTRY_IDENTITY_VERIFIED=YES`

`ECONOMICS_TRANSACTION_ATOMIC=YES`

`FEE_DOUBLE_COUNT_PREVENTED=YES_FOR_TESTED_SOURCES`

`FUNDING_DOUBLE_COUNT_PREVENTED=YES_FOR_TESTED_SOURCES`

`PARTIAL_CLOSE_IDENTITY_VERIFIED=NO_TEST_ABSENT_AND_EXCHANGE_ID_SEMANTICS_UNVERIFIED`

`ECONOMICS_REPLAY_IDEMPOTENT=YES_FOR_FRESH_SCHEMA_NORMAL_RESTART`

`DELAYED_CLOSE_ECONOMICS_VERIFIED=NO`

`CURRENT_PILOT_NAV_AUTHORITATIVE=NO`

`DYNAMIC_COMPOUNDING_CORE_VERIFIED=NO`

`OWNERSHIP_FALLBACK_REMOVED=YES_FOR_EXACT_POSITION_AND_HISTORY_MATCHING`

`SHARED_POSITION_CONFLICT_GUARD_VERIFIED=YES_MOCKED_TRANSPORT`

`PILOT_STOP_ID_OWNERSHIP_VERIFIED=YES`

`ADAPTIVETREND_REGRESSION_PASS=YES`

`FROZEN_SPEC_UNCHANGED=YES`

`BITGET_AUTH_READ_VERIFIED=NO_EXPECTED_EXTERNAL_RUNTIME`

`RUNTIME_VERIFICATION_COMMANDS=python3_-m_funding_pilot.read_only_verify;python3_-m_pytest_-q_tests/test_funding_pilot_real_runtime.py_tests/test_funding_signal_parity.py`

`REAL_ORDER_ARMED=FALSE`

`REAL_ORDERS_SENT=0`

`READY_FOR_DEPLOYED_RUNTIME_VERIFICATION=NO`

`BLOCKERS=FULL_RECORDED_POLLER_GOLDEN_TEST_ABSENT;FAILED_POST_FILL_LIFECYCLE_DISCARDS_LIVE_OWNERSHIP;POST_EXECUTION_EXCEPTION_TERMINALIZATION_INCOMPLETE;PARTIAL_CLOSE_AND_DELAYED_HISTORY_TESTS_ABSENT;CLOSE_EVENT_ID_SEMANTICS_UNAUTHENTICATED;CURRENT_NAV_NOT_PROVEN`

`FINAL_STATUS=IMPLEMENTATION_BLOCKED`

## Findings

### Frozen poller parity — unchanged failure

No full recorded-snapshot golden parity test was added by this commit. The
existing parity file still contains only three pure funding-event formula
tests. `last_audit` exposes universe, ranking, stale symbols, and selection,
but no test compares those values against a research replay on identical
recorded inputs. Turnover, event freshness, symbol selection, side, and
timestamp are not verified end to end.

Increasing the candle request limit to 1000 and emitting audit fields are not
evidence of 100% parity. Research and production remain separate
implementations. All seven required parity dimensions therefore remain
unproven.

### Lifecycle terminalization — CAPITAL RISK

The commit adds explicit `FAILED` events for missing persisted protection,
missing opening fee, reconciliation exceptions, and several stop-mismatch
outcomes. Successful time exits remain tied to the exact entry OID.

The state model is still unsafe after an exchange fill:

- `_owned()` treats every `ENTRY_TERMINAL`, including `FAILED`, as closed and
  removes its ownership metadata.
- A post-execution reconciliation exception may occur while the filled position
  remains open. The code appends `FAILED` and immediately rethrows without
  flattening or proving zero exposure. Subsequent recovery no longer sees that
  lifecycle as pilot-owned.
- Missing persisted protection and missing opening fee similarly terminalize
  ownership without first proving the already-executed exchange position flat.
- Exceptions from opening-fee insertion, STOP_ACK append, close mutation,
  intermediate truth calls, order cancellation, or stop cancellation are not
  enclosed by a whole-post-execution terminal/recovery owner.

Thus some zombies are hidden rather than recovered: the local lifecycle is
terminal while exchange exposure may remain. Required intermediate states such
as EXECUTED, PROTECTION_PENDING, EXIT_PENDING, and HALTED are also not modeled
as a complete transition graph for restart at every state.

### Atomic economics — implementation proven

`append_economic_once()` inserts the ECONOMICS event and its unique source row
inside one SQLite transaction. The source table has a primary-key constraint,
so a process cannot commit the event without its dedupe identity through this
method. Tested opening-fee and funding/close overlap replays remain stable on a
fresh-schema restart. `ECONOMICS_TRANSACTION_ATOMIC=YES` is justified.

### Partial-close identity — implementation suggestive, verification missing

Closed history now selects a close order, order, trade, row, close, or update ID
for closing fee and realized PnL keys, rather than position ID alone. That can
represent two partial-close rows if Bitget supplies distinct stable IDs.

No builder test creates partial close A plus partial close B, despite the
mandate explicitly requiring it. Authenticated schema behavior is unavailable,
and a mutable update timestamp is not proven to be a stable economic source ID.
The implementation also keys opening fee once per entry OID, which is not a
model for multiple entry fills/fees. Partial-close and fill-level identity are
therefore not verified.

### Delayed-close checkpoint — logic exists, proof and lifecycle hygiene fail

A close without immediate realized-PnL history now records `EXIT_PENDING` and
keeps CANONICAL_OPEN ownership active. Later ticks call exchange truth and
append exact-OID `CANONICAL_TIME_EXIT` once `realized_pnl:<close_id>` appears.
This addresses the previous immediate-termination race in principle.

However:

- No delayed-history test exists.
- The logic assumes PositionManager's close order ID exactly matches one of the
  history row identity fields selected by the adapter; this is not authenticated
  or fixture-tested with a delay.
- Every historical `EXIT_PENDING` is processed on every later tick, even after
  it has been finalized, causing duplicate CANONICAL_TIME_EXIT events.
- Stop cancellation and local position closure happen before delayed economics,
  whereas the mandate's required audit sequence places final attributable
  economics reconciliation before residual-stop removal and CLOSED.

`DELAYED_CLOSE_ECONOMICS_VERIFIED` remains NO.

### NAV and compounding

Normal fresh-schema fixtures prove arithmetic and dynamic 10%/20% sizing from
recorded economics. Complete NAV authority does not follow because partial
fills/closes and delayed close economics are unverified, post-fill FAILED
lifecycle ownership can disappear, and authenticated funding/history identity
semantics remain unknown. Kill-switch and compounding inputs can therefore be
incomplete.

### Preserved ownership protections

Exact exchange-position/history matching, foreign same-symbol conflict guards,
exact pilot-stop cancellation, and selected AdaptiveTrend regression tests do
not regress. These remain YES at the mocked-transport level.

### External runtime handoff

Authenticated reads remain unavailable here. The GET-only reachability command
is:

`python3 -m funding_pilot.read_only_verify`

The builder's second command is:

`python3 -m pytest -q tests/test_funding_pilot_real_runtime.py tests/test_funding_signal_parity.py`

That second command runs mocked tests; it does not classify authenticated live
account rows through the production adapter. A deployed authenticated adapter
classification command still does not exist. Both commands must run only on
the authorized Runner with secure credential injection and without exposing
secrets.

## Tests and checks run

- Selected parity, pilot, real-adapter, canonical lifecycle, ownership,
  economics, AdaptiveTrend, TP/SL/BE, recovery, portfolio, monitor, and runner
  suites — 56 passed.
- Production module `py_compile` — passed.
- Builder-range `git diff --check` — passed.
- Frozen-spec SHA-256 verification — matched.
- SQLite atomicity, post-fill ownership, partial-close identity, delayed-close,
  restart, NAV, and compounding code inspection — completed.

## Risk impact

No real order or exchange mutation was attempted. The runtime remained
hard-disarmed. No credentials, `.env` files, production process, position,
order, stop, leverage, capital, or AdaptiveTrend state was touched. This
verifier added only this report.

## Remaining concerns

Add one shared canonical poller and full recorded golden replay. Keep ownership
active in FAILED/HALTED recovery states until exchange zero is proven. Wrap the
entire post-execution path in a recovery state machine. Add crash, restart-at-
every-state, partial-close A/B, delayed-history, and finalized-pending tests.
Only then can NAV/compounding be declared deterministic and the core handed to
authenticated deployed-runtime verification.
