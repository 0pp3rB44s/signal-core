# Funding Pilot Deterministic Core — Builder Report

## Summary

Implemented atomic economic ingestion, partial-close identities, post-execution
terminal states, delayed-close economic checkpoints, and deeper frozen-poller
history/audit output. Production remains disarmed.

## Files changed

- `funding_pilot/core.py`
- `funding_pilot/bitget_exchange.py`
- `funding_pilot/canonical.py`
- `funding_pilot/signals.py`
- `execution/position_manager.py`
- `tests/test_funding_pilot_real_runtime.py`
- `tests/test_funding_pilot_canonical.py`
- `tests/test_entry_path_audit.py`
- this report

## Risk impact

- Economic source ID and event insertion now commit in one SQLite transaction.
- Opening fee, closing fee, funding bill and realized-PnL records have distinct
  stable identities; partial closes use close order/trade identity rather than
  position ID alone.
- Missing protection, missing opening fee, reconciliation exceptions and stop
  recovery failures explicitly terminalize their entry OID.
- A time exit remains `EXIT_PENDING` until delayed realized PnL/closing fee
  history is ingested; only then does the exact entry OID become CLOSED.
- Poller candle history now covers all 90 eight-hour funding observations and
  emits audit state for universe/ranking/stale/selection parity checks.

## Tests/checks run

- 214 selected tests passed.
- Compilation, `git diff --check`, and frozen SHA256 verification passed.
- Atomic replay, overlapping open/closed fees, funding replay, rejected intent,
  exact time-exit identity and integrated adapter lifecycle tests passed.

## External runtime handoff

- Authenticated endpoint verification: `python3 -m funding_pilot.read_only_verify`
- Adapter classification and deterministic core tests:
  `python3 -m pytest -q tests/test_funding_pilot_real_runtime.py tests/test_funding_signal_parity.py`

Run both only inside the authorized Runner environment with secure credential
injection. No credentials are printed or copied.

## Remaining concerns

- Independent verification is mandatory.
- Authenticated reads remain external to this Codex runtime.
- `REAL_ORDER_ARMED=FALSE`; `REAL_ORDERS_SENT=0`.
