# Independent audit — runtime funding parity `56aa132`

Date: 2026-09-02

Builder commit: `56aa13228cebfdeeec71ba69849bdf87aea3a386`

Scope: local source, mocked exchange-truth, accounting, and restart verification only. No `.env` or credentials were accessed, no network request was made, and no exchange operation was attempted.

## Verdict

The new canonical funding-bill helper is genuinely shared by `read_only_verify` and `BitgetPilotExchangePort`. Its GET path, `contract_settle_fee` query, `data.bills` envelope, exact classifier, empty-history handling, and required schema are correct. Exact position-ID plus exact-symbol attribution and bill-ID exactly-once replay also work.

The runtime is not ready for deployment/auth rerun because no-position-ID fallback attribution is unsafe under same-symbol conflict. `BitgetPilotExchangePort.truth()` ingests funding before reading and validating live positions and before its same-symbol foreign position/order/plan conflict guards. An independent hostile snapshot containing an owned pilot lifecycle, a different-position-ID foreign DOGE position, and a no-position-ID DOGE funding bill caused `truth()` to raise `FailClosed` for the position mismatch only after `funding:foreign-funding` had already been atomically committed and attributed to the pilot position. The false event survives restart and corrupts NAV/compounding.

The fallback window is also wider than the actual live position: `ownership_started_at_ms` is set from the first lifecycle event (`ENTRY_INTENT`) rather than the exact-position `CANONICAL_OPEN`/confirmed-fill boundary. A same-symbol bill after intent but before confirmed pilot ownership can therefore qualify.

## Evidence classifications

- **PROVEN** — Both the read-only verifier and live exchange adapter call `fetch_funding_bills`; query/envelope/classification logic has one canonical implementation.
- **PROVEN** — Canonical request is authenticated GET `/api/v2/mix/account/bill` with uppercase product type, `businessType=contract_settle_fee`, and bounded `limit`.
- **PROVEN** — Only exact funding business types are returned; fee, PnL/order, transfer, liquidation, and other bills are excluded. Empty `data.bills=[]` is valid; malformed funding schema fails closed.
- **PROVEN** — Rows carrying a position ID require both exact exchange position ID and exact symbol.
- **DEFECT / CAPITAL RISK** — Rows without a position ID can be committed before same-symbol foreign position/order/plan conflicts are validated.
- **DEFECT / CAPITAL RISK** — Fallback uses the `ENTRY_INTENT` timestamp rather than a confirmed exact-position-open boundary.
- **PROVEN** — `append_economic_once(funding:<billId>)` atomically writes event/source identity and prevents duplicates across repeated polls and restart.
- **PROVEN** — Existing replay, delayed-close economics, partial-close, NAV arithmetic, and compounding regression tests pass for non-conflicting correctly attributed inputs.
- **INSUFFICIENT / BLOCKED BY DEFECT** — Current pilot NAV cannot be called authoritative while fallback can durably ingest foreign funding.
- **PROVEN** — Builder scope outside reports is confined to funding helper, funding adapter/verifier, and tests. Production settings/runner, environment files, execution arming, and AdaptiveTrend are unchanged.
- **UNKNOWN / EXTERNAL RUNTIME** — Authenticated Bitget reads remain an external gate and were not attempted.

## Required fields

CANONICAL_FUNDING_HELPER_SHARED=YES

READ_ONLY_VERIFY_USES_CANONICAL_HELPER=YES

BITGET_PILOT_EXCHANGE_USES_CANONICAL_HELPER=YES

FUNDING_ENDPOINT=GET_/api/v2/mix/account/bill

FUNDING_BUSINESS_TYPE=contract_settle_fee

FUNDING_RESPONSE_ENVELOPE=data.bills

FUNDING_EXACT_CLASSIFIER=YES

EMPTY_FUNDING_HISTORY_VALID=YES

FEE_BILLS_EXCLUDED=YES

