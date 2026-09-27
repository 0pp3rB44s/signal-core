# FORWARD-PAPER VALIDATION — FORENSIC CLOSE-OUT REPORT

**Audit timestamp:** 2026-07-27T18:42:52Z (local 20:42:52 CEST)
**Auditor role:** Senior Principal Trading Systems Engineer — final operational validation
**Scope:** Forward-Paper validation "run 2", baseline commit `cda8187`
**Archive:** `validation_72h/closeout_20260727T184252Z/` (43 files, 11 MB, read-only)

No code, configuration, strategy or logic was modified during this audit.

---

## 0. CORRECTION TO THE AUDIT PREMISE

The audit was commissioned on the basis that "the Forward Paper validation has
completed." **Evidence contradicts this.** The validation did not complete and was
not stopped by an operator.

| Fact | Evidence |
|---|---|
| Clock start | `manifest.json` → `2026-07-25T16:09:41Z` |
| Planned end | `manifest.json` → `2026-07-28T16:09:41Z` |
| Elapsed at audit | 50.55 h of 72 h (**70.2 %**) |
| Remaining | 21.45 h |
| Trading process alive at audit | **No** — 0 processes |
| Cause of termination | `state/last_shutdown.json` → `signal: SIGTERM`, `exit_code: 143`, `2026-07-27T10:53:51Z` |

The run was **terminated early by a host reboot**, not by operator action and not by
completion. This report is therefore a close-out of an **incomplete** validation.

---

## PHASE 1 — VALIDATION FREEZE

No termination was necessary: every process was already dead on arrival.

| Component | PID | State at audit | Exit evidence |
|---|---|---|---|
| Trading runtime (`app.main`) | 96180 | **not running** | `last_shutdown.json`: `signal:SIGTERM`, exit 143, 2026-07-27T10:53:51Z |
| Supervisor (`forward_paper_keepalive`) | 90257 | **not running** | no shutdown record; log silent after 2026-07-25T16:09:07Z |
| Validation monitor (`monitor_loop`) | 90258 | **not running** | no shutdown record; last output 2026-07-26T12:09:03Z |
| Power assertion (`caffeinate`) | 96296 | **not running** | bound `-w 96180`; died with target |
| Archiver (`run_archiver`) | 49787 | **not running** | no shutdown record |

Freeze verification (`live_state/phase1_freeze.txt`):

- orphan processes: **0** · zombie processes: **0** · detached python in repo: **0**
- remaining watchdog loops: **0** · active runtime threads: **0**
- lock files present: 10 · **lock holders: 0** (no stale held locks)
- **Stale PID files remain** and were deliberately NOT removed (no changes permitted):
  `state/bot.pid` (96180), `validation_72h/monitor.pid` (90258),
  `validation_72h/supervisor.pid` (90257) — all reference dead processes.

Clean signal handling is evidenced: `RUNTIME_SIGNAL_RECEIVED | signal=SIGTERM |
exit_code=143` at 2026-07-27T10:53:51Z, followed by a written `last_shutdown.json`.

---

## PHASE 2 — FORENSIC SNAPSHOT

Immutable archive `validation_72h/closeout_20260727T184252Z/`, permissions `a-w`.

Captured: manifest, heartbeat, last-shutdown, runtime state, git state (commit,
branch, status, log), environment (OS, arch, python, boot time, disk, directory
sizes), `forward_paper.out` (5.1 MB, 27 965 lines), keepalive log, monitor log,
supervisor out, runtime log, forward-paper event store, outcomes CSV, data-quality
report, 21 monitor snapshots, live process state, freeze verification.

**Evidence not available** (not captured, cannot be reconstructed post-reboot):
per-process CPU/memory time series, the bot's own RSS/CPU at any point, thread
counts during the run, and OS sleep/wake records predating the 2026-07-27T10:54:07Z
boot (unified log rotated).

---

## PHASE 3 — RUNTIME ANALYSIS

