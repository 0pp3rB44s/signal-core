# Repository safety baseline

This baseline records the repository guarantees validated by the cleanup
branch. It describes technical safety only; it does not claim positive trading
expectancy.

## Safe defaults

- `EXECUTION_ENABLED` defaults to `false`.
- `EXECUTION_MODE` defaults to `DRY_RUN`.
- The dashboard binds to `127.0.0.1` by default.
- The dashboard refuses to start without `DASHBOARD_PASSWORD`.
- Application logging redacts credential fields and bearer tokens across JSON,
  dictionaries, nested structures, key/value text and HTTP headers.
- Private exchange response bodies are never copied wholesale into logs or
  exceptions; only status, endpoint, error code and a bounded redacted message
  are retained.

## Runtime integrity

- JSON state writes use checksums, snapshots, atomic replacement and an
  interprocess file lock.
- Cooldown read-modify-write operations use one atomic state transaction.
- Bitget REST clients in bot and dashboard processes share one interprocess
  request pacing budget. Corrupt state fails closed and stale OS locks recover
  automatically when their owner exits.
- CSV telemetry, learning, dataset and dashboard access uses one shared
  interprocess lock layer. Header creation is serialized and JSON report writes
  use atomic replacement.
- The current spread note format (`spread_bps=`) is enforced by the existing
  execution-cost risk gate.

## Operational scope

- `scripts/start_bot.sh` and `scripts/stop_all.sh` remain the supported bot
  lifecycle commands.
- Backtests and validation scripts do not enable live execution.
- Position management runs in an independent serialized monitor loop. Every
  monitor cycle reconciles against current exchange mark prices; stale scan
  prices and candle ranges are not used for TP, SL, profit-lock, MFE or MAE.
- Scan-context continuation decisions run only with a fresh completed scan.
  Execution state updates and position sync share one process/interprocess lock,
  and no more than one position sync can run concurrently.
- Runtime configuration is centralized in typed `Settings`; legacy environment
  names remain supported and credentials use redacted secret types.
- The break-even fee buffer changed from `0.10%` to `0.12%` solely as a
  transaction-cost coverage fix. Entry conditions, initial SL distance,
  risk-per-trade percentage, leverage and position sizing were not increased.

## Legacy disposition

See `docs/LEGACY_MODULES.md` for the import, script and manual-workflow map.
Only the confirmed empty and unreferenced `telemetry/event_logger.py` placeholder
was removed. Ambiguous/manual modules remain in place.

No further entry, exit, TP, SL, break-even or sizing optimization is included.
Such work requires a reproducible baseline, backtest and forward-paper
comparison.
