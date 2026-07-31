# Forward-Paper Validation — campaign history and findings

Permanent record of the Forward-Paper validation campaign of 2026-07-25 → 2026-07-27.
This document is historical: it is appended to, never rewritten. Raw evidence lives in
`validation_72h/archive/` (checksummed bundle); the full audit is
`reports/CLOSEOUT_REPORT.md` inside that bundle.

The campaign measured **operational reliability only**. It is not an edge test and
makes no claim about expectancy. See `docs/RESEARCH_JOURNAL.md` for the edge position.

---

## 1. What preceded the campaign

Three defects blocked any meaningful forward-paper run. All three were found by
inspection of live evidence, not by tests.

| # | Defect | Evidence | Fix |
|---|---|---|---|
| D1 | `ContractSpec` not JSON-serialisable aborted **every** paper write | `forward_paper_events.jsonl` 0 bytes for 11 days while `funnel_events.jsonl` grew to 75 MB; 147 executable plans, 0 trades; 297 × `FORWARD_PAPER_FAILED_CLOSED` | producer-level `instrument_context()` + `store.jsonable()` + logged `json_safe` fallback |
| D2 | Scan loop had **no exception handling**; a DNS blip killed the process | 20 h undetected outage on 2026-07-24 | `_scan_cycle_iteration()` wrapper with consecutive-failure counter |
| D3 | Break-even stop could book a profit at a price the market never traded | `stop moved to 74.004931 while candle high was 73.947`; `TRADE_CLOSED exit_price=74.004931 reason=STOP_LOSS gross_pnl=+0.025` | `_protective_stop()` returns the stop only while it is still on the protective side of the mark |

The paper lifecycle itself (open → manage → SL/TP → close → outcome → restart
recovery) was demonstrated end-to-end before the campaign began.

---

## 2. Run 1 — INVALIDATED

| | |
|---|---|
| Clock | 2026-07-25T14:02:10Z → stopped 15:53Z (1 h 51 m of 72 h) |
| Baseline | `221efa0` |
| Verdict | **INVALID — no usable reliability evidence** |

**Cause.** The confirmation timeframe reached Bitget as `granularity=1h`. Only `1H` is
accepted; the API returned **HTTP 400 code 400171, 742 times**. Zero market data was
fetched for the entire run.

**Why it was not noticed.** The heartbeat reported success throughout:

```json
"stage": "scan_cycle_complete",
"scan_cycles_completed": 106,
"details": {"snapshot_count": 0, "plan_count": 0, "executable_plan_count": 0}
```

106 cycles reported *completed* having built *zero* snapshots, and both hourly monitor
snapshots recorded `health=HEALTHY`. The archived event store for run 1 has SHA-256
`e3b0c442…b7852b855` — the digest of an empty file.

**Two defects, not one.**

- **Wrong value at the API boundary.** `get_candles()` passed `granularity` straight
  through. `get_multi_timeframe_candles()` carried a private partial map that knew
  `1h → 1H` for two timeframes only, so every other call path sent a rejected value.
  `api_granularity()` is now the single canonical boundary, applied at the top of
  `get_candles` ahead of the request; unsupported values raise before any network call.
  Bitget accepts minutes lowercase and hours/days/weeks/months uppercase; `1M` (month)
  is matched case-sensitively so it can never collapse into `1m` (minute). All 18
  aliases were verified against the live endpoint.
- **Observability could not detect it.** Per-symbol errors are swallowed by design, so
  a fault hitting *every* symbol reached the end of the cycle and still published
  `scan_cycle_complete`. A non-empty symbol list yielding no snapshot now raises
  `ScanCycleProducedNoMarketData`, and the health check reports
  `SCAN_PRODUCED_NO_MARKET_DATA`, `SCAN_LOOP_FAILING` (≥3 consecutive) or `DEGRADED`.

Both defects were verified to reproduce with their fix reverted.

---

## 3. Run 2 — audited, FAIL against the 72 h criterion

| | |
|---|---|
| Clock start | 2026-07-25T16:09:41Z |
| Planned end | 2026-07-28T16:09:41Z |
| Actual end | 2026-07-27T10:53:51Z — `SIGTERM`, exit 143 |
| Baseline | `cda8187` |
| Config | LTCUSDT only, 15m/1h, max 1 position, 60 s scans, real detectors, smoke harness disabled |

### Time accounting

```
process lifetime    42.75 h
suspended         − 22.19 h   (single gap, host sleep)
effective scanning  20.56 h   = 28.5 % of the 72 h requirement
dead before audit    7.82 h   (unnoticed)
```

The 22.19 h gap shows the log jumping from `2026-07-26 14:38:21` straight to
`2026-07-27 12:49:44` with no intervening line, no exception and **the same PID
96180**, resuming with `report_age_hours=25.1`. That is process suspension, not a
crash. A `caffeinate` power assertion was held for exactly this purpose and did not
prevent it. `kern.boottime` = `2026-07-27T10:54:07Z`, **16 s after** the SIGTERM,
establishing a host restart as the terminating event.

