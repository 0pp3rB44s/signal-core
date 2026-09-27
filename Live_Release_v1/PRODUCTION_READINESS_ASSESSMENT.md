# PRODUCTION READINESS ASSESSMENT — Live Version 1

**Assessment date:** 2026-07-27
**Assessed baseline:** `rc1-forward-paper-validated` (validated commit `cda8187`)
**Assessor role:** Principal Trading Systems Architect
**Evidence base:** `validation_72h/archive/RC1_forward_paper_validation.tar.gz`
(sha256 `3b5bf715…0739`, 63 files, integrity verified 63/63 twice),
`validation_72h/CLOSEOUT_REPORT.md`, repository at commit `55b5c88`.

No runtime behaviour was changed to produce this assessment.

---

# VERDICT: NOT APPROVED FOR LIVE TRADING

**2 PASS · 7 PASS WITH LIMITATION · 4 FAIL**

The system's *trading logic* is in good order. Its *operational envelope* is not, and
its *live execution path has never run*. Those are the two things that decide whether
real money may be exposed, so the verdict is negative regardless of how clean the paper
results are.

---

## Scoring summary

| # | Category | Verdict |
|---|---|---|
| 1 | Execution Engine | **FAIL** |
| 2 | Risk Engine | PASS WITH LIMITATION |
| 3 | Supervisor | **FAIL** |
| 4 | Monitoring | **FAIL** |
| 5 | Heartbeat | PASS WITH LIMITATION |
| 6 | Recovery | PASS WITH LIMITATION |
| 7 | Logging | **PASS** |
| 8 | State Persistence | **PASS** |
| 9 | Configuration | PASS WITH LIMITATION |
| 10 | Documentation | PASS WITH LIMITATION |
| 11 | Auditability | PASS WITH LIMITATION |
| 12 | Traceability | PASS WITH LIMITATION |
| 13 | Operational Maturity | **FAIL** |

---

## 1. Execution Engine — **FAIL**

**Evidence for.** The *simulated* path is correct end-to-end: 3 trades opened, 2 closed,
both reconciling independently to < 1e-6, exits inside the traded candle range (no
impossible fills). Mandatory stop present on 3/3. Layered gates implemented:
execution-enabled, symbol allow-list, per-cycle cap, notional ceiling and floor, balance
precheck, open-position gate, symbol cooldown.

**Evidence against.** The live order path has **never been executed**. Across the entire
campaign: **0 order calls, 0 private endpoints, 9 892 public requests**. Order
submission, rejection handling, partial fills, real slippage, exchange error codes,
protective-order attachment against a real exchange and live position reconciliation are
all **completely unvalidated**.

**Why FAIL and not LIMITED.** For a component whose defects lose real money, "implemented
and unit-tested but never once run against the venue" is not a limitation — it is an
absence of evidence. Risk R5.

## 2. Risk Engine — **PASS WITH LIMITATION**

**Evidence for.** Sizing derives from stop distance and is capped by notional ceiling
(15 `PLANNER_NOTIONAL_CAPPED` events). Fixed leverage; no compounding, no escalation.
Max-1-position respected — 3 trades strictly sequential, never concurrent. Mandatory
stop on 3/3 trades. Daily soft/hard and weekly-freeze kill switches implemented, day
mode logged every cycle.

**Limitations.** `RISK_DECISION` PASS 5 / **FAIL 0** — no blocking path was exercised in
a running system (R7). Equity was static at 56.64 throughout, so no loss-limit behaviour
was observed. `MAX_OPEN_POSITIONS` was 1, so multi-position interaction is untested. The
engine reads absolute module paths (`BASE_PATH`, `REPORTS_PATH`, `AGENT_DECISIONS_PATH`)
outside the deployed commit, so **risk decisions depend on mutable files** and are not
reproducible from the commit alone.

## 3. Supervisor — **FAIL**

**Evidence for.** Health-check driven rather than merely process-alive. Restarts only via
the strict launcher. Correctly **fail-closes** on `DEGRADED`,
`SCAN_PRODUCED_NO_MARKET_DATA` and `SCAN_LOOP_FAILING` instead of masking code defects.
Measured recovery: 6 s / 2 s foreground, **91 s / 121 s detached**, 0 duplicates, power
assertion rebound.

**Evidence against.** It **did not survive a host reboot**. Boot at 2026-07-27T10:54:07Z;
nothing ran for the following **7.82 h**. It is a `nohup` shell loop with no boot
persistence, and it produced no liveness evidence after 2026-07-25T16:09:07Z. A
supervisor that cannot outlive the machine it supervises does not provide unattended
operation. Risk R1.

