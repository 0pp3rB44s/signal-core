# 01 — System Overview & Architecture

## What the system is

A single-process crypto-futures trading system for Bitget USDT perpetuals. One Python
process scans a watchlist on a fixed interval, runs detectors, scores and risk-gates
candidates, produces trade plans, and then either records them to a simulated
forward-paper ledger or (when explicitly enabled) submits them to the exchange.

It is **not** a distributed system. There is one bot process, one supervisor shell
loop, one optional observation loop and one independent archiver process.

## Operating modes

| Mode | `FORWARD_PAPER_ONLY` | `EXECUTION_ENABLED` | `EXECUTION_MODE` | Orders |
|---|---|---|---|---|
| Strict forward paper | `true` | forced `false` | forced `DRY_RUN` | impossible |
| Dry run | `false` | `false` | `DRY_RUN` | none |
| Live | `false` | `true` | `LIVE` | **real** |

`Settings.enforce_forward_paper_only()` coerces the safe values: setting
`FORWARD_PAPER_ONLY=true` forces `execution_enabled=False` regardless of what was
requested. Verified exhaustively — 16/16 configuration combinations, 0 violations.

Current state: `EXECUTION_ENABLED=false` (owner decision, 2026-07-13).

## Execution path — market data to recorded outcome

| Stage | Implementation |
|---|---|
| Entry point | `app/main.py` → `app/runner.py` (`StartupRunner.run`) |
| Scan loop | `_scan_cycle_iteration()` → `_scan_cycle()`, every `SCAN_INTERVAL_SEC` |
| Market data | `data/market_fetcher.py`, `clients/bitget_*_client.py` |
| Timeframe normalisation | `clients/bitget_market_client.py:api_granularity()` — **the only boundary** |
| Features | `market_features/engine.py:build_market_snapshot()` |
| Detectors | `strategies/` (`low_vol_reclaim`, `momentum_breakout/breakdown`, `continuation`, `liquidity_sweep`) |
| Selection & scoring | selector in `app/runner.py`, `strategies/scoring.py` |
| Risk | `risk/risk_manager.py`, `risk/cooldown_manager.py` |
| Planning | `planning/trade_planner.py` |
| Forward paper | `forward_paper/service.py`, `forward_paper/store.py` |
| Live execution | `execution/execution_service.py` — **skipped entirely when `forward_paper_only`** |
| Position management | `execution/position_manager.py`, `execution/tp_sl_lifecycle.py` |
| Telemetry | `telemetry/funnel.py`, `app/runtime_diagnostics.py` |

In strict forward-paper mode `execution_service` and `position_manager` are `None`
(`app/runner.py`), so no order-placing object exists in the process at all.

## Data flow

```
watchlist → fetch candles/orderbook (public REST)
          → MarketSnapshot (features, context)
          → detectors → candidates
          → selector → scorer
          → RiskManager.evaluate
          → TradePlanner.build → TradePlan(verdict=EXECUTABLE|BLOCKED)
          ├─ forward paper: ForwardPaperService.process → hash-chained JSONL → outcomes CSV
          └─ live:          ExecutionService.execute    → exchange orders → journal
```

Every stage emits a funnel event (`data_store/funnel_events.jsonl`) carrying a stable
`candidate_id`, so a decision can be traced end-to-end.

## Persistence

| Store | Path | Properties |
|---|---|---|
| Forward-paper events | `data_store/forward_paper_events.jsonl` | append-only, hash-chained, sequence-checked, semantic-deduped |
| Forward-paper outcomes | `data_store/forward_paper_outcomes.csv` | reconstructed from events, never written directly |
| Funnel telemetry | `data_store/funnel_events.jsonl` | append-only, hash-chained |
| Runtime heartbeat | `state/runtime_heartbeat.json` | last stage, cycle counters, details |
| Shutdown record | `state/last_shutdown.json` | reason, exit code, signal |
| Live trade journal | `state/live_trade_journal.json` | analytics only — never gates a trade |
| Executed trades / position events | `state/executed_trades.json`, `state/position_events.json` | live-mode state |

Position truth for gating is read from the **exchange**, not from the journal.

## Concurrency and locking

Single process. `state/scan_cycle.lock` (flock) prevents overlapping scans;
`trading_state_lock()` guards execution and position sync. Per-file locks guard state
writes. At the last audit freeze: 10 lock files present, **0 holders** — no stale locks.

## What is proven and what is not

Proven in forward paper (see [12_VALIDATION_SUMMARY.md](12_VALIDATION_SUMMARY.md)):
the full simulated lifecycle, event integrity through a 22 h suspension and a SIGTERM,
exact accounting, and recovery from a real DNS outage.

**Not proven:** anything downstream of `ExecutionService.execute()`. That code path has
never run against the exchange.
