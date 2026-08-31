# Funding-crowding pilot component

This package implements frozen-spec verification, an isolated persistent pilot
ledger, dynamic 10%/20% NAV sizing, shared-margin reservation, minimum-notional
skip behavior, exchange-truth reconciliation, duplicate/orphan-stop detection,
restart recovery, a latched 5% kill switch, and restart-safe telemetry.

It deliberately contains no live Bitget adapter. `orders_enabled=False` is the
only verified mode. The repository's canonical `ExecutionService` requires a
take-profit for every live entry, while the frozen pilot has a 24-hour time exit
and no take-profit. A direct Bitget adapter would bypass `RiskManager` and
`PositionManager`, which project governance forbids.

Therefore this is a no-order deployment artifact core, not a launchable real-
money artifact. Real orders remain prohibited until the canonical execution and
position-ownership path can support stop-only plus time-exit semantics without
changing the frozen alpha specification or AdaptiveTrend behavior.