| Metric | Value | Source |
|---|---|---|
| Bot process start | 2026-07-25T16:09:04Z | `forward_paper_runtime.state` |
| Bot process end | 2026-07-27T10:53:51Z | `last_shutdown.json` |
| **Process lifetime** | **42.75 h** | computed |
| **Suspended (single gap)** | **22.19 h** | log gap analysis |
| **Effective scanning time** | **20.56 h** | lifetime − gap |
| **Effective coverage of 72 h** | **28.5 %** | computed |
| Dead time SIGTERM → audit | 7.82 h | computed |
| Configured scan interval | 60 s | `forward_paper_runtime.state` |
| Scan cycles completed | **1 177** | `SCAN_CYCLE_COMPLETED` count; heartbeat agrees (`scan_cycles_completed: 1177`) |
| Scan cycles started | 1 177 | heartbeat |
| Cycle-to-cycle gap | min 62 s · **median 63 s** · p95 63 s · max 79 887 s | log timestamps |
| Derived scan duration | ≈ 3 s (median gap 63 s − 60 s sleep) | computed |
| Missed-scan windows > 300 s | **1** | log gap analysis |
| Restart count | **0** | `keepalive.history` absent; no `herstart gelukt` after 16:09:07Z |
| Supervisor interventions | **0** | keepalive log |
| Monitor interventions | 0 (read-only by design) | monitor log |
| Heartbeat frequency | per cycle; last write 2026-07-27T10:53:02Z | `runtime_heartbeat.json` |

### The 22.19-hour gap

The log jumps from `2026-07-26 14:38:21` directly to `2026-07-27 12:49:44` (local)
with **no intervening line, no exception, no restart, and the same PID 96180**. The
first line after the gap is a normal cycle, and `LEARNING_REFRESH_STARTED |
report_age_hours=25.1` confirms wall-clock advanced while the process believed it was
continuing.

This is the signature of **process suspension (host sleep)**, not a crash. A
`caffeinate -ims -w 96180` power assertion (PID 96296) was held for the express
purpose of preventing this, and it did not prevent it.

**Evidence not available:** the OS sleep/wake record for that window. The unified log
does not retain entries predating the 2026-07-27T10:54:07Z boot, and no independent
liveness logging of the caffeinate process exists. It is therefore **not established
by evidence** whether the assertion process died first or was held and proved
insufficient. The suspension itself is established; its precise mechanism is not.

### Termination

`kern.boottime` = **2026-07-27T10:54:07Z**, exactly **16 s after** the bot's SIGTERM.
This establishes a host restart as the terminating event. Nothing restarted the
validation after boot: the supervisor was a `nohup`-ed shell loop, which does not
survive reboot, and no launchd job supervises this harness.

---

## PHASE 4 — EVENT ACCOUNTING

Validated by `ForwardPaperEventStore.read_events()`, which enforces sequence
contiguity, `previous_hash` chaining, per-event checksums and semantic uniqueness.

| Check | Result |
|---|---|
| Hash chain | **VALID** across all 49 events |
| Sequence contiguous 1..49 | **True** |
| Duplicate `event_id` | **0** |
| Duplicate semantic transitions | **0** |
| Terminal-close conflicts | **0** |
| Fragmented transitions | **0** |
| Orphan events (no `TRADE_OPENED`) | **0** |
| Events after `TRADE_CLOSED` | **0** on both closed trades |
| Chronological ordering per trade | **True** on all 3 trades |
| Legacy unlinked events | 0 |

Observed event types (49 total): `TRADE_OPENED` 3, `MARK_DECISION` 22,
`MAE_UPDATE` 9, `MFE_UPDATE` 5, `SL_TOUCH` 2, `STOP_UPDATED` 2,
`PROFIT_LOCK_ACTIVATED` 2, `EXIT_REASON_TRANSITION` 2, `TRADE_CLOSED` 2.

Event types in the audit checklist that **did not occur**: `ENTRY_FILLED`,
`PARTIAL_EXIT`, `TP_TOUCH`, `TRAIL_UPDATE`. No take-profit was reached and no partial
exit occurred during the window, so their absence is consistent with the observed
price action rather than evidence of a defect. `ENTRY_FILLED` and `TRAIL_UPDATE` are
not emitted by this implementation — fills are recorded inside `TRADE_OPENED`
(`simulated_fill`) and stop movement via `STOP_UPDATED`. **No missing transition, no
impossible transition, and no incomplete lifecycle was found among closed trades.**

One trade is **unresolved**: `paper_2b760b2f13c268cf9af7` has `TRADE_OPENED` and 19
management events but no `TRADE_CLOSED` — it was open when the host rebooted.
`unresolved_open_trade_count: 1` is correctly reported by the data-quality layer.

