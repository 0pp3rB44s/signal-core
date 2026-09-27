# 14 — Live Release v1 — Release Notes

## Status

**PREPARED, NOT APPROVED.** This release assembles everything required to operate the
system in production. It does **not** authorise live trading, and the readiness
assessment does not support it.

| | |
|---|---|
| Package | `Live_Release_v1/` |
| Baseline tag | `rc1-forward-paper-validated` |
| Validated commit | `cda8187` (forward-paper run 2) |
| Test suite | 339 passed, reproducible |
| Readiness | **2 PASS · 7 PASS WITH LIMITATION · 4 FAIL** → NOT APPROVED |
| Live execution | disabled (`EXECUTION_ENABLED=false`) since 2026-07-13 |

## What this release contains

Fifteen documents covering system overview and architecture, execution engine, risk
engine, configuration and exchange preparation, deployment, rollback, recovery and
disaster recovery, monitoring/heartbeat/supervisor/logging/alerting, emergency
procedures, the operator handbook, known risks, validation and audit summaries, and the
production readiness assessment.

## What changed in the codebase

**Nothing.** This release is documentation and organisation only. No trading logic, no
configuration, no strategy, no risk and no execution code was modified. Verified: 339
tests still pass, and no file outside documentation was touched.

## Carried from RC1

Five defects were found and fixed *before* this release, each with a regression test:

| # | Defect |
|---|---|
| D1 | `ContractSpec` not JSON-serialisable — aborted every paper write for 11 days |
| D2 | Scan loop had no exception handling — a DNS blip killed the process for 20 h |
| D3 | Break-even stop booked profit at a price the market never traded |
| D4 | `granularity=1h` rejected by Bitget — 742 × HTTP 400171 |
| D5 | A failed scan published `scan_cycle_complete` as success |

## Known blockers

| ID | Blocker |
|---|---|
| R1 | No recovery after host restart (CRITICAL) |
| R2 | Host sleep suspends the process undetected (CRITICAL) |
| R3 | Nothing monitors the monitor (HIGH) |
| R4 | Heartbeat freshness has no live consumer (HIGH) |
| R5 | Live order path never executed (HIGH) |

Full register: [11_KNOWN_RISKS.md](11_KNOWN_RISKS.md).

## Honest position

The forward-paper lifecycle is demonstrably correct: safety was absolute across 9 892
requests, the event chain survived a 22-hour suspension and a SIGTERM intact, and every
closed trade reconciles to better than 1e-6. What is not demonstrated is that the system
can be left alone. The 72-hour reliability criterion was met to 28.5 %, and the three
failing categories — restart recovery, supervisor, monitoring — are precisely the ones
that matter when nobody is watching.

**There is still no proven edge.** The two closed paper trades in this release have no
statistical meaning and must never be cited as performance.
