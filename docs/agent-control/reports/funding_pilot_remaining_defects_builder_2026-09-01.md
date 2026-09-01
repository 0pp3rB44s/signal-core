# Funding Pilot Remaining Defects — Builder Report

## Summary

Closed the verifier-identified code defects: chronological lifecycle identity,
complete same-symbol order conflict coverage, open-fee and accrued-funding NAV,
and direct frozen event-formula parity. Production remains hard-disarmed.

Builder status: **READY FOR INDEPENDENT VERIFICATION**.

## Files changed

- `funding_pilot/bitget_exchange.py`
- `funding_pilot/canonical.py`
- `funding_pilot/signals.py`
- `tests/test_funding_pilot_real_runtime.py`
- `tests/test_funding_signal_parity.py`
- `tests/test_entry_path_audit.py`
- this report

## Risk impact

- Ownership events are replayed in durable event-ID order and keyed by pilot
  entry client OID. Multiple active pilot lifecycles for one symbol fail closed.
- Foreign same-symbol entry, reduce-only, native-stop, conditional/plan and
  multiple-order combinations all hard-skip without mutation.
- Closed economics require exact position ID. Opening fees are persisted at
  protected entry; accrued funding bills require the same exact position ID and
  update current NAV before close.
- The production event decision uses a shared pure implementation matching the
  original pandas formula, including average tie ranks, sample volatility and
  lagging. Eligibility turnover is now trailing 24h quote turnover. Events older
  than five minutes are operationally stale and cannot trigger an entry.

## Tests/checks run

- 212 selected tests passed, including TP/SL/BE, recovery, runner, concrete
  adapter, ownership collisions, open economics and formula parity.
- Production module compilation and `git diff --check` passed.
- Frozen specification SHA256 remains
  `cda7ecb21e6fd089ef98abb8047d409285825a37d6a2e87510d73cf653ea2e13`.

## Authenticated Bitget read

Secure runtime availability was checked without reading or exposing secrets:
`BITGET_AUTH_RUNTIME_AVAILABLE=False`. Therefore authenticated read verification
remains an external-runtime blocker and is not claimed as successful.

## Remaining concerns

- Independent verification is mandatory.
- Authenticated read-only validation must run in the authorized deployed runtime
  where credentials are injected securely.
- `REAL_ORDER_ARMED=FALSE`; `REAL_ORDERS_SENT=0`.