REALIZED_PNL_ORDER_BILLS_EXCLUDED=YES

TRANSFER_BILLS_EXCLUDED=YES

LIQUIDATION_BILLS_EXCLUDED=YES

FUNDING_SCHEMA_FAIL_CLOSED=YES

EXACT_POSITION_AND_SYMBOL_ATTRIBUTION=YES

NO_POSITION_ID_ATTRIBUTION_SAFE=NO

LIFECYCLE_WINDOW_EXACT=NO_ENTRY_INTENT_PRECEDES_CONFIRMED_POSITION

SAME_SYMBOL_CONFLICT_GUARD_BEFORE_ECONOMIC_WRITE=NO

FOREIGN_FUNDING_INGESTION_PREVENTED=NO

BILL_ID_ATOMIC_DEDUP=YES

POLL_REPLAY_IDEMPOTENT=YES

RESTART_REPLAY_IDEMPOTENT=YES

ECONOMICS_REPLAY_VERIFIED=YES_FOR_CORRECTLY_ATTRIBUTED_ROWS

CURRENT_PILOT_NAV_AUTHORITATIVE=NO

DYNAMIC_COMPOUNDING_CORE_VERIFIED=NO_END_TO_END_DUE_FOREIGN_FUNDING_RISK

READ_ONLY_SETTINGS_DECOUPLED=YES_UNCHANGED

PRODUCTION_SETTINGS_GUARD_UNCHANGED=YES

READ_ONLY_TRANSPORT_ENFORCED=YES_UNCHANGED

BITGET_AUTH_READ_VERIFIED=NO_EXPECTED_EXTERNAL_RUNTIME

ENV_LIVE_UNCHANGED=YES_COMMIT_SCOPE_ONLY_NOT_ACCESSED

SECRETS_EXPOSED=NO

ADAPTIVETREND_UNTOUCHED=YES

REAL_ORDER_ARMED=FALSE

REAL_ORDERS_SENT=0

READY_FOR_DEPLOYMENT_AND_AUTH_READ_RERUN=NO

BLOCKERS=NO_POSITION_ID_FUNDING_IS_COMMITTED_BEFORE_SAME_SYMBOL_CONFLICT_VALIDATION; FALLBACK_WINDOW_STARTS_AT_ENTRY_INTENT_NOT_CONFIRMED_EXACT_POSITION_OPEN; FALSE_FUNDING_SURVIVES_RESTART_AND_CORRUPTS_NAV

FINAL_STATUS=IMPLEMENTATION_BLOCKED

## Tests and checks

- Ran verifier, live adapter, deterministic pilot, parity, configuration-security, and canonical entry-path tests: `80 passed`.
- Inspected both helper call sites and the complete `truth()` ordering.
- Independently supplied mixed bill types and verified canonical filtering behavior by source inspection and existing tests.
- Independently created a same-symbol foreign exact-position mismatch plus a no-position-ID funding bill inside the fallback window. `truth()` raised `FailClosed`, but the ledger already contained `funding:foreign-funding` with `funding=0.7` attributed to the pilot exchange position ID.
- Verified source identity is durable in `economic_sources`, so later restart/reconciliation cannot automatically undo the false attribution.
- Reviewed atomic SQLite event/source insertion and poll/restart replay tests.
- Confirmed no production configuration, environment, AdaptiveTrend, execution arm, credential, network, or order action occurred.

## Required remediation

Obtain and validate complete authoritative exchange position/order/plan conflict truth before any fallback economic write. Use the confirmed exact pilot position-open timestamp, not `ENTRY_INTENT`, as the lower attribution boundary. For no-position-ID rows, require the snapshot to prove one and only one exact owned same-symbol pilot lifecycle and no foreign same-symbol position, regular order, plan, or protection throughout the attributable window; otherwise fail closed without writing. Add hostile tests asserting both failure and zero ledger mutation, including restart after the rejected snapshot.