---

## PHASE 5 — TRADE VALIDATION

All three trades: strategy `low_vol_reclaim`, symbol `LTCUSDT`, direction LONG,
timeframe 15m. Accounting was recomputed independently from raw events and compared
with the outcomes CSV.

### Trade 1 — `paper_50e14853a32d3dfa321b` (CLOSED)

| Field | Value |
|---|---|
| Entry / exit | 2026-07-26T04:45:00Z → 05:30:00Z (2 700 s) |
| Planned entry / fill | 46.705000 / **46.750000** |
| Size / notional | 0.605775401 / **28.320000 USDT** |
| Initial stop / risk price | 46.622672 / 0.127328 |
| Target | 46.915526 (not reached) |
| Initial risk | 0.07713217 |
| Stop moved | 46.622672 → **46.796750** (`PROFIT_LOCK_FEE_BE`) |
| MFE / MAE | +0.2353 % / −0.2139 % |
| Exit reason / price | STOP_LOSS / 46.796750 |
| Gross (recomputed) | **+0.02832000** |
| Fees (recomputed) | **0.03400099** (entry 0.016992 + exit 0.017008992) |
| Net (recomputed) | **−0.00568099** |
| CSV values | 0.02832 / 0.03400099 / −0.00568099 |
| **Reconciles** | **gross OK · fees OK · net OK** (< 1e-6) |
| result_r | −0.07365269 |
| Exit within traded range [46.65, 46.86] | **True** |

### Trade 2 — `paper_dec390d723ab0597e9a0` (CLOSED)

| Field | Value |
|---|---|
| Entry / exit | 2026-07-26T09:00:00Z → 09:15:00Z (900 s) |
| Planned entry / fill | 46.979586 / **47.100000** |
| Size / notional | 0.325477707 / **15.330000 USDT** |
| Initial stop / risk price | 46.882464 / 0.217536 |
| Target | 47.382797 (not reached) |
| Initial risk | 0.07080312 |
| Stop moved | 46.882464 → **47.147100** (`PROFIT_LOCK_FEE_BE`) |
| MFE / MAE | +0.3822 % / −0.1062 % |
| Exit reason / price | STOP_LOSS / 47.147100 |
| Gross (recomputed) | **+0.01533000** |
| Fees (recomputed) | **0.01840520** |
| Net (recomputed) | **−0.00307520** |
| CSV values | 0.01533 / 0.0184052 / −0.0030752 |
| **Reconciles** | **gross OK · fees OK · net OK** (< 1e-6) |
| result_r | −0.04343309 |
| Exit within traded range [47.05, 47.28] | **True** |

### Trade 3 — `paper_2b760b2f13c268cf9af7` (OPEN at freeze)

Entry 2026-07-26T09:15:00Z, fill 47.160000, size 0.298134012 (14.060000 USDT),
initial stop 46.922432, target 47.468838, initial risk 0.07082710, entry fee
0.008436, MFE +0.2545 % / MAE −0.4665 %. No exit. **Accounting cannot be closed for
this trade — evidence of a completed lifecycle is not available.**

### Aggregate (closed trades only)

Gross **+0.04365000** · Fees **0.05240619** · Funding 0.000000 · Slippage: entry
slippage recorded per trade, exit slippage modelled as 0.0 · **Net −0.00875619**.

Both closed trades exited at a stop that had been moved above entry by
`PROFIT_LOCK_FEE_BE`, producing a **positive gross** converted to a **negative net by
fees**. Fee drag (0.0524) exceeded gross (0.0437). This is an operational observation
on a 2-trade sample and carries **no statistical significance whatsoever**.

---

## PHASE 6 — SAFETY VALIDATION