### What passed

| Area | Evidence |
|---|---|
| Safety | 13/13 controls. 0 private endpoints, 0 order calls. Only 3 public paths; **9 892 requests, 100 % HTTP 200** |
| Event integrity | hash chain VALID across 49 events **after** a 22 h suspension and a SIGTERM; 0 duplicate ids/semantics, 0 terminal conflicts, 0 orphans, 0 events after close |
| Accounting | both closed trades reconcile independently to **< 1e-6**; exits inside the traded range (no impossible fills) |
| Stability | 0 tracebacks, 0 scan-cycle failures, 0 fail-closed paper writes, 0 HTTP errors, 0 × 400171 |
| Network resilience | a **real** DNS failure at 10:32:47 was absorbed by retry/backoff (1.25 s → 2.5 s), succeeded on attempt 3, cycle completed |

The real DNS incident upgrades retryable-network resilience from "proven at the code
seam" to proven against an actual outage.

### What failed

| Area | Evidence |
|---|---|
| Continuous uptime | 20.56 h effective of 72 h required |
| Host-sleep prevention | 22.19 h suspension despite an active power assertion |
| Restart after reboot | supervisor was a `nohup` shell loop; did not survive the reboot; 7.82 h dead |
| Monitoring continuity | monitor stopped silently at 2026-07-26T12:09:03Z; 21 snapshots where ~50 were due; logged nothing |

### Runtime and performance

| Metric | Value |
|---|---|
| Scan cycles completed | 1 177 (heartbeat agrees with log) |
| Cycle cadence | median 63 s, p95 63 s against a 60 s configured sleep |
| Derived scan duration | ≈ 3 s |
| Missed-scan windows > 300 s | 1 (the 22.19 h suspension) |
| Restarts / supervisor interventions | 0 / 0 |
| API latency (9 892 samples) | median 317.2 ms, p95 344.1 ms, max 1 313.0 ms, mean 322.0 ms (σ 33.8) |
| Disk at audit | 31 Gi free / 85 % used (improved from 23 Gi / 89 %) |
| CPU / memory | **evidence not available** — never sampled; process table lost at reboot |

### Trades

3 opened, 2 closed, 1 unresolved at freeze. All `low_vol_reclaim`, LTCUSDT, LONG, 15m.

| Trade | Fill | Exit | Reason | Gross | Fees | Net |
|---|---|---|---|---|---|---|
| `paper_50e14853a32d3dfa321b` | 46.750000 | 46.796750 | STOP_LOSS | +0.02832000 | 0.03400099 | **−0.00568099** |
| `paper_dec390d723ab0597e9a0` | 47.100000 | 47.147100 | STOP_LOSS | +0.01533000 | 0.01840520 | **−0.00307520** |
| `paper_2b760b2f13c268cf9af7` | 47.160000 | — | OPEN at freeze | — | — | — |

Aggregate closed: gross **+0.04365000**, fees **0.05240619**, net **−0.00875619**.

Both closures were profit-lock stops — positive gross converted to negative net by
fees. **Sample size 2: no inference of any kind is possible.** No take-profit was
reached, so `TP_TOUCH` and `PARTIAL_EXIT` remain unexercised in live conditions.

### Strategy accounting (873 funnel events in window)

Candidates detected 14 · selected 4 · executable 2 · blocked 3 · opened 3 · closed 2.
Dominant fail reasons: `NO_DETECTION` 403, `NOT_SELECTED` 10, `PLAN_BLOCKED` 3.
**Risk rejects: 0** — the risk engine blocked nothing, so its blocking path is
unproven outside unit tests.

**Open discrepancy:** `FORWARD_PAPER_LINK` PASS = 3 but `EXECUTABLE_DECISION` PASS = 2.
Three trades opened against two logged executable decisions. Unexplained; the event
store itself is internally consistent. Recorded as risk R9.

---

## 4. Live-readiness verdict

**PASS 7 · PASS WITH LIMITATION 5 · FAIL 3.**

FAIL: Restart Recovery, Supervisor, Monitoring.

**Final verdict: NOT READY FOR NEXT PHASE.** The 72 h criterion was not met
(20.56 h effective), a host reboot ended the run with no recovery, monitoring failed
silently, and the live order path has never been executed.

This is not a finding against the trading logic. Whenever the host was awake the
system behaved correctly. The blocking issue is that the environment could not keep it
awake, could not restart it, and could not report that it had stopped.

---

## 5. Appended history

| Date | Entry |
|---|---|
| 2026-07-25 | Run 1 started, invalidated the same day (granularity defect) |
| 2026-07-25 | Run 2 started at 16:09:41Z on `cda8187` |
| 2026-07-27 | Run 2 terminated by host reboot at T+42.75 h; forensic close-out audit performed; verdict NOT READY FOR NEXT PHASE |
| 2026-07-27 | Evidence frozen into `validation_72h/archive/RC1_forward_paper_validation.tar.gz` (912 KB, 63 files, all checksums verified) |
