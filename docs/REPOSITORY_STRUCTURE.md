# Repository structure — RC1

Orientation map of the repository as of Release Candidate 1 (2026-07-27). Describes
layout and intent only; it is not an architecture specification (see
`docs/BLUEPRINT.md`).

## Runtime path — market data to recorded outcome

| Stage | Module |
|---|---|
| Entry point | `app/main.py` → `app/runner.py` (`StartupRunner`) |
| Configuration | `app/config.py` (`Settings`, mode enforcement) |
| Market data | `data/market_fetcher.py`, `clients/` |
| Exchange clients | `clients/bitget_*_client.py`; **timeframe normalisation boundary: `clients/bitget_market_client.py:api_granularity()`** |
| Features | `market_features/engine.py` |
| Detectors | `strategies/` |
| Scoring / selection | `strategies/scoring.py`, selector in `app/runner.py` |
| Risk | `risk/risk_manager.py`, `risk/cooldown_manager.py` |
| Planning | `planning/trade_planner.py` |
| Forward paper | `forward_paper/service.py`, `forward_paper/store.py` |
| Execution (live) | `execution/` — **not exercised in RC1** |
| Telemetry | `telemetry/funnel.py`, `app/runtime_diagnostics.py` |
| Archiving | `archiving/` |

## Operational scripts

| Script | Purpose |
|---|---|
| `scripts/start_forward_paper.sh` | strict forward-paper launcher; enforces clean `main`, no duplicate processes, blank credentials, power assertion |
| `scripts/forward_paper_keepalive.sh` | health-check-driven supervisor; restarts only via the strict launcher; fail-closed after 3 restarts / 1800 s |
| `scripts/check_forward_paper.sh` | health check; emits `status=` and a typed exit code |
| `scripts/run_supervised.sh` | general supervisor (standard mode) |
| `scripts/start_bot.sh` | general launcher (env-driven) |
| `scripts/lib/power_assertion.sh` | holds a `caffeinate` assertion bound to a PID |
| `scripts/daily_ops_check.sh`, `scripts/healthcheck.sh` | routine operational checks |

## Validation campaign

```
validation_72h/
├── PREFLIGHT_REPORT.md      pre-flight gates + run-1 addendum   (tracked)
├── CLOSEOUT_REPORT.md       forensic close-out audit            (tracked)
├── run_env.sh               run configuration                   (tracked)
├── supervise.sh             supervisor entry point              (tracked)
├── monitor.sh, monitor_loop.sh  hourly observation              (tracked)
├── archive/
│   ├── README.md                                   archive index (tracked)
│   ├── RC1_forward_paper_validation.tar.gz         evidence bundle, 912 KB (tracked)
│   ├── RC1_forward_paper_validation.tar.gz.sha256  digest        (tracked)
│   └── RC1_forward_paper_validation/               expanded copy (ignored)
├── snapshots/               hourly monitor output               (ignored)
├── manifest.json            run identity                        (ignored)
├── invalidated_run_*/       run-1 evidence + INVALID.md         (verdict tracked)
└── closeout_*/              raw audit capture                   (ignored)
```

Runtime evidence is ignored in its expanded form and preserved as one checksummed
bundle, so history carries the evidence without carrying churn.

## Documentation

| Scope | Documents |
|---|---|
| Course / status | `MASTER_PLAN.md`, `PROJECT_STATUS.md`, `ROADMAP.md`, `CHANGELOG.md`, `RELEASE_NOTES.md` |
| Operations | `BEDIENING.md`, `DAILY_OPERATIONS.md`, `GO_LIVE_CHECKLIST.md`, `GO_LIVE_RUNBOOK.md` |
| Validation | `docs/FORWARD_PAPER_VALIDATION.md`, `validation_72h/*.md` |
| Reliability | `docs/RECOVERY_PROCEDURES.md`, `docs/MONITORING_GUIDE.md` |
| Risk / limits | `docs/RISK_REGISTER.md`, `docs/KNOWN_LIMITATIONS.md` |
| Subsystems | `docs/ARCHIVING.md`, `docs/FORWARD_PAPER_OBSERVABILITY.md`, `docs/CANDIDATE_LIFECYCLE.md`, `docs/FUNNEL_TELEMETRY.md`, `docs/UNIFIED_FEATURE_ENGINE.md` |
| Research | `docs/RESEARCH_JOURNAL.md`, `docs/STRATEGY_FUNNEL*.md` |
| Lifecycle map | `WORKING_BOT_EXECUTION_PATH.md` |

## Ignored by policy

`logs/`, `state/`, `data_store/`, `reports/`, `data/historical/`, `.venv/`, `*.pid`.
Generated runtime evidence is not committed except as a checksummed archive bundle.