| Control | Evidence | Verdict |
|---|---|---|
| Forward-Paper mode | `mode=FORWARD_PAPER_ONLY` in runtime state; `FORWARD_PAPER_ONLY ACTIVE` banner | **PASS** |
| Dry run / no live execution | launcher exports `EXECUTION_ENABLED=false`, `EXECUTION_MODE=DRY_RUN` | **PASS** |
| No private execution | `PRIVATE EXCHANGE CALLS DISABLED` banner | **PASS** |
| No live orders | `place_order`/`place_futures_order` occurrences: **0** | **PASS** |
| No private API calls | private `/account`, `/order`, `/position` paths: **0** | **PASS** |
| API surface actually used | only 3 public paths: `/market/candles` 8 323, `/market/merge-depth` 1 177, `/market/contracts` 400 | **PASS** |
| All responses | **9 892 requests, 100 % `status=200`** | **PASS** |
| Execution markers | `EXECUTION_REPORT`/`ORDER_PLACED`/`ORDER_SUBMITTED`: **0** | **PASS** |
| Position manager disabled | `POSITION_SYNC`/`PositionManager` markers: **0** | **PASS** |
| Credentials protected | launcher blanks `BITGET_API_KEY/SECRET/PASSPHRASE`; no secret appears in logs | **PASS** |
| Max positions respected | 3 trades, strictly sequential; never >1 concurrently open | **PASS** |
| Mandatory stop | all 3 trades carry a non-zero `initial_stop` | **PASS** |
| Risk model | fixed notional per trade, no compounding, leverage 1.0, no escalation observed | **PASS** |

**No safety control was breached at any point in the run.**

---

## PHASE 7 — ERROR ANALYSIS

Full sweep of `forward_paper.out` (27 965 lines) plus keepalive, monitor and
supervisor logs.

| Class | Count | Detail |
|---|---|---|
| CRITICAL | **0** | — |
| ERROR | **2** | both `BITGET_DNS_RESOLUTION_FAILURE`, same incident |
| WARNING | 44 | 19 `PLAN_REJECT`, 15 `PLANNER_NOTIONAL_CAPPED`, 3 `BITGET_REQUEST_EXCEPTION`, 3 `BITGET_NETWORK_RETRY`, 1 `RUNTIME_SIGNAL_RECEIVED`, 3 banner lines |
| Tracebacks | **0** | — |
| `SCAN_CYCLE_FAILED` | **0** | — |
| `FORWARD_PAPER_FAILED_CLOSED` | **0** | — |
| `BITGET_HTTP_ERROR` | **0** | — |
| HTTP 400171 (granularity) | **0** | the run-1 defect did not recur |
| Non-200 API responses | **0** | — |
| Deadlocks / lock contention | none observed; 0 lock holders at freeze | — |
| State corruption | none; chain VALID | — |
| File corruption | none; all artefacts parse | — |
| False-HEALTHY conditions | **0** — no cycle published `scan_cycle_complete` with `snapshot_count=0` | — |

### Real network incident (unplanned, live)

At 2026-07-26 10:32:47 a genuine DNS resolution failure occurred:

```
ERROR  BITGET_DNS_RESOLUTION_FAILURE  /market/contracts  attempt=1/3
WARN   BITGET_NETWORK_RETRY  sleep=1.25s  attempt=1
ERROR  BITGET_DNS_RESOLUTION_FAILURE  /market/contracts  attempt=2/3
WARN   BITGET_NETWORK_RETRY  sleep=2.5s   attempt=2
INFO   BITGET_API_LATENCY  /market/contracts  status=200  latency_ms=1117.78
```

A second independent failure hit `/market/candles` at 10:33:07 and also recovered.
`SCAN_CYCLE_COMPLETED` followed at 10:33:13. **This upgrades retryable-network
resilience from "proven at the code seam" to "proven against a real outage":** the
exception was visible, the retry layer with exponential backoff absorbed it, the
process survived, no duplicate paper order was generated, and no state was corrupted.

### Silent failures found

1. **The validation monitor stopped producing output at 2026-07-26T12:09:03Z** and
   never resumed — 21 snapshots exist where roughly 50 would be expected for the
   elapsed window. It logged no error. Nothing monitors the monitor.
2. **The 22.19 h suspension produced no log line, no alert and no health failure.**
   No component detected that the system had been frozen for most of a day.
3. **The post-reboot dead period (7.82 h) went entirely unnoticed.** No process,
   supervisor or alert reacted to the run ending.

---

## PHASE 8 — STRATEGY ACCOUNTING

Funnel events within the run window: **873**.

