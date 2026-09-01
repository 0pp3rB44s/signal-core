# Funding pilot final isolation — independent verification

## Scope

Independent verification of builder commit
`b931c35b7b51e6364b6c326802d7c9fb9acac15d` against the final runtime
isolation and exchange-read mandate. No builder file, credential file,
production process, or exchange state was modified.

## Required verdicts

`SIGNAL_POLLER_WIRED=NO`

`BITGET_AUTH_READ_VERIFIED=NO`

`OWNERSHIP_FALLBACK_REMOVED=NO`

`SHARED_POSITION_CONFLICT_GUARD_VERIFIED=NO`

`PILOT_STOP_ID_OWNERSHIP_VERIFIED=YES`

`FEES_FUNDING_PNL_WIRED=NO`

`CANONICAL_PATH_VERIFIED=NO`

`NATIVE_STOP_VERIFIED=YES_MOCKED_TRANSPORT_BENEATH_REAL_ADAPTER`

`TIME_EXIT_VERIFIED=YES_MOCKED_TRANSPORT_BENEATH_REAL_ADAPTER`

`POST_EXIT_ZERO_PILOT_EXPOSURE_VERIFIED=YES_MOCKED_TRANSPORT_BENEATH_REAL_ADAPTER`

`RESTART_RECOVERY_VERIFIED=NO`

`KILL_SWITCH_VERIFIED=NO`

`DYNAMIC_COMPOUNDING_VERIFIED=NO`

`ADAPTIVETREND_ISOLATION_VERIFIED=NO`

`SAME_SYMBOL_CONFLICT_TEST=FAIL_INCOMPLETE_ORDER_CONFLICT_COVERAGE`

`FROZEN_SPEC_UNCHANGED=YES`

`REAL_ORDER_ARMED=FALSE`

`REAL_ORDERS_SENT=0`

`READY_FOR_FINAL_OWNER_LAUNCH_DECISION=NO`

`BLOCKERS=BITGET_AUTH_READ_UNAVAILABLE;FOREIGN_SAME_SYMBOL_ORDERS_NOT_CONFLICTED;STALE_SYMBOL_OWNERSHIP_EVENT_CAN_OVERRIDE_CURRENT_IDENTITY;OPEN_FEES_AND_FUNDING_ABSENT_FROM_NAV;FROZEN_SIGNAL_PARITY_NOT_PROVEN_AND_TURNOVER_INPUT_DIFFERS`

`FINAL_STATUS=IMPLEMENTATION_BLOCKED`

## Findings

### Exact position and economics checks — improved but not complete

The adapter now requires an exact persisted exchange position ID to classify
an exchange position and to ingest closed-position history. The former
same-symbol-only acceptance path was removed from those two operations.
Foreign same-symbol history without that ID is ignored. This is a material
improvement.

However, the broader ownership fallback is not fully removed:

- `_owned()` still collapses all historical ownership events into one record
  per symbol.
- It merges event kinds in fixed order: all `ENTRY_INTENT`, then all
  `STOP_ACK`, then all `CANONICAL_OPEN`. A historical `CANONICAL_OPEN` for a
  prior lifecycle can overwrite the newer `STOP_ACK` identity for a just-filled
  lifecycle on the same symbol.
- In that case the first post-entry `exchange.truth()` can raise an identity
  mismatch before `CanonicalFundingPilot.process_signal()` reaches its
  explicit mismatch-flatten branch. The newly filled position may remain open
  while the method exits fail-closed locally.

Ownership must be lifecycle keyed and active-state aware, not symbol keyed.

### Shared exposure guard — DEFECT / CAPITAL RISK

The explicit test proves an unowned same-symbol position fails without a
mutation. The mandate also requires a conflict for any existing unproven
same-symbol order. `truth()` filters foreign pending orders out of
`pilot_working_orders` and does not reject them. Foreign same-symbol plan/stop
orders are likewise ignored unless they match the pilot stop ID.

Therefore a pending AdaptiveTrend order on the requested pilot symbol can
coexist through pilot admission and later create aggregated exposure. The
required same-symbol position/order conflict rule is not verified, and the
collision test is incomplete.

### Pilot stop identity and time exit — PROVEN at harness level

PositionManager now requires the exact persisted exchange position ID before
mutation and cancels the exact persisted pilot stop through
`cancel_futures_plan_order`. It no longer invokes symbol-wide stop
cancellation. It then rechecks position zero, stop absence, and pilot working
orders. The mocked transport beneath production classes proves the intended
time-exit lifecycle and does not call the broad AdaptiveTrend stop path.

