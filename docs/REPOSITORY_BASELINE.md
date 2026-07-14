# Repository safety baseline

This baseline records the repository guarantees validated by the cleanup
branch. It describes technical safety only; it does not claim positive trading
expectancy.

## Safe defaults

- `EXECUTION_ENABLED` defaults to `false`.
- `EXECUTION_MODE` defaults to `DRY_RUN`.
- The dashboard binds to `127.0.0.1` by default.
- The dashboard refuses to start without `DASHBOARD_PASSWORD`.
- Application logging redacts common credential fields and bearer tokens.

## Runtime integrity

- JSON state writes use checksums, snapshots, atomic replacement and an
  interprocess file lock.
- Cooldown read-modify-write operations use one atomic state transaction.
- Bitget REST clients in the bot process share one thread-safe request pacing
  budget.
- The current spread note format (`spread_bps=`) is enforced by the existing
  execution-cost risk gate.

## Operational scope

- `scripts/start_bot.sh` and `scripts/stop_all.sh` remain the supported bot
  lifecycle commands.
- Backtests and validation scripts do not enable live execution.
- Position management currently runs at the end of each complete scan cycle;
  it is not an independent high-frequency monitor.

## Deferred work

The following items need a separate design or migration plan and are not part
of this baseline:

- independent position-monitor scheduling;
- interprocess locking/snapshotting for all CSV telemetry readers and writers;
- consolidation of configuration reads that bypass `Settings`;
- removal of legacy agent and dashboard modules that may still have external
  manual users;
- strategy, entry, exit, TP, SL, break-even or sizing changes.