| Stage | PASS | FAIL | Fail reason |
|---|---|---|---|
| DETECTOR_ATTEMPT | 417 | — | — |
| DETECTOR_DECISION | 14 | 403 | `NO_DETECTION` 403 |
| SELECTOR_DECISION | 4 | 10 | `NOT_SELECTED` 10 |
| SCORING_DECISION | 5 | 0 | — |
| RISK_DECISION | **5** | **0** | — |
| PLANNER_DECISION | 2 | 3 | `PLAN_BLOCKED` 3 |
| EXECUTABLE_DECISION | 2 | 3 | `PLAN_BLOCKED` 3 |
| FORWARD_PAPER_LINK | **3** | 0 | — |
| OUTCOME_LINK | 2 | 0 | — |

Counts: candidates detected **14**, selected **4**, executable **2**, blocked **3**,
trades opened **3**, trades closed **2**.

- **Risk rejects: 0** — the risk engine blocked nothing in this window.
- **Expectancy / edge / AI-gate / pressure / momentum / continuation rejects:** no
  events carrying those reason codes occurred in the window. Non-detections are
  attributed to `NO_DETECTION` (403) and non-selections to `NOT_SELECTED` (10);
  finer-grained rejection categories are **evidence not available** for this run.
- Plan blocks (3) are recorded as `PLAN_BLOCKED`; 19 `PLAN_REJECT` and 15
  `PLANNER_NOTIONAL_CAPPED` warnings appear in the log.

**Note on counting:** `FORWARD_PAPER_LINK` PASS = 3 while `EXECUTABLE_DECISION`
PASS = 2. Three trades were opened but only two executable decisions were logged in
the window. This is an **unexplained discrepancy in funnel accounting** and is
recorded as an open finding; the event store itself is internally consistent.

---

## PHASE 9 — PERFORMANCE ANALYSIS

### API latency (9 892 samples, all HTTP 200)

| Endpoint | n | min | median | p95 | max |
|---|---|---|---|---|---|
| `/market/candles` | 8 321 | 292.3 | 317.4 | 344.5 | 1 313.0 |
| `/market/contracts` | 394 | 302.7 | 326.1 | 356.8 | 1 117.8 |
| `/market/merge-depth` | 1 177 | 291.8 | 312.5 | 333.8 | 803.1 |
| **ALL** | **9 892** | **291.8** | **317.2** | **344.1** | **1 313.0** |

Mean 322.0 ms, σ 33.8 ms. Latency was stable; both maxima coincide with the DNS
incident retries.

### Cycle latency

Median cycle-to-cycle 63 s against a 60 s configured sleep ⇒ **derived scan duration
≈ 3 s**. p95 identical at 63 s: cadence was consistent apart from the single
suspension.

### Storage

| Path | Size at audit |
|---|---|
| `logs/` | 1.1 G (`forward_paper.out` 5.1 M) |
| `reports/` | 1.1 G |
| `data_store/` | 121 M (`funnel_events.jsonl` 74 M) |
| `state/` | 72 M |
| `data/archive/` | 194 M |
| `validation_72h/` | 12 M |
| Disk free | **31 Gi (85 % used)** — improved from 23 Gi / 89 % at run start |

Disk pressure did **not** materialise as a risk. **CPU and memory usage are evidence
not available** — no sampling was performed during the run and the process table was
lost at reboot.

---

## PHASE 10 — VALIDATION SUMMARY

### **FAIL** — against the stated acceptance criterion

The mission criterion was **72 continuous unattended hours**. Evidence:

- effective scanning time **20.56 h = 28.5 %** of the requirement;
- process lifetime 42.75 h, of which **22.19 h suspended**;
- run terminated at T+42.75 h by host reboot, then **7.82 h dead and unnoticed**;
- the clock never reached 72 h (50.55 h elapsed at audit, 21.45 h short).

This is a FAIL of the **reliability** objective. It is **not** a failure of the
trading lifecycle, which performed correctly whenever the host was awake:

**What passed on evidence:** safety (13/13 controls, zero private calls, 100 % 200s),
event-chain integrity (0 duplicates, 0 conflicts, 0 orphans), trade accounting (both
closed trades reconcile exactly to < 1e-6, no impossible fills), zero tracebacks,
zero false-HEALTHY conditions, the run-1 granularity defect did not recur, and
retryable-network resilience proven against a **real** outage.

**What failed on evidence:** continuous uptime, host-sleep prevention, restart after
host reboot, and monitoring continuity.

