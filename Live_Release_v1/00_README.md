# Live Release v1 — production documentation package

> ## STATUS: PREPARED, NOT APPROVED
>
> This package exists so that **everything required for production is documented and
> in place**. It is **not** a statement that the system is ready.
>
> The definitive assessment is
> [PRODUCTION_READINESS_ASSESSMENT.md](PRODUCTION_READINESS_ASSESSMENT.md):
> **4 FAIL · 7 PASS WITH LIMITATION · 2 PASS** → **NOT APPROVED FOR LIVE TRADING.**
>
> Live execution remains technically disabled (`EXECUTION_ENABLED=false`, owner
> decision since 2026-07-13). Nothing in this package enables it.

Documentation completeness and production readiness are different things. This package
delivers the first. It records honestly that the second does not yet exist.

## Contents

| # | Document | Covers |
|---|---|---|
| 01 | [System Overview & Architecture](01_SYSTEM_OVERVIEW.md) | what the system is, component map, data flow, mode model |
| 02 | [Execution Engine](02_EXECUTION_ENGINE.md) | order path, safety gates, restart behaviour, state persistence |
| 03 | [Risk Engine](03_RISK_ENGINE.md) | sizing, stops, kill-switches, exposure caps |
| 04 | [Configuration Guide](04_CONFIGURATION_GUIDE.md) | every production setting, exchange preparation |
| 05 | [Deployment Guide](05_DEPLOYMENT_GUIDE.md) | gated promotion path to live |
| 06 | [Rollback Guide](06_ROLLBACK_GUIDE.md) | reverting a release or a mode |
| 07 | [Recovery Guide](07_RECOVERY_GUIDE.md) | recovery + disaster recovery |
| 08 | [Monitoring Guide](08_MONITORING_GUIDE.md) | heartbeat, supervisor, health checks, logging, alerting |
| 09 | [Emergency Procedures](09_EMERGENCY_PROCEDURES.md) | stop, flatten, isolate, escalate |
| 10 | [Operational Checklist](10_OPERATIONAL_CHECKLIST.md) | operator handbook, daily/weekly routine |
| 11 | [Known Risks](11_KNOWN_RISKS.md) | R1–R13 carried into this release |
| 12 | [Validation Summary](12_VALIDATION_SUMMARY.md) | what the forward-paper campaign proved |
| 13 | [Audit Summary](13_AUDIT_SUMMARY.md) | forensic close-out findings |
| 14 | [Release Notes](14_RELEASE_NOTES.md) | contents and identity of this release |
| — | [Production Readiness Assessment](PRODUCTION_READINESS_ASSESSMENT.md) | **the verdict** |

## Release identity

| | |
|---|---|
| Baseline tag | `rc1-forward-paper-validated` |
| Validated commit | `cda8187` (forward-paper run 2) |
| Documentation commit | `55b5c88` and later |
| Evidence | `validation_72h/archive/RC1_forward_paper_validation.tar.gz` sha256 `3b5bf715…0739` |
| Test suite | 339 passed, reproducible |

## The four blocking failures

| Category | Why it fails |
|---|---|
| Execution Engine (live) | the live order path has **never been executed** — 0 private calls, 0 orders, across the entire campaign |
| Supervisor | did not survive a host reboot; run stayed dead 7.82 h |
| Monitoring | observation loop stopped silently 22.7 h before the run ended; nothing noticed |
| Operational Maturity | 72 h reliability criterion not met — 20.56 h effective (28.5 %) |

Read [PRODUCTION_READINESS_ASSESSMENT.md](PRODUCTION_READINESS_ASSESSMENT.md) before
acting on anything else in this package.
