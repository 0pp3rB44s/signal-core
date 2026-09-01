# Funding pilot release-defect closure — independent verification

## Scope

Independent verification of builder commit
`9a060de028102e1e17d3415346674652a3fd1b12`. No builder file, credential
file, production runtime, or exchange state was modified.

## Required verdicts

`FROZEN_POLLER_PARITY=FAIL_NOT_100_PERCENT`

`SIGNAL_POLLER_WIRED=NO_FROZEN_PARITY`

`ORDER_LIFECYCLE_IDENTITY_VERIFIED=NO`

`REJECTED_INTENT_TERMINATION_VERIFIED=YES_FOR_EXECUTION_REJECT_AND_EXCEPTION_ONLY`

`TIME_EXIT_ENTRY_IDENTITY_VERIFIED=YES`

`OPEN_POSITION_FEES_WIRED=YES`

`ACCRUED_FUNDING_WIRED=YES`

`FEE_DOUBLE_COUNT_PREVENTED=YES_FOR_TESTED_OPEN_CLOSE_OVERLAP`

`FUNDING_DOUBLE_COUNT_PREVENTED=YES_FOR_TESTED_BILL_CLOSE_OVERLAP`

`ECONOMICS_REPLAY_IDEMPOTENT=NO_CRASH_ATOMICITY_OR_CLOSE_LATENCY_PROOF`

`CURRENT_PILOT_NAV_AUTHORITATIVE=NO`

`OWNERSHIP_FALLBACK_REMOVED=YES_FOR_EXACT_POSITION_AND_HISTORY_MATCHING`

`SHARED_POSITION_CONFLICT_GUARD_VERIFIED=YES_MOCKED_TRANSPORT`

`PILOT_STOP_ID_OWNERSHIP_VERIFIED=YES`

`BITGET_AUTH_READ_VERIFIED=NO`

`EXTERNAL_RUNTIME_CREDENTIAL_BLOCKER=YES`

`READ_ONLY_VERIFY_GET_ONLY=YES`

`CANONICAL_PATH_VERIFIED=NO`

`NATIVE_STOP_VERIFIED=YES_MOCKED_TRANSPORT_BENEATH_REAL_ADAPTER`

`TIME_EXIT_VERIFIED=YES_MOCKED_TRANSPORT_BENEATH_REAL_ADAPTER`

`POST_EXIT_ZERO_PILOT_EXPOSURE_VERIFIED=YES_MOCKED_TRANSPORT_BENEATH_REAL_ADAPTER`

`RESTART_RECOVERY_VERIFIED=NO`

`KILL_SWITCH_VERIFIED=NO`

`DYNAMIC_COMPOUNDING_VERIFIED=NO`

`ADAPTIVETREND_ISOLATION_VERIFIED=YES_MOCKED_TRANSPORT`

`ADAPTIVETREND_REGRESSION_PASS=YES`

`FROZEN_SPEC_UNCHANGED=YES`

`REAL_ORDER_ARMED=FALSE`

`REAL_ORDERS_SENT=0`

`READY_FOR_FINAL_OWNER_LAUNCH_DECISION=NO`

`BLOCKERS=FULL_POLLER_PARITY_FIXTURE_ABSENT;STALE_EVENT_PARITY_UNPROVEN;POST_EXECUTION_FAILURES_LACK_TERMINAL_EVENTS;CLOSE_HISTORY_LATENCY_CAN_DROP_REALIZED_PNL_AND_CLOSE_FEE;ECONOMIC_APPEND_AND_DEDUPE_NOT_ATOMIC;AUTHENTICATED_BITGET_READ_UNAVAILABLE`

`FINAL_STATUS=IMPLEMENTATION_BLOCKED`

## Findings

### Poller parity remains unproven

Production turnover now reads ticker `usdtVolume`, aligning that input source
with the frozen research universe. The pure event formula and average-rank tie
handling tests continue to pass.

The required 100% parity artifact still does not exist. The parity test file
contains only three pure funding-decision cases. It never instantiates the
production poller or compares identical recorded snapshots for:

- universe membership;
- turnover values and percentiles;
- simultaneous candidate ranking;
- selected symbol and side;
- signal timestamp;
- eligible versus ineligible events;
- fresh versus stale event behavior.

The five-minute stale cutoff has no matching frozen research fixture or parity
assertion. Consequently the builder's “direct research-formula parity” evidence
does not satisfy the mandate's full poller parity requirement.

### Lifecycle terminal states remain incomplete

Execution exceptions now append `FAILED`, non-executed reports append
`REJECTED`, and time-exit records carry the exact originating entry OID. The
chronological OID-keyed ownership merge correctly removes these tested
terminal lifecycles and prevents the earlier rejected-intent poisoning case.

Not every intent reaches a terminal or OPEN state:

