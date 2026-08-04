# Release A: close flatness, economics and recovery

Release A changes only the safety/data-integrity boundary around closing a
position. It does not change strategy, risk, leverage, sizing, margin, entry
routing, or TP/SL parameters.

## Runtime contract

- A fresh Bitget position read returns `FLAT`, `REMAINS`, or `UNKNOWN`.
- Only `FLAT` with remaining size exactly zero permits local close state,
  protection cleanup, or close-economics recording.
- A close order acknowledgement is never proof of flatness.
- Exchange history is accepted only when gross PnL, both fees, funding,
  netProfit, identity, times, size and prices are present and consistent.
- `netProfit` is stored literally; it is never passed through the generic
  gross-minus-fees serializer.
- A confirmed flat lifecycle is first visible as `CLOSE_PROVISIONAL`. It is
  replaced economically only after an unambiguous exchange-history match.

Startup recovery gates the scanner's only call into `ExecutionService.execute`.
PositionManager also runs a bounded periodic oldest-unresolved-first sweep.
Resolved provisional rows are filtered before the limit; every numeric CSV
rotation is included.

## Migration audit

`scripts/audit_provisional_close_migration.py` performs GET-only exchange
history reads and is read-only by default:

```bash
python3 scripts/audit_provisional_close_migration.py
```

It reports safe matches, ambiguous/missing matches, existing economic rows,
and before/expected-after totals. Local dataset mutation requires the separate
`--apply` flag. Release A does not run the script automatically and does not
migrate existing rows.

The owner-triggered emergency entrypoint is `scripts/emergency_flatten.py`.
It routes through `PositionManager.emergency_flatten_all()` so lifecycle
identities are recorded and reconciled. It performs no action unless invoked
with the exact `--confirm-emergency-flatten` flag; it is not run automatically.

## Rollback

Rollback is a normal revert of the Release A commits followed by the project's
regular owner-reviewed deployment process. Do not delete provisional rows:
they are audit evidence and are intentionally non-economic. Rolling back does
not require an exchange order, leverage change, or TP/SL change.