---

## PHASE 11 — LIVE READINESS AUDIT

| # | Category | Verdict | Evidence |
|---|---|---|---|
| 1 | Execution Engine | **PASS WITH LIMITATION** | paper lifecycle correct end-to-end; **live order path never exercised** — 0 order calls by design |
| 2 | Exchange Safety | **PASS** | 0 private endpoints, 0 order calls, credentials blanked, 9 892/9 892 public 200s |
| 3 | Restart Recovery | **FAIL** | supervisor did not survive host reboot; 7.82 h dead with no recovery; `nohup` loop has no boot persistence |
| 4 | Risk Engine | **PASS WITH LIMITATION** | mandatory stops present on 3/3 trades, max-1-position respected, no compounding; but **0 risk rejects occurred**, so blocking behaviour was never exercised live |
| 5 | State Persistence | **PASS** | hash chain VALID across 49 events after an unclean 22 h suspension and a SIGTERM; no corruption |
| 6 | Heartbeat | **PASS WITH LIMITATION** | truthful throughout (`snapshot_count=1`, cycles agree with logs); but **did not detect a 22 h suspension** — freshness is not evaluated by any live consumer |
| 7 | Supervisor | **FAIL** | 0 interventions needed while alive, but did not survive reboot and produced no liveness evidence after 2026-07-25T16:09:07Z |
| 8 | Monitoring | **FAIL** | monitor silently stopped at 2026-07-26T12:09:03Z; 21 of ~50 expected snapshots; no self-monitoring |
| 9 | Logging | **PASS** | 27 965 lines, structured, 0 tracebacks, signal capture and shutdown record written correctly |
| 10 | Trade Accounting | **PASS** | both closed trades reconcile independently to < 1e-6; fees and gross verified from raw events |
| 11 | Event Integrity | **PASS** | chain VALID, 0 duplicate ids/semantics, 0 terminal conflicts, 0 orphans, correct ordering |
| 12 | Recovery | **PASS WITH LIMITATION** | survived a real DNS outage and a 22 h suspension without state damage; **host-level recovery absent** |
| 13 | Emergency Stop | **PASS WITH LIMITATION** | SIGTERM handled cleanly (exit 143, shutdown record); stop flags exist; **no live kill-switch was exercised** |
| 14 | Configuration Separation | **PASS WITH LIMITATION** | strict launcher enforces safe env independent of `.env`; but **manifest config hash `17524444…` ≠ hash stamped on trades `d6f53802…`** — config identity is not reliably traceable (git commit *does* match: `cda8187`) |
| 15 | Operational Documentation | **PASS WITH LIMITATION** | pre-flight report, manifest, invalidation record and this close-out exist; **no runbook for host reboot or monitor death** |

**Totals: PASS 7 · PASS WITH LIMITATION 5 · FAIL 3.**

---

## PHASE 12 — REMAINING RISKS

### CRITICAL

**R1 — No recovery after host restart.**
*Evidence:* boot 2026-07-27T10:54:07Z; nothing running for 7.82 h; supervisor was a
`nohup` shell loop; `com.cgc.tradingbot` launchd job not loaded for this harness.
*Impact:* in live trading a reboot leaves positions unmanaged indefinitely — stops
and targets stop being enforced by the bot.
*Likelihood:* High (observed once in 43 h).
*Recommendation (operational, no code change):* supervise via a boot-persistent
mechanism and verify by rebooting before any live deployment.

**R2 — Host sleep suspends the trading process undetected.**
*Evidence:* 22.19 h log gap, same PID, no error; power assertion was intended to
prevent exactly this; `pmset` showed idle sleep at 1 minute.
*Impact:* an open position is unmanaged for hours while the process believes it is
running; stop-losses are not evaluated.
*Likelihood:* High on this hardware.
*Recommendation:* treat sleep prevention as a hard pre-flight gate with independently
verified assertion liveness; do not rely on a child `caffeinate` alone.

### HIGH

**R3 — Nothing monitors the monitor.**
*Evidence:* monitor stopped 2026-07-26T12:09:03Z; 21 vs ~50 expected snapshots; no
error logged; no alert.
*Impact:* observability can fail silently, so absence of alerts cannot be trusted as
evidence of health — the precise failure mode this validation existed to catch.
*Likelihood:* High (observed).

