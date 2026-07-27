# 08 — Monitoring, Heartbeat, Supervisor, Logging & Alerting

## Heartbeat

`state/runtime_heartbeat.json`, rewritten each scan stage.

```json
{"stage": "scan_cycle_complete",
 "scan_cycles_started": 1177, "scan_cycles_completed": 1177,
 "details": {"snapshot_count": 1, "plan_count": 0, "executable_plan_count": 0}}
```

| Field | Meaning |
|---|---|
| `stage` | `scan_cycle_complete`, `scan_cycle_failed`, `scan_cycle_incomplete`, `candle_request`, `candle_response`, `symbol_scan_start` |
| `snapshot_count` | **most diagnostic field.** A completed cycle with `0` means every symbol failed |
| `consecutive_scan_failures` | drives `DEGRADED` / `SCAN_LOOP_FAILING` |
| `timestamp` | freshness — **evaluated by no live consumer** (R4) |

## Health checks

`scripts/check_forward_paper.sh` → `status=` on stdout plus a typed exit code.

| Condition | Status | Exit |
|---|---|---|
| Normal | `HEALTHY` | 0 |
| Process absent | `PROCESS_NOT_RUNNING` | non-zero |
| Private/exchange call detected in paper mode | `PRIVATE_CALL_DETECTED` | 6 |
| Paper writer failing | `FORWARD_PAPER_WRITE_FAILING` | 7 |
| Executable plans, no paper output | `FORWARD_PAPER_OUTPUT_MISSING` | 9 |
| Completed cycle, `snapshot_count=0` | `SCAN_PRODUCED_NO_MARKET_DATA` | 10 |
| ≥3 consecutive scan failures | `SCAN_LOOP_FAILING` | 11 |
| ≥1 consecutive scan failure | `DEGRADED` | 12 |

All three scan verdicts were verified live against injected heartbeats; the real
heartbeat still returned `HEALTHY`.

## Supervisor

`scripts/forward_paper_keepalive.sh` — health-check driven, not merely process-alive.

- Restarts **only** via `scripts/start_forward_paper.sh`.
- Restarts only on `PROCESS_NOT_RUNNING` / `NOT_STARTED`.
- **Fail-closes** on `DEGRADED`, `SCAN_PRODUCED_NO_MARKET_DATA`, `SCAN_LOOP_FAILING` —
  deliberately, because a restart cannot fix a code defect and would mask it.
- Gives up after **3 restarts in 1800 s** (`state/forward_paper_keepalive.history`).
- Stop flag: `state/forward_paper_keepalive.stop`.

Measured restart latency: 6 s / 2 s foreground; **91 s / 121 s detached**.

## Logging

| Log | Contents |
|---|---|
| `logs/forward_paper.out` | main runtime log (27 965 lines over 42.75 h) |
| `logs/forward_paper_keepalive.log` | supervisor events only |
| `logs/runtime.log` | start/stop audit line per run |
| `data_store/funnel_events.jsonl` | per-decision telemetry, hash-chained |

Markers worth alerting on: `SCAN_CYCLE_FAILED`, `SCAN_CYCLE_RECOVERED`,
`FORWARD_PAPER_FAILED_CLOSED`, `BITGET_HTTP_ERROR`, `400171`,
`BITGET_DNS_RESOLUTION_FAILURE`, `RUNTIME_SIGNAL_RECEIVED`,
`FORWARD_PAPER_UNSERIALIZABLE_VALUE`, `PLANNER_NOTIONAL_CAPPED`.

## Reference baselines (RC1, 42.75 h)

| Metric | Observed |
|---|---|
| Cycle cadence | median 63 s, p95 63 s (60 s configured) |
| Scan duration | ≈ 3 s |
| API latency | median 317.2 ms, p95 344.1 ms, max 1 313.0 ms, σ 33.8 |
| API success | 9 892 / 9 892 = 100 % HTTP 200 |
| Errors | 2 ERROR (one DNS incident), 44 WARNING, 0 tracebacks |

Deviation from these is the practical alert threshold.

## Alerting — required, largely absent

| Requirement | Status |
|---|---|
| Heartbeat freshness alert | **MISSING** (R4) |
| Monitor self-liveness | **MISSING** (R3) |
| Off-host delivery | **MISSING** — everything is local |
| Process-death alert | **MISSING** — check exists, nothing calls it when dead |
| Scan-failure alert | partial — status exists, no delivery |
| Chain-invalid alert | partial — detected, no delivery |
| Unresolved-position alert | partial — counted, no delivery |
| Archiver throughput | **MISSING**, no threshold defined (R12) |

## Blind spots proven by the campaign

- **B1** Nothing monitors the monitor — it stopped at 2026-07-26T12:09:03Z; 21 snapshots
  where ~50 were due; no error, no alert.
- **B2** A 22.19 h suspension was invisible — no log line, no health failure.
- **B3** 7.82 h of process death went unreported.
- **B4** Archiver throughput has no threshold.
- **B5** CPU and memory are never sampled — evidence not available.

**Every monitoring component here is pull-based, local, and can die unobserved.**
Absence of alerts is therefore not evidence of health.