This supports YES for stop-ID ownership, native-stop/time-exit/post-exit
harness behavior, but it does not overcome the lifecycle ownership and
authenticated-read blockers.

### Stop mismatch cleanup — incomplete under stale identity

The explicit post-ack mismatch branch now removes adapter-classified working
orders and stops and verifies all three pilot collections are empty. That path
passes the real-adapter/mock-transport test. The stale ownership-event failure
described above can cause `truth()` itself to raise before that cleanup path is
entered, so the canonical lifecycle as a whole remains NO.

### Signal poller — production construction present, frozen parity not verified

The runner now constructs `FrozenFundingCrowdingSignalPoller` by default and
the poller verifies the frozen file hash at construction and each call. There
is no direct automated parity test for this poller, and code inspection finds
material divergence/ambiguity from the frozen research pipeline:

- The research universe uses 24-hour USDT turnover for the turnover percentile.
  The production poller uses the latest completed one-hour candle's quote
  volume as its turnover feature.
- `_pct_rank()` gives tied values their maximum empirical rank, while the
  frozen pandas research implementation uses average ranks.
- The most recent published funding event can be emitted long after its event
  timestamp after restart, using the historical event-time open as the plan
  reference, without an event-freshness gate.

The mandated actual frozen signal logic is therefore not proven. Merely
constructing this poller is insufficient for `SIGNAL_POLLER_WIRED=YES` under
the mandate's parity requirement.

### Fees, funding, PnL, NAV, and compounding — incomplete

Closed history is now attributed only by exact exchange position ID, and its
realized PnL, open fee, close fee, and funding fields enter the ledger.
Unrealized PnL enters from the exact open position.

During an open lifecycle, however, confirmed opening fees persisted by
ExecutionService and accrued funding are not ingested into the pilot ledger.
They enter only if/when a matching closed-position-history row appears. Current
pilot NAV can therefore be overstated during the holding period. Authenticated
Bitget funding sign and history semantics also remain unrehearsed.

Because the 10%/20% formulas depend on that incomplete NAV,
`FEES_FUNDING_PNL_WIRED` and `DYNAMIC_COMPOUNDING_VERIFIED` remain NO.

### Restart and kill switch — blocked by ownership/economics defects

Restart repair and the 5% cancel/flatten/latch mechanism pass component/harness
tests when supplied one unambiguous lifecycle. Restart still reconstructs from
the symbol-collapsed ownership map, and the kill switch depends on incomplete
current economics. A stale lifecycle identity can also block safe
reconciliation before recovery. Neither is release-verifiable yet.

### Authenticated reads — external blocker

The builder truthfully reports `CREDENTIALS_AVAILABLE=False`. This verifier did
not inspect or expose credentials and had no independently authorized secure
credential access. Mocked transport below the real adapter is useful code-path
evidence but is not an authenticated Bitget read. Therefore
`BITGET_AUTH_READ_VERIFIED=NO`.

### Hard disarm, regression, and frozen hash — PROVEN

Production construction still passes the literal `armed_live=False`, the
adapter rejects writes while disarmed, and this audit sent zero real orders.
Selected AdaptiveTrend, TP/SL/BE, recovery, portfolio, runner, and monitoring
tests pass. The frozen file still hashes to
`cda7ecb21e6fd089ef98abb8047d409285825a37d6a2e87510d73cf653ea2e13`.

## Tests and checks run

- Selected funding-pilot, real-adapter, canonical entry, time-exit, lifecycle,
  recovery, isolation, portfolio, monitor, and runner suites — 44 passed.
- Production module `py_compile` — passed.
- Builder-range `git diff --check` — passed.
- Frozen-spec SHA-256 verification — matched.
- Production ownership-event ordering, same-symbol position/order conflict,
  exact stop mutation, economics, poller parity, restart, and kill call-site
  inspection — completed.

## Risk impact

No real order or exchange mutation was attempted. The runtime remained
hard-disarmed. No credentials, `.env` files, production process, position,
order, stop, leverage, capital, or AdaptiveTrend state was touched. This
verifier added only this report.

## Remaining concerns

Key ownership by an immutable lifecycle identity rather than symbol and retire
closed ownership records. Reject all unproven same-symbol positions, pending
orders, and plan orders before entry. Ensure reconciliation exceptions after a
fill enter a guaranteed safe recovery owner. Ingest confirmed opening fees and
accrued funding during the open lifecycle. Derive the poller directly from the
frozen specification/research implementation and add fixture parity tests,
including tie handling and stale-event rejection. Finally perform the mandated
authenticated read-only Bitget rehearsal before requesting launch approval.