**R4 — Heartbeat freshness is never evaluated by a live consumer.**
*Evidence:* heartbeat remained at 2026-07-27T10:53:02Z for 7.82 h with no
consequence; a 22 h stale heartbeat produced no failure.
*Impact:* the strongest available liveness signal is recorded but not acted upon.
*Likelihood:* High.

**R5 — Live order path has never been executed.**
*Evidence:* 0 order calls, 0 private endpoints across the entire validation, by design.
*Impact:* order placement, rejection handling, partial fills, slippage on real fills
and exchange error codes are entirely unvalidated.
*Likelihood:* Certain to matter at go-live.

### MEDIUM

**R6 — Statistically meaningless trade sample.** 2 closed trades, both stopped out
after profit-lock, net −0.00875619 total. No TP was ever reached, so
`TP_TOUCH`/`PARTIAL_EXIT` paths are unexercised in live conditions.

**R7 — Risk engine blocking never exercised.** 0 risk rejects in the window; gate
behaviour is unproven outside unit tests.

**R8 — Config identity not traceable.** Manifest hash `17524444…` vs trade-stamped
`d6f53802…`. Reproducing the exact configuration of this run from the manifest alone
is not possible.

**R9 — Funnel accounting discrepancy.** 3 `FORWARD_PAPER_LINK` PASS vs 2
`EXECUTABLE_DECISION` PASS. Unexplained; event store itself is consistent.

**R10 — One position left unresolved.** `paper_2b760b2f13c268cf9af7` open at freeze;
in live trading an equivalent state is an unmanaged position.

### LOW

**R11 — Stale PID files persist** (`bot.pid` 96180, `monitor.pid` 90258,
`supervisor.pid` 90257) pointing at dead processes.

**R12 — Archiver stopped with the reboot** and did not restart; no throughput
threshold is defined, so degradation would not be flagged.

**R13 — Storage growth unbounded in principle** — `logs/` 1.1 G, `reports/` 1.1 G.
Disk improved to 31 Gi free during the run, so this did not bite.

---

## PHASE 13 — LIVE DEPLOYMENT CHECKLIST (checklist only — NOT performed)

**API**
- [ ] Live API key provisioned with **trade** permission and **withdrawal disabled**
- [ ] IP allow-list configured and verified
- [ ] Key/secret/passphrase supplied via secret store, never `.env` in the repo
- [ ] Private endpoint reachability confirmed on a throwaway key first
- [ ] Rate-limit budget measured against live order + position polling

**Security**
- [ ] Secret scan across repo, logs and archives
- [ ] Log redaction verified for key material
- [ ] File permissions on `state/`, `data_store/`, credential files
- [ ] Confirm no credential appears in any archived validation artefact

**Risk**
- [ ] Max position count enforced live
- [ ] Max notional per trade and aggregate exposure cap
- [ ] Mandatory stop verified present on the exchange, not only internally
- [ ] Daily / weekly loss kill-switch thresholds set and tested
- [ ] Leverage fixed; escalation impossible
- [ ] Risk-reject path exercised deliberately at least once

**Logging**
- [ ] Structured logs shipped off-host
- [ ] Retention and rotation configured (`logs/` and `reports/` are ~1.1 G each)
- [ ] Order lifecycle logged with exchange order IDs

**Monitoring**
- [ ] Heartbeat **freshness** alert with an explicit staleness threshold
- [ ] Monitor self-liveness (a watchdog for the watchdog)
- [ ] Alert on scan-failure count, on unresolved open positions, on chain-invalid
- [ ] Alert delivered off-host (not only local log)
- [ ] Archiver throughput threshold defined

**Recovery**
- [ ] Boot-persistent supervision; verified by a real reboot test
- [ ] Sleep prevention verified with independent assertion liveness checking
- [ ] Position reconciliation against exchange truth on every start
- [ ] Documented behaviour for a position open at process death

**Rollback**
- [ ] One-command halt that cancels working orders and stops new entries
- [ ] Documented rollback to forward-paper mode
- [ ] Tagged, reproducible deployment commit

**Position limits / exchange configuration**
- [ ] Margin mode, leverage and position mode set on the exchange account
- [ ] Symbol whitelist enforced server-side where possible
- [ ] Minimum notional and precision validated per symbol

