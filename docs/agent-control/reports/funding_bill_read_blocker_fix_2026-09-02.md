# Funding bill read blocker fix

Date: 2026-09-02
Branch: `codex/read-only-auth-verifier`
Base SHA: `4d98c33b00422504751191446398b46e007e68a1`

## Summary

The final authenticated-read failure was caused by querying Bitget V2 futures account bills with the invalid business type `funding_fee`. Bitget's documented funding-fee bill type is `contract_settle_fee`, and the endpoint returns its list under `data.bills`. The verifier now uses that exact GET query, parses the documented envelope, and locally classifies only records whose `businessType` is exactly `contract_settle_fee`.

An empty valid `bills` list passes. Fees, realized-PnL bills, transfers, liquidations, and other business types are excluded from the funding count. Missing funding schema or endpoint failure remains fail-closed. The GET-only verb and endpoint allowlists are unchanged.

## Required report

`FUNDING_ENDPOINT_FIXED=YES`

`FUNDING_CLASSIFICATION=PASS_LOCAL_MOCK_AND_SCHEMA_VERIFIED; DEPLOYED_RERUN_REQUIRED`

`BITGET_AUTH_READ_VERIFIED=NO_PENDING_DEPLOYED_RERUN`

`ALL_OTHER_CLASSIFICATIONS_STILL_PASS=YES_LOCAL_REGRESSION`

`READ_ONLY_TRANSPORT_ENFORCED=YES`

`PRODUCTION_SETTINGS_GUARD_UNCHANGED=YES`

`ENV_LIVE_UNCHANGED=YES_NOT_ACCESSED`

`ADAPTIVETREND_UNTOUCHED=YES`

`REAL_ORDER_ARMED=FALSE`

`REAL_ORDERS_SENT=0`

`READY_FOR_FINAL_OWNER_LAUNCH_DECISION=NO_PENDING_DEPLOYED_AUTHENTICATED_RERUN`

`BLOCKERS=DEPLOYED_AUTHENTICATED_READ_ONLY_RESULT_NOT_YET_OBSERVED_FOR_THIS_FIX`

## Files changed

- `funding_pilot/read_only_verify.py`
- `tests/test_funding_read_only_verify.py`
- this report

No production settings, environment file, frozen strategy, execution arming, AdaptiveTrend, or order transport code changed.

## Tests and checks

- Verifier, funding core/parity, configuration-security, and live launch-guard suites: 66 passed.
- Valid funding response: passed.
- Empty funding history: passed.
- Invalid legacy `funding_fee` business type: rejected by the exchange-semantics harness.
- Mixed bills: only `contract_settle_fee` counted.
- Missing funding schema: failed closed.
- Funding transport request assertion: `GET /api/v2/mix/account/bill`, `businessType=contract_settle_fee`, limit 100.
- `python3 -m compileall -q funding_pilot`: passed.
- `git diff --check`: passed.

## Deployed rerun

```bash
cd ~/cgc/bitget_ai_agent_funding_pilot
set -a
source ~/cgc/bitget_ai_agent_phase7/.env.live
set +a
~/cgc/bitget_ai_agent_phase7/.venv/bin/python -m funding_pilot.read_only_verify
```

Required external result: all eight classifications pass and `BITGET_AUTH_READ_VERIFIED=true`. This report does not authorize live launch or any order operation.
