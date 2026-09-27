# 13 — Audit Summary

Source: `validation_72h/CLOSEOUT_REPORT.md` (14-phase forensic close-out,
2026-07-27T18:42:52Z). Archive: `validation_72h/archive/`.

## Audit verdict

## **NOT READY FOR NEXT PHASE**

Live-readiness scoring: **PASS 7 · PASS WITH LIMITATION 5 · FAIL 3.**

## Correction the audit had to make

The audit was commissioned on the premise that the validation had *completed*. Evidence
contradicted it: 50.55 h of 72 h elapsed, and the process was already dead — terminated
by SIGTERM 16 s before a host reboot, not by an operator and not by completion.

## Findings by phase

| Phase | Finding |
|---|---|
| 1 Freeze | every process already dead; 0 orphans, 0 zombies, **0 lock holders**; 3 stale PID files documented, not removed |
| 2 Snapshot | 44-file read-only archive; CPU/memory and pre-boot sleep records **evidence not available** |
| 3 Runtime | 42.75 h lifetime, 22.19 h suspended, 20.56 h effective; 1 177 cycles; median cadence 63 s; 0 restarts |
| 4 Events | chain VALID; 0 duplicate ids/semantics; 0 terminal conflicts; 0 orphans; no impossible transitions |
| 5 Trades | both closed trades reconcile to < 1e-6; exits inside traded range; 1 unresolved |
| 6 Safety | 13/13 controls pass; 0 private endpoints; 9 892/9 892 HTTP 200 |
| 7 Errors | 0 CRITICAL, 2 ERROR (one real DNS incident, recovered), 44 WARNING, 0 tracebacks |
| 8 Strategy | 873 funnel events; 14 candidates, 4 selected, 2 executable, 3 opened; **0 risk rejects**; one unexplained counter discrepancy |
| 9 Performance | latency median 317.2 ms, σ 33.8; disk improved to 31 Gi free |
| 10 Summary | FAIL against the 72 h criterion |
| 11 Readiness | 7 PASS / 5 LIMITED / 3 FAIL |
| 12 Risks | 2 Critical, 3 High, 5 Medium, 3 Low |
| 13 Checklist | live deployment checklist produced, not performed |
| 14 Report | final verdict issued |

## The three FAIL categories

| Category | Evidence |
|---|---|
| **Restart Recovery** | host reboot 2026-07-27T10:54:07Z ended the run; nothing restored it; 7.82 h dead |
| **Supervisor** | did not survive the reboot; no liveness evidence after 2026-07-25T16:09:07Z |
| **Monitoring** | stopped silently at 2026-07-26T12:09:03Z; 21 of ~50 snapshots; no error, no alert |

## Silent failures identified

1. The monitor stopped and nothing noticed.
2. A 22.19 h suspension produced no log line, no alert, no health failure.
3. The 7.82 h post-reboot dead period went entirely unreported.

**The generalisable lesson: absence of alerts is not evidence of health.** Every
monitoring component in this stack is pull-based, local, and can die unobserved — and
all three failure modes occurred within 43 hours.

## What the audit explicitly did not fault

The trading logic. Safety was clean on 13/13 controls with zero private calls across
9 892 requests; the hash chain survived a 22-hour suspension and a SIGTERM intact; and
both closed trades reconcile independently to better than 1e-6 with no impossible fills.
Whenever the host was awake, the system did what it was asked to do.

The blocking issue is that the environment could not keep it awake, could not restart
it, and could not report that it had stopped.

## Evidence not available (stated, not estimated)

- CPU and memory usage — never sampled; process table lost at reboot.
- OS sleep/wake records for the 22.19 h gap — unified log rotated at boot.
- Whether the `caffeinate` assertion process was alive during the suspension.
- Fine-grained strategy rejection categories for the run window.