**Alerting / health checks**
- [ ] `check_forward_paper.sh` equivalent for live mode
- [ ] Paging path for CRITICAL, with a tested delivery
- [ ] Post-restart health gate before trading resumes

---

## PHASE 14 — FINAL REPORT

**Executive summary.** The forward-paper validation did not complete. It was
terminated at T+42.75 h by a host reboot, having already lost 22.19 h to host sleep,
leaving **20.56 h of effective scanning — 28.5 % of the 72 h requirement**. Within
the time the system was actually awake, it behaved correctly: safety was absolute,
event integrity was perfect, and trade accounting reconciles exactly. Every failure
found is in the **operational envelope surrounding** the bot, not in the bot's
trading logic.

**Technical summary.** 1 177 scan cycles, median cadence 63 s, derived scan duration
≈ 3 s, 9 892 API requests at 100 % HTTP 200, median latency 317 ms. Zero tracebacks,
zero scan-cycle failures, zero fail-closed paper writes, zero HTTP errors. A real DNS
outage was absorbed by the retry layer with no state damage.

**Operational summary.** 0 supervisor interventions were needed while the host was
awake; 0 were possible once it rebooted. The monitor stopped silently 22.7 h before
the end. Neither the suspension nor the 7.82 h outage triggered any alert.

**Validation summary.** FAIL against the 72-hour criterion; PASS on safety, event
integrity and accounting.

**Trade summary.** 3 opened, 2 closed, 1 unresolved. Gross +0.04365000, fees
0.05240619, net **−0.00875619**. Both closures were profit-lock stops — positive
gross, negative net after fees. Sample size 2: no inference is possible.

**Runtime summary.** Process lifetime 42.75 h; suspended 22.19 h; effective 20.56 h;
dead 7.82 h before audit; restarts 0.

**Performance summary.** Latency stable (σ 33.8 ms); disk pressure eased to 31 Gi
free; CPU and memory **evidence not available**.

**Risk summary.** 2 Critical, 3 High, 5 Medium, 3 Low.

**Evidence summary.** Archive `validation_72h/closeout_20260727T184252Z/`, 43 files,
11 MB, read-only. Every figure in this report is derived from those artefacts.

**Live readiness summary.** PASS 7 · PASS WITH LIMITATION 5 · FAIL 3 (Restart
Recovery, Supervisor, Monitoring).

**Known limitations.** No live order path exercised; no TP/partial-exit path
exercised live; risk blocking never triggered; CPU/memory not sampled; OS sleep
records unavailable; caffeinate liveness during the gap unknown; config hash not
traceable to the manifest; funnel link/executable counts disagree by one.

**Open risks.** R1–R13 above.

---

# FINAL VERDICT

## **NOT READY FOR NEXT PHASE**

**Justification, strictly on evidence:**

1. The acceptance criterion — 72 continuous unattended hours — was **not met**.
   Effective coverage was **20.56 h (28.5 %)**; the clock reached 50.55 h of 72 h and
   the process was dead for the final 7.82 h of that.
2. **Restart Recovery: FAIL.** A host reboot at 2026-07-27T10:54:07Z ended the run and
   nothing restored it. In live trading this leaves positions unmanaged.
3. **Supervisor: FAIL.** It did not survive the reboot and produced no liveness
   evidence for the final 42 h.
4. **Monitoring: FAIL.** The monitor stopped silently at 2026-07-26T12:09:03Z, and
   neither the 22.19 h suspension nor the 7.82 h outage raised anything. Absence of
   alerts is therefore not evidence of health.
5. The live order path has **never been executed** — 0 private calls by design — so
   execution, fills, rejections and slippage remain entirely unvalidated.

**What this verdict does not say.** It is not a finding against the trading lifecycle.
Safety was clean on 13/13 controls with zero private calls across 9 892 requests; the
hash chain survived a 22-hour suspension and a SIGTERM intact; and both closed trades
reconcile independently to better than 1e-6 with no impossible fills. The bot did what
it was asked to do whenever the machine was awake.

The blocking issue is that **the environment could not keep it awake, could not
restart it, and could not tell anyone it had stopped** — and a 72-hour reliability
claim cannot be made from 20.56 hours of evidence.