- If ExecutionService returns `EXECUTED` but the persisted open row is missing
  or protection is not verified, `process_signal()` raises without an
  `ENTRY_TERMINAL` event.
- If confirmed opening fee is unavailable, it raises without a terminal event.
- If the post-ack stop identity is missing/mismatched, the recovery branch may
  flatten and then raises, but it does not append `FAILED` or `CANCELLED`.
- Other exceptions after `execution_service.execute()` likewise bypass the
  narrow try/except and leave `ENTRY_INTENT` active.

Those failures can still poison future same-symbol attempts and restart. The
claim that every intent terminates is therefore false.

### Time-exit identity and ownership protections

The successful time-exit wrapper snapshots the active PositionManager row's
exact entry client OID and persists it with `CANONICAL_TIME_EXIT`. `_owned()`
uses that OID to terminate one lifecycle. Exact position IDs, foreign
same-symbol conflict guards, and exact pilot-stop cancellation remain intact in
the mocked-transport suite. AdaptiveTrend collision tests do not regress.

### Economics overlap improved, but authoritative lifecycle accounting fails

The new item keys prevent the tested duplicate opening fee, closing fee,
realized PnL, and funding bill from being counted twice on a normal replay.
Closed `totalFunding` is intentionally not ingested, avoiding overlap with
funding bills. Opening fee and accrued funding are present while open.

Two production failure modes remain:

1. `process_time_exits()` queries `exchange.truth()` once immediately after the
   close, then appends `CANONICAL_TIME_EXIT`, which makes the lifecycle inactive.
   If Bitget closed-position history is eventually consistent and does not yet
   contain the close, later reconciliations no longer call history ingestion
   for that lifecycle. Closing fee and realized PnL can be permanently omitted.
   The replay test never terminates its lifecycle and therefore misses this.
2. Each ledger economics append and its `meta` dedupe marker are two independent
   commits. A process failure after append but before marker allows the same
   item to be appended again on restart. Exactly-once ingestion is not atomic.

Additionally, keys such as `closing_fee:<position_id>` assume one final history
record per position. Partial-close rows sharing a position ID would be collapsed
without fill/transaction-level identity. Authenticated exchange behavior is
unknown.

Current NAV, kill-switch level, and dynamic sizing are therefore not yet
authoritative across the complete lifecycle.

### Read-only verifier audit and exact external handoff

`funding_pilot.read_only_verify` invokes only client read helpers whose
implementations use GET, plus one explicit private GET for account bills. It
contains no write call. It avoids printing balances, positions, order details,
or credentials and reports only row counts/status.

However, it treats empty lists as successful schema verification and does not
run the pilot adapter's ownership/economics classification. Even a successful
run would prove authenticated endpoint reachability, not the full production
classification lifecycle by itself.

Credentials are unavailable in this Codex environment. The exact authorized
handoff is:

`python3 -m funding_pilot.read_only_verify`

Run it from the authoritative deployed repository on the Runner using the
existing secure runtime credential injection mechanism. Do not copy credentials
to this worktree or expose its output beyond non-secret status/counts.

### Hard disarm, regression, and frozen hash

Production remains hardcoded to `armed_live=False`; adapter writes reject while
disarmed; this audit sent zero orders. Selected AdaptiveTrend, TP/SL/BE,
recovery, portfolio, runner, and monitoring tests pass. The frozen file hashes
to `cda7ecb21e6fd089ef98abb8047d409285825a37d6a2e87510d73cf653ea2e13`.

## Tests and checks run

- Selected parity, funding-pilot, real-adapter, canonical lifecycle, ownership,
  economics, AdaptiveTrend, TP/SL/BE, recovery, portfolio, monitor, and runner
  suites — 56 passed.
- Production module `py_compile` — passed.
- Builder-range `git diff --check` — passed.
- Frozen-spec SHA-256 verification — matched.
- All methods invoked by `funding_pilot.read_only_verify` traced to GET
  implementations — passed.
- Terminal-state, close-latency, economics dedupe/atomicity, restart, kill, and
  compounding call-site inspection — completed.

## Risk impact

No real order or exchange mutation was attempted. The runtime remained
hard-disarmed. No credentials, `.env` files, production process, position,
order, stop, leverage, capital, or AdaptiveTrend state was touched. This
verifier added only this report.

## Remaining concerns

Add full recorded-snapshot parity tests that drive the actual poller. Wrap the
entire post-intent lifecycle in terminal-state handling, including protected-row,
fee, stop-reconciliation, and recovery failures. Keep closed lifecycles pending
economic finalization until exchange history is complete. Make economic event
insertion and dedupe identity one SQLite transaction with unique constraints,
using fill/bill/transaction IDs. Then rerun restart, kill, compounding, and the
authenticated Runner read verification.
