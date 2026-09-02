# Final funding runtime parity fix

Date: 2026-09-02
Branch: `codex/read-only-auth-verifier`
Base SHA: `064d2b5d4d235febc5b83e9093ead1035e99f193`

## Summary

The verifier and live pilot economics adapter now call one canonical funding-bill service. It owns the exact authenticated GET query (`/api/v2/mix/account/bill`, `businessType=contract_settle_fee`, `limit=100`), parses `data.bills`, accepts empty history, validates the documented funding schema, and returns only exact funding-settlement records.

The live adapter books funding once by `billId`. It excludes fees, realized-PnL bills, transfers, liquidations, and other bill types. Attribution uses exact symbol plus exact position identity when Bitget supplies it; for the documented schema without position identity it requires exact symbol and a bill timestamp within the durable pilot ownership lifecycle. Existing same-symbol conflict guards remain unchanged.

## Required status

`LIVE_FUNDING_QUERY_FIXED=YES`

`LIVE_FUNDING_CLASSIFICATION_PARITY=YES`

`LIVE_FUNDING_DEDUP_VERIFIED=YES`

`CURRENT_PILOT_NAV_AUTHORITATIVE=YES_LOCAL_DETERMINISTIC_CORE`

`DYNAMIC_COMPOUNDING_VERIFIED=YES`

`ECONOMICS_REPLAY_IDEMPOTENT=YES`

`BITGET_AUTH_READ_VERIFIED=NO_PENDING_DEPLOYED_AUTHENTICATED_RERUN`

`PRODUCTION_SETTINGS_GUARD_UNCHANGED=YES`

`ENV_LIVE_UNCHANGED=YES_NOT_ACCESSED`

`ADAPTIVETREND_UNTOUCHED=YES`

`REAL_ORDER_ARMED=FALSE`

`REAL_ORDERS_SENT=0`

`READY_FOR_FINAL_OWNER_LAUNCH_DECISION=NO`

`BLOCKERS=EXACT_PATCH_NOT_DEPLOYED_AND_AUTHENTICATED_READ_ONLY_RERUN_NOT_OBSERVED`

## Files changed

- `funding_pilot/funding_bills.py`
- `funding_pilot/read_only_verify.py`
- `funding_pilot/bitget_exchange.py`
- `tests/test_funding_pilot_real_runtime.py`
- `tests/test_entry_path_audit.py`
- this report

No frozen strategy, production settings, `.env.live`, AdaptiveTrend, execution arming, or order-routing code changed.

## Tests and checks

- Funding verifier, live adapter, economics ledger, NAV/compounding, replay/idempotency, restart recovery, position economics, portfolio, configuration-security, and live launch-guard suites: 203 passed.
- Mixed bills: only exact `contract_settle_fee` records booked.
- Empty funding history: valid.
- Exact `billId` repeated across polls/restart: booked once.
- Official bill rows without `positionId`: older and foreign-symbol funding excluded; owned lifecycle row included.
- `python3 -m compileall -q funding_pilot`: passed.
- `git diff --check`: passed.
- Credentials, environment files, network, and exchange order operations: not accessed.

## Deployed authenticated rerun

After installing the exact independently verified commit on the deployed isolated pilot worktree:

```bash
cd ~/cgc/bitget_ai_agent_funding_pilot
set -a
source ~/cgc/bitget_ai_agent_phase7/.env.live
set +a
~/cgc/bitget_ai_agent_phase7/.venv/bin/python -m funding_pilot.read_only_verify
```

All eight classifications and `BITGET_AUTH_READ_VERIFIED` must pass. This local result does not authorize launch or order transmission.
