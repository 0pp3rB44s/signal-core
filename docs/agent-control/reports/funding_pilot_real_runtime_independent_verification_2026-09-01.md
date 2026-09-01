# Funding pilot real runtime — independent verification

## Scope

Strict independent verification of builder commit
`a4fe4b1c1dc228066e56e63087e682535a958fea` against the real-runtime mandate.
No builder file, credential file, production process, or exchange state was
modified.

## Required verdicts

`REAL_RUNNER_EXISTS=YES`

`BITGET_EXCHANGEPORT_EXISTS=YES`

`READ_ONLY_EXCHANGE_TRUTH_VERIFIED=NO`

`OWNERSHIP_ISOLATION_VERIFIED=NO`

`REAL_LEDGER_WIRED=YES`

`REAL_FEES_FUNDING_PNL_WIRED=NO`

`STOP_FAILURE_FLATTEN_VERIFIED=NO`

`TIME_EXIT_VERIFIED=NO`

`POST_EXIT_ZERO_EXPOSURE_VERIFIED=NO`

`RESTART_RECOVERY_VERIFIED=NO`

`KILL_SWITCH_VERIFIED=NO`

`DYNAMIC_COMPOUNDING_VERIFIED=NO`

`ADAPTIVETREND_REGRESSION_PASS=YES`

`FROZEN_SPEC_UNCHANGED=YES`

`REAL_ORDER_ARMED=FALSE`

`REAL_ORDERS_SENT=0`

## Final report fields

`RUNTIME_BUILD_COMPLETE=NO`

`RUNNER_IMPLEMENTED=YES`

`BITGET_EXCHANGEPORT_IMPLEMENTED=YES`

`OWNERSHIP_ISOLATION_IMPLEMENTED=NO`

`AUTHORITATIVE_LEDGER_WIRED=YES`

`FEES_FUNDING_PNL_WIRED=NO_SAFE_ATTRIBUTION`

`READ_ONLY_EXCHANGE_TRUTH_VERIFIED=NO_CREDENTIALS_UNAVAILABLE`

`ADAPTIVETREND_ISOLATION_VERIFIED=NO`

`ADAPTIVETREND_REGRESSION_PASS=YES`

`FROZEN_SPEC_UNCHANGED=YES`

`READY_FOR_FINAL_OWNER_LAUNCH_DECISION=NO`

`BLOCKERS=AUTHENTICATED_READ_NOT_PERFORMED;SYMBOL_LEVEL_POSITION_AND_ECONOMICS_OWNERSHIP_AMBIGUOUS;TIME_EXIT_BROADLY_CANCELS_SAME_SYMBOL_STOPS;KILL_AND_CLOSE_CAN_FLATTEN_SHARED_POSITION;STOP_MISMATCH_DOES_NOT_VERIFY_ORPHAN_STOP_ZERO;PRODUCTION_SIGNAL_POLLER_ABSENT`

`FINAL_STATUS=IMPLEMENTATION_BLOCKED`

## Evidence

### Runner and hard disarm — PROVEN

`app.runner.StartupRunner` conditionally constructs
`CanonicalFundingPilotRunner` behind `FUNDING_PILOT_RUNTIME_ENABLED`. The
position-monitor cycle schedules `tick()`. The production constructor passes
the literal `armed_live=False`; there is no configuration path that changes
it. The concrete adapter independently rejects every mutation while disarmed.

This establishes a real scheduled read/dry-run runtime with a hard write
barrier. The production construction supplies no signal poller, however, so it
currently cannot evaluate or submit the frozen pilot signal even if a later
owner-authorized arm mechanism were added.

### Authenticated real reads — external blocker / not verified

The builder reports an authenticated read-only rehearsal could not run because
credentials were unavailable. This independent audit did not inspect forbidden
credential files or attempt to bypass that constraint. The concrete adapter
was exercised with mocked transport beneath the real adapter boundary, which
is valid write-path test evidence but is not authenticated real Bitget read
evidence. Therefore `READ_ONLY_EXCHANGE_TRUTH_VERIFIED=NO`.

### Ownership model — CAPITAL RISK / DEFECT

Pilot working orders require the deterministic `cgc-fcp-` client-order-ID
namespace, and stops can resolve by that namespace or an exact persisted stop
ID. Those parts are useful. Position and closed-economics ownership is not
deterministic enough for a shared production account:

- `_owned()` collapses ownership to one record per symbol.
- Exchange positions are classified as pilot positions solely because their
  symbol appears in the local ownership map. The exchange position row does
  not need a pilot client ID or matched exchange position ID.
