# RUN INVALIDATED

**Status: INVALID — this run produces no reliability evidence.**

- Clock started: 2026-07-25T14:02:10Z
- Stopped:       2026-07-25T15:53Z (~1h51m elapsed of 72h)
- Baseline commit: `221efa0`

## Why

Every scan failed for the entire run. The confirmation timeframe was sent to Bitget
as `granularity=1h`; Bitget only accepts `1H` and returned **HTTP 400 code 400171
on every request — 742 occurrences**.

The process stayed alive and the health check reported `HEALTHY` throughout. The
archived heartbeat shows the contradiction:

```json
"stage": "scan_cycle_complete",
"scan_cycles_started": 106,
"scan_cycles_completed": 106,
"details": {"snapshot_count": 0, "plan_count": 0, "executable_plan_count": 0}
```

106 cycles reported **completed** having built **zero** snapshots. Both hourly
monitor snapshots recorded `health=HEALTHY`. Zero market data was ever fetched, so
no scan, signal, candidate, plan or trade evidence from this window is usable.

## Preserved here

`manifest.json`, `snapshots/` (4), `forward_paper.out` (742 × 400171),
`keepalive.log`, `monitor.log`, `runtime_heartbeat.json`,
`forward_paper_events.jsonl` (0 events — nothing was ever written).

## Two defects, both fixed before the next run

1. **Wrong value at the API boundary.** `get_candles` passed `granularity` straight
   through. `get_multi_timeframe_candles` held a private partial map that knew
   `1h -> 1H` for two timeframes only, so every other path sent the raw lowercase
   value. Fixed in `clients/bitget_market_client.py:api_granularity()`, now the
   single canonical boundary; all 18 aliases verified against the live API.
2. **Observability could not tell.** A cycle whose per-symbol handler swallowed every
   failure still published `scan_cycle_complete`. Fixed: zero snapshots from a
   non-empty symbol list now raises `ScanCycleProducedNoMarketData`, which the
   existing resilience wrapper counts and publishes as `scan_cycle_failed`; the
   health check now reports `SCAN_PRODUCED_NO_MARKET_DATA`, `SCAN_LOOP_FAILING` or
   `DEGRADED` instead of `HEALTHY`.

Defect 1 alone would have wasted 72 hours. Defect 2 is why nobody would have noticed.