## 4. Monitoring — **FAIL**

**Evidence for.** Health check emits typed statuses with distinct exit codes; all three
scan verdicts verified live against injected heartbeats. Rich structured logging and a
per-decision funnel.

**Evidence against.** The observation loop **stopped silently** at 2026-07-26T12:09:03Z
— 21 snapshots where ~50 were due — logging no error. It did not detect the 22.19 h
suspension, and it did not detect 7.82 h of process death. Nothing monitors the monitor
(R3); no alert is delivered off-host; heartbeat freshness has no live consumer (R4);
archiver throughput has no threshold (R12).

**The decisive point:** during this campaign, absence of alerts was indistinguishable
from health. Monitoring that fails silently is worse than none, because it manufactures
false confidence.

## 5. Heartbeat — **PASS WITH LIMITATION**

**Evidence for.** Truthful throughout run 2: `scan_cycles_completed: 1177` agrees exactly
with 1 177 `SCAN_CYCLE_COMPLETED` log lines; `snapshot_count: 1` matched reality. It
correctly recorded a clean shutdown (`signal:SIGTERM`, `exit_code: 143`). After D5 was
fixed, a failed scan can no longer publish `scan_cycle_complete`.

**Limitations.** Its **freshness is evaluated by nothing that runs** — it froze for
7.82 h with no consequence (R4). It did not surface the 22.19 h suspension: it simply
stopped advancing, which no live consumer treats as a fault.

## 6. Recovery — **PASS WITH LIMITATION**

**Evidence for.** Survived a **real** DNS outage (retry 1.25 s → 2.5 s, success on
attempt 3, cycle completed, no duplicate order, no state damage). Survived a 22.19 h
suspension *and* a SIGTERM with the hash chain intact. Open positions reconstruct from
the event log; a persisted terminal intent completes before newer candles, so a crash
cannot strand a trade; duplicate opens are suppressed by deterministic `candidate_id`.

**Limitations.** Host-level recovery is absent (R1, R2). Live-mode position
reconciliation against the exchange has never been tested. There is **no off-host
backup** — `data_store/`, `state/` and telemetry are local only, so total host loss means
total loss of trading history not already archived. Rollback has never been rehearsed.

## 7. Logging — **PASS**

27 965 structured lines over 42.75 h with **0 tracebacks**. Distinct, greppable markers
for every failure mode. Signal capture and shutdown record written correctly. Per-request
API latency logged (9 892 samples). Full per-decision funnel with stable
`candidate_id`. No credential appeared in any log across the campaign. Log rotation
exists (`com.cgc.cleanlogs`).

The logs were sufficient to reconstruct the entire incident timeline — including
distinguishing process *suspension* from a crash, purely from a gap between two lines and
the same PID either side. That is the practical test of logging quality, and it passed.

## 8. State Persistence — **PASS**

The forward-paper store is append-only and hash-chained, with sequence contiguity,
per-event checksums, semantic-transition uniqueness and terminal-close conflict
detection. It was **VALID across all 49 events after a 22-hour process suspension and a
SIGTERM**: 0 duplicate event ids, 0 duplicate semantic transitions, 0 terminal conflicts,
0 orphans, 0 events after close. All five corruption modes were verified detectable on
isolated copies, with a clean control reading correctly. Outcomes are reconstructed from
events, never written directly. At freeze: 10 lock files, **0 holders** — no stale locks.

One unresolved open trade is correctly *reported* as unresolved rather than silently
dropped, which is the desired behaviour.

## 9. Configuration — **PASS WITH LIMITATION**

**Evidence for.** The safety boundary is the launcher, not `.env`:
`scripts/start_forward_paper.sh` forces `FORWARD_PAPER_ONLY`, sets
`EXECUTION_ENABLED=false` and `EXECUTION_MODE=DRY_RUN`, disables the position manager,
**blanks the credentials**, and refuses to start on a dirty tree or beside an existing
bot. `enforce_forward_paper_only()` verified exhaustively: **16/16 combinations, 0
violations**; `forward_paper_only` always forces execution off.

**Limitations.** Configuration identity is **not traceable**: the manifest recorded
`config_version_hash 17524444…` while trades were stamped `d6f53802…` (R8). Code identity
does match (`cda8187`). Risk-relevant inputs are read from absolute paths outside the
commit. And the clean-tree requirement, while a good gate, silently breaks **every**
automatic restart when violated — a sharp operational edge.

## 10. Documentation — **PASS WITH LIMITATION**

