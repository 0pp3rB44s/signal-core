# Funding pilot deployed read-only auth integration

Date: 2026-09-01
Branch: `codex/read-only-auth-verifier`
Base SHA: `9425e678fed009db8d47fb49461b318722a25b77`

## Summary

The authenticated verifier no longer constructs production `app.config.Settings`. It uses a minimal credential-only environment model and a Bitget client whose final transport boundary permits only `GET` plus seven explicit read endpoints. Production settings and launch validation code are unchanged. No credential or production environment file was read, printed, copied, or modified during implementation.

## Required report

`READ_ONLY_SETTINGS_DECOUPLED=YES`

`PRODUCTION_SETTINGS_GUARD_UNCHANGED=YES`

`READ_ONLY_TRANSPORT_ENFORCED=YES`

`ENV_LIVE_UNCHANGED=YES`

`BITGET_AUTH_READ_VERIFIED=NO_NOT_RUN_IN_DEPLOYED_SECURE_RUNTIME`

`BALANCE_CLASSIFICATION=NOT_RUN_EXTERNAL_RUNTIME`

`POSITION_CLASSIFICATION=NOT_RUN_EXTERNAL_RUNTIME`

`REGULAR_ORDER_CLASSIFICATION=NOT_RUN_EXTERNAL_RUNTIME`

`PLAN_ORDER_CLASSIFICATION=NOT_RUN_EXTERNAL_RUNTIME`

`STOP_CLASSIFICATION=NOT_RUN_EXTERNAL_RUNTIME`

`FILL_CLASSIFICATION=NOT_RUN_EXTERNAL_RUNTIME`

`FEE_CLASSIFICATION=NOT_RUN_EXTERNAL_RUNTIME`

`FUNDING_CLASSIFICATION=NOT_RUN_EXTERNAL_RUNTIME`

`SECRETS_EXPOSED=NO`

`ADAPTIVETREND_UNTOUCHED=YES`

`REAL_ORDER_ARMED=FALSE`

`REAL_ORDERS_SENT=0`

`READY_FOR_FINAL_RUNTIME_AUDIT=YES`

`BLOCKERS=AUTHENTICATED_CLASSIFICATION_MUST_RUN_ON_DEPLOYED_SECURE_RUNTIME`

## Files changed

- `funding_pilot/read_only_verify.py`
- `tests/test_funding_read_only_verify.py`
- this report

No production configuration, AdaptiveTrend, `.env.live`, execution arming, order routing, leverage, sizing, or strategy admission file changed.

## Tests and checks

- Read-only verifier, funding runtime/parity, production settings security, and live launch-guard tests: 58 passed.
- `python3 -m compileall -q funding_pilot`: passed.
- `git diff --check`: passed.
- Mutation verbs and a GET request to a mutation endpoint are rejected before network transport.
- Missing credentials fail validation before client construction.
- Synthetic secret sentinels do not appear in verifier output.

## Deployed command

Run only on the deployed host:

```bash
cd ~/cgc/bitget_ai_agent_funding_pilot
set -a
source ~/cgc/bitget_ai_agent_phase7/.env.live
set +a
~/cgc/bitget_ai_agent_phase7/.venv/bin/python -m funding_pilot.read_only_verify
```

The command performs authenticated reads only and prints classification metadata, record counts, schema-presence flags, and pass/fail status. It does not print records, balances, account identifiers, or credentials.

## Remaining concerns

Authenticated endpoint reachability and live response schemas remain unknown until the command runs in the secure deployed runtime. This implementation and its local tests do not constitute live-launch approval.
