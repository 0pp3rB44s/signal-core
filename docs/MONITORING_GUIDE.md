# Monitoring guide — signals, thresholds and known blind spots

What is observable today, what the signals mean, and — importantly — what the
Forward-Paper campaign proved is *not* observable. Evidence in `validation_72h/archive/`.

Companion documents: `docs/RECOVERY_PROCEDURES.md`, `docs/RISK_REGISTER.md`.

---

## Signal sources

| Source | Path | Written by | Cadence |
|---|---|---|---|
| Runtime heartbeat | `state/runtime_heartbeat.json` | bot | every scan stage |
| Shutdown record | `state/last_shutdown.json` | bot signal handler | on exit |
| Runtime state | `state/forward_paper_runtime.state` | strict launcher | on start |
| Health check | `scripts/check_forward_paper.sh` | on demand | on demand |
| Supervisor log | `logs/forward_paper_keepalive.log` | keepalive | on event only |
| Observation snapshots | `validation_72h/snapshots/*.json` | `validation_72h/monitor.sh` | hourly |
| Paper event store | `data_store/forward_paper_events.jsonl` | forward-paper service | per lifecycle event |
| Data quality | `reports/forward_paper_data_quality.json` | reconstructor | per reconstruction |
| Funnel telemetry | `data_store/funnel_events.jsonl` | funnel telemetry | per decision |

## Heartbeat fields that matter

```json
{"stage": "scan_cycle_complete",
 "scan_cycles_started": 1177, "scan_cycles_completed": 1177,
 "details": {"snapshot_count": 1, "plan_count": 0, "executable_plan_count": 0}}
```

- `stage` — `scan_cycle_complete`, `scan_cycle_failed`, `scan_cycle_incomplete`,
  `candle_request`, `candle_response`, `symbol_scan_start`.
- `snapshot_count` — **the single most diagnostic field.** A completed cycle with
  `snapshot_count: 0` means every symbol failed. This exact signature ran for 106
  cycles in run 1 while the health check reported HEALTHY.
- `consecutive_scan_failures` — drives `DEGRADED` / `SCAN_LOOP_FAILING`.
- `timestamp` — freshness. **Not evaluated by any live consumer** (risk R4).

## Health-check verdicts

| Condition | Status | Exit |
|---|---|---|
| Normal | `HEALTHY` | 0 |
| Process absent | `PROCESS_NOT_RUNNING` | non-zero |
| Paper writer failing | `FORWARD_PAPER_WRITE_FAILING` | 7 |
| Executable plans, no paper output | `FORWARD_PAPER_OUTPUT_MISSING` | 9 |
| Completed cycle, `snapshot_count=0` | `SCAN_PRODUCED_NO_MARKET_DATA` | 10 |
| ≥3 consecutive scan failures | `SCAN_LOOP_FAILING` | 11 |
| ≥1 consecutive scan failure | `DEGRADED` | 12 |

Verified live against injected heartbeats: all three scan verdicts fire correctly and
the real heartbeat still returns `HEALTHY`.

## Log markers worth alerting on

| Marker | Meaning |
|---|---|
| `SCAN_CYCLE_FAILED` | a cycle raised; consecutive count published |
| `SCAN_CYCLE_RECOVERED` | loop recovered after failures |
| `FORWARD_PAPER_FAILED_CLOSED` | paper write aborted — the 2026-07 silent failure |
| `BITGET_HTTP_ERROR` / `400171` | rejected request; `400171` = bad granularity |
| `BITGET_DNS_RESOLUTION_FAILURE` / `BITGET_NETWORK_RETRY` | transient network, retried |
| `RUNTIME_SIGNAL_RECEIVED` | process received a termination signal |
| `FORWARD_PAPER_UNSERIALIZABLE_VALUE` | a producer put a non-JSON object in a payload |

## Reference baselines (run 2, 2026-07-25 → 27)

| Metric | Observed |
|---|---|
| Cycle cadence | median 63 s, p95 63 s (60 s configured sleep) |
| Derived scan duration | ≈ 3 s |
| API latency | median 317.2 ms, p95 344.1 ms, max 1 313.0 ms, σ 33.8 ms |
| API success rate | 9 892 / 9 892 = 100 % HTTP 200 |
| Errors in 42.75 h | 2 ERROR (one DNS incident), 44 WARNING, 0 tracebacks |
| Disk | 31 Gi free / 85 % used |

Deviations from these baselines are the practical alerting thresholds.

---

## Blind spots proven by the campaign

These are **evidenced failures of observability**, not hypotheticals.

### B1 — Nothing monitors the monitor (risk R3)

The hourly observation loop stopped at `2026-07-26T12:09:03Z` and never resumed. 21
snapshots exist where ~50 were due. It logged no error. Nothing noticed.

**Consequence: absence of alerts is not evidence of health.** Any operator reading
"no alerts" during that window would have concluded the run was fine.

### B2 — A 22-hour process suspension is invisible (risk R2)

Host sleep froze the process for 22.19 h. No log line, no alert, no health failure. The
heartbeat simply stopped advancing — and nothing evaluates heartbeat age.

### B3 — Process death goes unreported (risks R1, R4)

After the host reboot the bot was dead for 7.82 h. The health check *would* have
returned `PROCESS_NOT_RUNNING`, but nothing was running to call it.

### B4 — Archiver throughput has no threshold (risk R12)

Pre-flight gate case G was never implemented. A silent archive degradation would not be
flagged.

### B5 — CPU and memory are never sampled

No time series exists for the run. After the reboot the process table was lost.
**Evidence not available** for any resource-exhaustion analysis.

---

## Practical implication

Every monitoring component in this stack is **pull-based and local**. Each depends on a
process that can itself die silently, and none escalates off-host. The campaign
demonstrated all three of the resulting failure modes within 43 hours.
