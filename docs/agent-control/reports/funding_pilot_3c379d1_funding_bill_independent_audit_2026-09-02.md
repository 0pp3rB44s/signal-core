# Independent audit — Bitget V2 funding bill verifier `3c379d1`

Date: 2026-09-02

Builder commit: `3c379d1d44128f3f43669015e9afb842859baafc`

Scope: local source, schema, and mocked-transport verification only. No `.env` or credential source was accessed, no network request was made, and no exchange operation was attempted.

## Verdict

The read-only verifier now implements the mandated current Bitget V2 funding-bill contract: authenticated GET `/api/v2/mix/account/bill`, `businessType=contract_settle_fee`, `limit=100`, and response envelope `data.bills`. A present empty `bills` list is valid. Mixed bill responses count only exact `contract_settle_fee` rows; fee, realized-PnL/order, transfer, liquidation, and every other business type are excluded. Funding rows missing required `billId`, `amount`, `businessType`, or `cTime` fail closed.

The implementation is ready for the external deployed authenticated read-only rerun. This does not claim authenticated success, authorize launch, arm execution, or permit orders.

## Evidence classifications

- **PROVEN** — Exact method/path/parameters are GET, `/api/v2/mix/account/bill`, product type, `businessType=contract_settle_fee`, and `limit=100`.
- **PROVEN** — Only a dictionary `data` containing a list-valued `bills` is accepted; missing/wrong envelopes and non-dictionary rows fail closed.
- **PROVEN** — Empty `data.bills=[]` is endpoint-reachable, schema-valid, classification-pass, count zero.
- **PROVEN** — Mixed funding, liquidation, fee/open, realized-PnL/close, and transfer rows yield a count containing only exact funding rows.
- **PROVEN** — An exact funding row missing `cTime` fails schema and makes the verifier exit nonzero.
- **PROVEN** — The obsolete `funding_fee` business type is no longer used by the verifier; its request value is a fixed internal constant and cannot be supplied by runtime admission settings.
- **PROVEN** — Balance, position, regular-order, plan, native-stop, order-history/fill, and fee-rate classification code is unchanged from the prior independently green verifier.
- **PROVEN** — GET-only method and exact endpoint allowlist enforcement, minimal settings, missing-credential rejection, and output redaction are unchanged.
- **PROVEN** — Builder scope outside reports is limited to the read-only verifier and its tests. Production configuration/runner, execution, environment files, and AdaptiveTrend are unchanged.
- **UNKNOWN / EXTERNAL RUNTIME** — Authenticated Bitget reachability and live response schemas remain for the controlled deployed rerun.
- **PRE-EXISTING / FUTURE PATCH** — The separate live economics adapter still contains its prior legacy funding request/envelope logic. That file was outside this read-only-verifier commit and does not block the external verifier rerun, but must be reconciled before relying on live-pilot funding accrual accounting.

## Required fields

READ_ONLY_SETTINGS_DECOUPLED=YES

PRODUCTION_SETTINGS_GUARD_UNCHANGED=YES

READ_ONLY_TRANSPORT_ENFORCED=YES

FUNDING_ENDPOINT=GET_/api/v2/mix/account/bill

FUNDING_BUSINESS_TYPE=contract_settle_fee

FUNDING_RESPONSE_ENVELOPE=data.bills

EMPTY_FUNDING_HISTORY_VALID=YES

MIXED_BILLS_EXACT_FUNDING_ONLY=YES

FEE_BILLS_EXCLUDED=YES

REALIZED_PNL_ORDER_BILLS_EXCLUDED=YES

TRANSFER_BILLS_EXCLUDED=YES

LIQUIDATION_BILLS_EXCLUDED=YES

WRONG_BUSINESS_TYPE_BLOCKED=YES_FIXED_EXACT_REQUEST

FUNDING_SCHEMA_FAIL_CLOSED=YES

BALANCE_CLASSIFICATION=YES_UNCHANGED

POSITION_CLASSIFICATION=YES_UNCHANGED

REGULAR_ORDER_CLASSIFICATION=YES_UNCHANGED

PLAN_ORDER_CLASSIFICATION=YES_UNCHANGED

STOP_CLASSIFICATION=YES_UNCHANGED

FILL_CLASSIFICATION=YES_UNCHANGED

FEE_CLASSIFICATION=YES_UNCHANGED

FUNDING_CLASSIFICATION=YES_LOCAL_MOCK_VERIFIED

BITGET_AUTH_READ_VERIFIED=NO_EXPECTED_EXTERNAL_RUNTIME

ENV_LIVE_UNCHANGED=YES_COMMIT_SCOPE_ONLY_NOT_ACCESSED

SECRETS_EXPOSED=NO

ADAPTIVETREND_UNTOUCHED=YES

PRODUCTION_WORKTREE_UNTOUCHED=YES_COMMIT_SCOPE

REAL_ORDER_ARMED=FALSE

REAL_ORDERS_SENT=0

READY_FOR_EXTERNAL_DEPLOYED_RERUN=YES

BLOCKERS=NONE_FOR_EXTERNAL_READ_ONLY_RERUN; AUTHENTICATED_RESULTS_REMAIN_EXTERNAL

FINAL_STATUS=LOCAL_READ_ONLY_FUNDING_CLASSIFIER_GREEN_READY_FOR_EXTERNAL_DEPLOYED_RERUN_NOT_LIVE_LAUNCH_APPROVAL

## Tests and checks

- Ran verifier, deterministic funding core/parity, and configuration-security tests: `62 passed`.
- Independently captured the funding call and verified the exact method, path, `contract_settle_fee`, product type, and limit parameters.
- Independently supplied mixed exact-funding, liquidation, fee/open, and transfer rows: exactly one funding row counted and the other rows excluded.
- Independently supplied an empty bills list: valid zero-count pass.
- Independently supplied a malformed exact funding row: schema false and process exit code 2.
- Inspected branch scope, GET endpoint allowlist, settings, output structure, and unchanged classifier paths.
- Ran bytecode compilation and changed-code diff checks: passed.
- Performed no credentials, `.env`, network, production mutation, or exchange-order action.
