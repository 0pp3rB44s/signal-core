# Funding Pilot Release Defect Closure — Builder Report

## Summary

Closed the scoped lifecycle and economics defects and aligned production
turnover with the research `usdtVolume` source. Added a deployed-runtime-only
authenticated read verifier. Production remains hard-disarmed.

## Files changed

- `funding_pilot/bitget_exchange.py`
- `funding_pilot/canonical.py`
- `funding_pilot/signals.py`
- `funding_pilot/read_only_verify.py`
- `tests/test_funding_pilot_real_runtime.py`
- `tests/test_entry_path_audit.py`
- this report

## Risk impact

- Every entry intent now terminates as FAILED, REJECTED, OPEN or CLOSED under
  its unique `cgc-fcp-*` identity. Rejected intents no longer remain active.
- Time-exit terminal events carry the exact originating entry client OID.
- Opening fees, closing fees, realized PnL and funding bills use separate stable
  economic item keys. Closed-history opening fees overlap safely with entry-time
  fees; closed `totalFunding` is not re-ingested after bill ingestion.
- Restart replay is idempotent through persistent item-key markers.
- Same-symbol ownership and pilot-stop-only protections remain intact.

## Tests/checks run

- 214 selected tests passed, including direct research-formula parity,
  lifecycle termination, time-exit identity, duplicate economics and restart
  replay.
- Production compilation and `git diff --check` passed.
- Frozen SHA256 remains
  `cda7ecb21e6fd089ef98abb8047d409285825a37d6a2e87510d73cf653ea2e13`.

## Authenticated verification handoff

Run this exact read-only component inside the authorized deployed environment
where credentials are injected securely:

`python3 -m funding_pilot.read_only_verify`

It performs only authenticated GET requests, prints counts/status—not secrets
or balances—and creates its temporary empty ownership ledger outside the repo.
The current Codex process has no credentials, so
`BITGET_AUTH_READ_VERIFIED=NO` remains an external blocker here.

## Remaining concerns

- Independent verification is mandatory.
- No real orders were sent: `REAL_ORDER_ARMED=FALSE`, `REAL_ORDERS_SENT=0`.
