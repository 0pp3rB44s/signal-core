# Funding Pilot Final Isolation — Builder Report

## Summary

Removed symbol-only ownership, added exact exchange-position-ID ownership,
same-symbol conflict rejection, exact pilot-stop cancellation, orphan-stop
cleanup verification, and the production frozen funding-crowding poller derived
from the original research formula. Production remains hard-disarmed.

Builder status: **READY FOR INDEPENDENT VERIFICATION**, not self-certified.

## Files changed

- `funding_pilot/bitget_exchange.py`
- `funding_pilot/canonical.py`
- `funding_pilot/runner.py`
- `funding_pilot/signals.py`
- `execution/position_manager.py`
- `tests/test_funding_pilot_real_runtime.py`
- `tests/test_funding_pilot_canonical.py`
- `tests/test_entry_path_audit.py`
- this report

## Risk impact

- A pilot position requires both a persisted `cgc-fcp-*` entry identity and an
  exact matching exchange position ID. Missing or mismatched identity fails
  closed without mutation.
- Any same-symbol position seen after a pilot entry intent but before exact
  exchange identity capture produces `SKIP_SIGNAL_SHARED_EXPOSURE_CONFLICT`.
- Closed economics are ingested only when history carries the exact persisted
  exchange position ID. Same-symbol foreign history is ignored.
- Time exit verifies exact position identity before close and cancels only the
  persisted pilot stop ID. It no longer calls symbol-wide TPSL cancellation.
- Stop mismatch cleanup cancels only adapter-classified pilot orders/stops and
  verifies no pilot position, working order or orphan stop remains.
- The production poller verifies the frozen hash each cycle and implements the
  original 90-observation funding percentile, three-funding-interval 24h return,
  lagged 30-event volatility, ±1.5 extension, continuation direction, current
  eligibility and deterministic simultaneous ranking.

## Tests/checks run

- 121 focused production-runtime, adapter, ownership, entry, recovery and runner
  tests passed.
- Explicit same-symbol collision and foreign-history contamination tests passed.
- Production module compilation and `git diff --check` passed.
- Frozen SHA256 remains
  `cda7ecb21e6fd089ef98abb8047d409285825a37d6a2e87510d73cf653ea2e13`.

## Authenticated read-only verification

The secure runtime was checked without reading, printing, copying or exporting
credentials. It reported `CREDENTIALS_AVAILABLE=False`. Therefore authenticated
Bitget reads remain an **external blocker** and are not represented as verified.

## Remaining concerns

- Independent verification is mandatory.
- `BITGET_AUTH_READ_VERIFIED` cannot become YES until the authorized runtime
  provides credentials to this isolated process.
- No real order was sent; `REAL_ORDER_ARMED=FALSE` and `REAL_ORDERS_SENT=0`.