**Evidence for.** This release provides fifteen documents covering architecture,
execution, risk, configuration and exchange prep, deployment, rollback, recovery and DR,
monitoring, emergency procedures, the operator handbook, risks, validation, audit and
release notes. Repository docs carry the campaign history, risk register, known
limitations, recovery procedures and monitoring guide. All history was appended, never
overwritten — verified mechanically: **0 original lines lost** across seven modified
files.

**Limitations.** The documentation describes procedures that have **never been executed**
— live deployment, rollback rehearsal, disaster recovery from total host loss. Documented
is not the same as rehearsed. No operator other than the author has followed these
runbooks end to end.

## 11. Auditability — **PASS WITH LIMITATION**

**Evidence for.** A complete forensic close-out was possible after the fact from logs and
state alone, including establishing the *cause* of termination (host reboot 16 s after
SIGTERM) and distinguishing suspension from crash. Evidence is preserved as a checksummed
bundle: 63 files, per-file SHA-256, verified 63/63 and re-verified after round-trip
extraction, with the committed git blob proven byte-identical. Run 1's empty event store
is cryptographically corroborated by the empty-file digest.

**Limitations.** **CPU and memory were never sampled**, so no resource analysis is
possible. OS sleep/wake records for the 22.19 h gap were lost to log rotation at boot.
Whether the power-assertion process was alive during the suspension cannot be
established. These are recorded as "evidence not available" rather than estimated —
correct practice, but they are real gaps in what an audit can conclude.

## 12. Traceability — **PASS WITH LIMITATION**

**Evidence for.** Every decision carries a stable `candidate_id` from detector through
selector, scorer, risk, planner, executable decision, forward-paper link and outcome
link. Trades are stamped with the git commit — `cda8187`, matching the deployed baseline.
Events are hash-chained, so tampering is detectable.

**Limitations.** The config hash on trades does not match the manifest (R8). Funnel
counters disagree with the event store: 3 `FORWARD_PAPER_LINK` PASS versus 2
`EXECUTABLE_DECISION` PASS — three trades opened against two logged executable decisions,
**unexplained** (R9). Fine-grained rejection categories were not emitted for the window.
Traceability is therefore strong for code identity and event integrity, weaker for
configuration and funnel reconciliation.

## 13. Operational Maturity — **FAIL**

**Evidence.** The single acceptance criterion — 72 continuous unattended hours — was met
to **28.5 %** (20.56 h effective). The run ended by host reboot at T+42.75 h and lay dead
7.82 h. Two prior campaigns also failed: run 1 invalidated after 1 h 51 m, and an earlier
pre-flight attempt aborted when the supervisor could not restart on a dirty tree.

Three of the five defects found in this campaign (D1, D2, D5) were **silent failures** —
the system reported healthy while doing nothing useful. Two were caught only because a
human looked at raw evidence rather than at the health status.

**The system has never once demonstrated that it can be left alone.** That is the
definition of operational maturity, and it is unmet.

---

## Preconditions for reassessment

Live approval requires evidence, not implementation, for each of:

| # | Precondition | Closes |
|---|---|---|
| 1 | A full-duration unattended reliability run passes | Operational Maturity |
| 2 | Boot-persistent supervision, verified by a real reboot | R1, Supervisor |
| 3 | Sleep prevention with independent assertion-liveness checking | R2 |
| 4 | Monitor self-liveness plus off-host alert delivery | R3, R4, Monitoring |
| 5 | Live order path exercised at minimum size in a controlled window | R5, Execution Engine |
| 6 | Risk-gate blocking exercised at least once in a running system | R7, Risk Engine |
| 7 | Off-host backup of event store, telemetry and state | Recovery |
| 8 | Rollback rehearsed end to end | Documentation, Recovery |
| 9 | Config hash traceability reconciled; funnel discrepancy explained | R8, R9, Traceability |
| 10 | Exchange preparation completed and verified | Configuration |
| 11 | **Explicit written owner authorisation** | — |

Items 1–4 are the critical path. Nothing else matters until the system can stay alive
unattended and say so truthfully.

---

## Statement

Live Version 1 is **documented but not deliverable**. Everything required to operate the
system in production now exists on paper; what does not exist is evidence that the system
can run unattended, recover without a human, report its own failure, or place a single
real order.

The forward-paper results are genuinely good — safety absolute across 9 892 requests, an
event chain intact through a 22-hour suspension and a SIGTERM, and accounting exact to
better than 1e-6. It would be easy to read those numbers as readiness. They are not. They
describe a system that works **while someone is watching a machine that happens to be
awake**.

**VERDICT: NOT APPROVED FOR LIVE TRADING.**
Live execution must remain disabled.