- When `exchange_position_id` is absent, closed history ingestion accepts all
  returned position-history rows for that symbol merely because the local
  entry client ID has the pilot prefix. The builder test itself demonstrates
  this fallback.
- Thus an AdaptiveTrend trade on the same symbol/side can be included in pilot
  unrealized PnL, realized PnL, fees, funding, notional, and position count.
  Historical AdaptiveTrend closes can also be ingested into pilot NAV.

Unknown ownership is required to fail closed. Symbol membership is not proof
of order-to-position or close-to-strategy ownership, so isolation is NO.

### Same-symbol mutation risk — CAPITAL RISK

`close_reduce_only()` invokes a full symbol/direction close. If Bitget exposes
an aggregate same-symbol position, this can flatten AdaptiveTrend exposure that
the symbol-level map misclassified as pilot-owned.

The PositionManager time exit calls `cancel_all_futures_tpsl_orders()` for the
entire symbol/hold-side and loss-plan types. It does not cancel only the
persisted pilot stop ID. Consequently it can cancel an AdaptiveTrend stop on
the same symbol/side. This violates the explicit isolation mandate even though
the subsequent pilot residual checks pass in the mocked harness.

The kill switch inherits the same position-classification/full-close risk.
Therefore time exit, kill, post-exit safety, and AdaptiveTrend isolation cannot
be approved.

### Fees, funding, PnL, NAV, and compounding — PIPELINE PRESENT, TRUTH UNSAFE

The adapter does call position history, requires realized PnL, open/close fees,
and funding fields, persists deduplicated economics, and combines them with
exchange unrealized PnL. The ledger is real and persistent. However, because
the economics and positions are not safely attributable, the resulting pilot
NAV is not authoritative. Dynamic 10%/20% sizing mathematics passes tests but
cannot be verified against a trustworthy pilot-only NAV.

Additionally, the raw funding sign is passed directly into a ledger formula
that subtracts `funding`; authenticated exchange semantics were not rehearsed,
so receipt-versus-payment handling remains unverified.

### Stop-failure flatten — incomplete residual proof

The post-ack missing-stop harness now proves a close travels through the real
adapter above mocked transport and confirms pilot position and working-order
absence. But the mismatch path does not cancel or verify absence of
`pilot_stops` after flattening. A mismatched alternative stop can remain orphaned.
Combined with ambiguous position ownership, this does not satisfy the complete
safe flatten requirement.

### Restart recovery — structurally improved, ownership unsafe

`recover()` now closes stale local rows and creates missing PositionManager
rows for exchange positions with durable schedules and stops. Ledger HWM and
HALTED state survive restart. But reconstruction uses the same symbol-level
position ownership and may rebuild a pilot lifecycle for AdaptiveTrend
exposure. It is therefore not release-verifiable.

### Regression and frozen specification — PROVEN

The new runtime is opt-in and disarmed. Selected AdaptiveTrend, TP/SL/BE,
position recovery, entry, portfolio, monitor, and runner infrastructure tests
pass. The frozen specification hashes to
`cda7ecb21e6fd089ef98abb8047d409285825a37d6a2e87510d73cf653ea2e13`
and was not modified by the builder commit.

## Tests and checks run

- Funding-pilot, canonical-entry, lifecycle, recovery, isolation, portfolio,
  forward-paper, and position-monitor selection — 42 passed.
- Runner migration and dual-architecture infrastructure selection — 24 passed.
- Production-module `py_compile` — passed.
- Builder-range `git diff --check` — passed.
- Frozen-spec SHA-256 verification — matched.
- Production runner construction/scheduling, arming, adapter ownership,
  economics, stop, exit, restart, and kill call-site inspection — completed.

## Risk impact

The runtime remained hard-disarmed. No real order or exchange mutation was
attempted. No credentials, `.env` files, production processes, positions,
orders, stops, leverage, capital, or AdaptiveTrend state were touched. This
verifier added only this report.

## Remaining concerns

Replace symbol-only ownership with a provable entry/fill/position lifecycle
identity and explicitly fail closed when Bitget cannot distinguish same-symbol
strategy exposure. Attribute history only through exact owned fill/position
identifiers. Cancel only the persisted pilot stop/order IDs, never all
same-symbol protection. Verify orphan-stop zero after stop-mismatch flatten.
Wire the frozen signal poller. Then repeat the mocked-transport suite and the
mandatory authenticated read-only rehearsal before requesting a final owner
launch decision.
