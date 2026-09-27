# MASTER STATUS REPORT — signal-core

**Audit date:** 2026-07-25 · **Auditor:** acting CTO / Lead Quant / Staff Eng / SRE
**Audited commit:** `6e57b2a` (main, clean, identical to `origin/main`)
**Method:** code, runtime, filesystem and git evidence only. Where documentation
and observed behaviour disagree, observed behaviour wins.

> **Supersedes** the status claims in `PROJECT_STATUS.md` (2026-07-18),
> `MASTER_PLAN.md` (2026-07-18) and `ROADMAP.md` (2026-07-18). Those documents
> describe an intended state that the machine does not exhibit.

---

## 0. VERDICT IN ONE PARAGRAPH

The platform is **not** in the state the documentation asserts. Three
independent, silent failures have been running for between five and seven days:
(1) the **forward-paper engine has written zero trades since 2026-07-19 22:45**
because a `ContractSpec` object leaks into a JSON payload and the writer
fails closed on every executable plan; (2) the **microstructure archive is
capturing ~2% of its nominal rate** because the host Mac sleeps, leaving roughly
**1.2 days of usable data after 7 calendar days**; (3) the **bot process died
20 hours ago** from an unhandled DNS error and nothing restarted it, because the
keepalive that was written, documented and merged has **never been executed once**.
Every health check reports PASS or OK through all three. The engineering quality
of the individual components is genuinely high; the quality of the *evidence
that they are working* is near zero. Phase B has not meaningfully started.

---

## PART 1 — REPOSITORY AUDIT

### 1.1 Scale and shape

| Metric | Value |
|---|---|
| Working tree | 4.1 GB |
| Tracked Python files / LOC | 189 / 38,807 |
| Test files / LOC | 40 / 4,578 |
| Local branches | 45 |
| Remote branches | 26 |
| Git worktrees | **18** (7 nested in `.claude/worktrees/`, 10 siblings, 1 primary) |
| Tags | 1 (`runner-v2026.07.19.1`) |
| Merged PRs | 12 |
| Last commit | 2026-07-19 (6 days of zero commits) |

Disk is dominated by artefacts, not code:

```
1.2G logs/     1.1G reports/    585M .venv/    565M .venv-archiver/
404M data/     126M .claude/    120M data_store/   42M .git/
```

Host volume is at **88% capacity, 24 GB free**. `ARCHIVE_RETENTION_DAYS`
defaults to **90**. At the *nominal* (correct) orderbook rate the archive
produces ~15 MB/day gzipped ≈ 1.35 GB per 90-day window — survivable, but only
because collection is currently broken. The disk guard trips at 5 GB.

### 1.2 Component classification

| Component | Class | Why |
|---|---|---|
| `app/` (runner, main) | **ACTIVE** | Owns scan loop; entrypoint for all runtimes |
| `market_features/engine.py` | **ACTIVE — DEFECTIVE** | Sole snapshot factory; injects a non-serializable object into `context` |
| `forward_paper/` | **ACTIVE — NON-FUNCTIONAL** | Runs, writes nothing; 0 events in 7 days |
| `candidate_lifecycle/`, `telemetry/funnel.py` | **COMPLETE** | 75,041 hash-chained events, chain valid, 0 dup ids |
| `archiving/` | **ACTIVE — STARVED** | Code correct; input starved by host sleep |
| `strategies/` (5 detectors) | **ACTIVE — NO EDGE** | Observation instruments only; all families rejected |
| `risk/`, `planning/`, `execution/` | **COMPLETE, DORMANT** | Mature from live phase; disabled since 2026-07-13 |
| `clients/` | **COMPLETE** | Public + private Bitget clients, rate limiter, TP/SL client |
| `scripts/` deploy chain | **PARTIAL — UNEXERCISED** | Written, tagged, CI-linted; **zero deployments performed** |
| `scripts/forward_paper_keepalive.sh` | **PARTIAL — NEVER RUN** | `logs/forward_paper_keepalive.log` does not exist |
| `tests/` | **PARTIAL** | 263 tests, 3 red locally, non-hermetic |
| `.github/workflows/ci.yml` | **ACTIVE — MISLEADING** | Green on a commit whose tests fail locally |
| `research/` (on main) | **PARTIAL** | Only 2 scripts tracked; H-4D-2/3 code lives on unmerged branches |
| `dashboard_v2/` | **DEPRECATED** | Not in critical path; retained by `LEGACY_MODULES.md` |
| `agents/`, `agents_v2/` | **DEPRECATED** | `agents/` marked "migrate"; v2 partially imported by runner |
| `agents_v3/` | **OBSOLETE (untracked)** | Not a repository module; 300 KB of local-only agent code |
| `backtesting/`, `scripts/run_optimizer.py` | **OBSOLETE** | No role in the evidence chain; optimizer contradicts pre-registration doctrine |
| `app/dashboard.py` | **OBSOLETE — SECURITY RISK** | Can mutate `.env`, no auth boundary; documented "do not run" |
| Root artefacts (`full_tree.txt`, `project_inventory.txt`, `python_bestanden.txt`, `mapstructuur.txt`, `0`) | **OBSOLETE** | 8.4 MB of stale generated junk, two empty files, tracked in git |
| Weekly data-quality report | **NOT STARTED** | ROADMAP deliverable; no script, no output in `reports/` |
| Forward-paper parity measurement | **NOT STARTED** | ROADMAP deliverable; impossible today (0 trades) |

### 1.3 Branch and worktree sprawl — HIGH severity

45 local branches against 12 merged PRs. **18 git worktrees** are registered,
seven of them **nested inside the repository** at `.claude/worktrees/`. This is
not cosmetic:

- `tests/test_legacy_inventory.py` fails **solely** because it scans the working
  tree and finds its own copies inside `.claude/worktrees/`. The repository's
  own test suite is being broken by the repository's own layout.
- `.claude/` is 126 MB of duplicated checkouts, and is **not** in `.gitignore`.
- Grep-based audits (including this one) return five copies of every hit.

The research history is worse: **H-4D-2 and H-4D-3 verdicts are recorded in
`CHANGELOG.md` on main, but the code and results that produced them exist only
on the unmerged branch `research/h4d2-time-of-day`.** Research on main is
therefore **not reproducible**, which directly violates gate A8 of
`GO_LIVE_CHECKLIST.md` ("independent reproduction from raw inputs is exact").

---

## PART 2 — ARCHITECTURE REVIEW

### 2.1 Actual data flow

```
                    ┌─────────────────────── PROCESS 1: bot (app.main) ────────────────────────┐
Bitget public REST  │                                                                          │
  contracts ────────┼─► market_fetcher.fetch_contracts  ◄── UNGUARDED: kills process on DNS    │
  candles  ─────────┼─► market_data_service.refresh_many ◄── guarded                           │
  merge-depth ──────┼─► LiveMarketContext(orderbook, htf, contract)                            │
                    │            │                                                             │
                    │            ▼                                                             │
                    │   market_features/engine.py:263  build_snapshot_from_inputs              │
                    │   context = {..., "instrument": ContractSpec}  ◄── POISON PILL           │
                    │            │                                                             │
                    │            ▼                                                             │
                    │   5 detectors → scoring → selector → risk_manager → trade_planner        │
                    │            │                              │                              │
                    │            ▼                              ▼                              │
                    │   telemetry/funnel.py            plans (EXECUTABLE | BLOCKED)             │
                    │   hash-chained, WORKING                   │                              │
                    │   75,041 events                           ▼                              │
                    │                            forward_paper/service.process                 │
                    │                            payload.strategy_features.market_context      │
                    │                                           │  = dict(snapshot.context)    │
                    │                                           ▼                              │
                    │                            forward_paper/store.py:30 json.dumps          │
                    │                                    ✗ TypeError                           │
                    │                                           │                              │
                    │                            runner.py:1392 catch → log → SWALLOW           │
                    │                            0 events written, scan continues              │
                    └──────────────────────────────────────────────────────────────────────────┘

                    ┌────────────────── PROCESS 2: archiver (archiving.run_archiver) ──────────┐
Bitget merge-depth ─┼─► orderbook_archiver  (10 s × 12 symbols = 1.2 req/s)                    │
Bitget fund-rate  ──┼─► funding_archiver    (300 s)                                            │
Bybit WS          ──┼─► liquidation_archiver (event-driven, 111 reconnects)                    │
                    │            └─► JSONL → daily rotate → gzip → 90d retention               │
                    │            └─► status.json every 30 s (lag-based health only)            │
                    └──────────────────────────────────────────────────────────────────────────┘
                                   ▲
                                   └── HOST SLEEPS. Both processes freeze. Health is blind.
```

### 2.2 Architectural weaknesses

**W1 — There is no supervision layer (CRITICAL).** Two long-running processes,
one host, no supervisor. `state/supervisor.stop` has existed since 2026-07-15.
`launchctl list` shows `com.cgc.tradingbot` loaded but not running,
`com.cgc.daily-learning-report` at exit status 1, `com.cgc.cleanlogs` at 78.
The documented continuity mechanism is `tmux new -s fp-keepalive …` — **`tmux`
is not installed on this machine.** The documented recovery path is
physically impossible to execute as written.

**W2 — Health signals measure liveness, never correctness (CRITICAL).** Every
check answers "is the process alive and is the file well-formed?" None answers
"is it producing what it is supposed to produce?" Result:
`daily_ops_check.sh` prints `data_orderbook PASS (2238 rijen, 0 dups)` on a day
whose nominal figure is **103,680 rows**. A 97.8% data loss renders as PASS.

**W3 — Fail-closed without fail-loud (CRITICAL).** `runner.py:1392` catches
every forward-paper exception, logs it, and continues. The comment is correct in
intent ("paper telemetry must never interrupt scans") but there is no counter,
no health degradation, no alert. A total functional outage looks identical to
normal operation from outside.

**W4 — The unified feature engine's parity claim is unenforced (HIGH).** PR #7
sold "field-exact parity, contract-tested." In reality
`tests/test_public_candidate_pipeline.py` constructs `MarketSnapshot(...)`
**by hand** with a hand-written `context` dict. Only
`tests/test_unified_feature_engine.py` calls the real factory, and it never
feeds the forward-paper writer. **No test crosses the seam between the
production snapshot factory and the paper store** — which is precisely where
the defect lives.

**W5 — Self-referential risk gating (HIGH).** `risk_manager` blocks strategies
on `clean_strategy_expectancy` computed from the system's own past trades. With
`momentum_breakout` at `trades=45, exp=-0.016`, the strategy is throttled to
PROBE size and symbols are kill-switched. In a research phase whose stated
purpose is to *observe* pipeline flow, the risk engine is suppressing the very
flow being observed — and it does so using live-phase history that the project
has already declared edgeless.

**W6 — Two Python runtimes on one host (MEDIUM).** Bot venv is **3.11**
(`/opt/homebrew/Cellar/python@3.11` in the crash trace); archiver venv is
**3.12**; `.python-version` contracts **3.12**; CI tests **3.12**. The process
that crashed was running an interpreter no CI job ever exercises.

**W7 — Liquidation source is cross-venue (MEDIUM, acknowledged).** Bitget has no
public liquidation channel, so liquidations come from **Bybit**. Any
liquidation-cascade hypothesis must carry an explicit cross-venue assumption.
This is correctly documented but not yet encoded in any pre-registration.

### 2.3 Unnecessary complexity

- Three generations of agents (`agents/`, `agents_v2/`, `agents_v3/`) with only
  `agents_v2/learning/coach_rules.py` genuinely on the runtime path.
- Two dashboards, one of which (`app/dashboard.py`) can rewrite `.env`.
- A backtest optimizer in a project whose doctrine forbids parameter
  optimisation without pre-registration.
- `app/runner.py` at **1,616 lines** orchestrating scan, fast lane, funnel,
  paper, execution, position sync and learning-refresh subprocess spawning.

### 2.4 Missing components

- Process supervision that survives reboot and host sleep.
- Data-completeness validation (expected vs actual volume).
- Alerting of any kind. There is no channel by which the system can tell a human
  it is broken.
- A parity harness comparing research assumptions to forward-paper reality
  (Phase D depends on it; it does not exist).
- Reproducible research entrypoints on `main`.

---

## PART 3 — CODEBASE HEALTH

### CRITICAL

**C1 — `ContractSpec` poisons every forward-paper write.**
`market_features/engine.py:263` sets `context={..., "instrument": inputs.contract}`.
`forward_paper/service.py:163` copies it: `"market_context": dict(snapshot.context)`.
`forward_paper/store.py:30` calls `json.dumps`. Reproduced directly:

```
REPRO CONFIRMED -> TypeError: Object of type ContractSpec is not JSON serializable
```

Impact: **zero forward-paper trades since 2026-07-19 22:45**, i.e. the entire
strict run. `data_store/forward_paper_events.jsonl` is 0 bytes;
`forward_paper_outcomes.csv` contains only a header;
`reports/forward_paper_data_quality.json` reports `trade_count: 0`,
`event_count: 0`, `complete_outcomes: 0`,
`historical_migration.status: NO_RELIABLE_FORWARD_PAPER_SOURCE_FOUND`.

**C2 — Unguarded network call kills the process.**
`app/runner.py:411` runs `while True: sleep(); self._scan_cycle()` with **no
exception handling**, while the position-monitor loop directly above it *does*
wrap each iteration ("Run one cycle without allowing a transient failure to kill
the loop"). Inside `_scan_cycle`, line 610 calls `fetch_contracts` **outside**
the try block that guards `refresh_many` — and `_is_network_resolution_error`,
the helper written for exactly this failure, is only consulted inside that
try block. A DNS blip on the line above the guard terminated the process on
2026-07-24 11:57:50 UTC after 431 cycles (`state/last_shutdown.json`:
`"reason": "uncaught_exception:BitgetRetryableError"`). An error class literally
named **Retryable** is not retried.

**C3 — No restart path is armed.** No crontab, no tmux (not installed), no
launchd job for the bot, `state/supervisor.stop` present since 07-15,
`logs/forward_paper_keepalive.log` **absent** — the keepalive has never run.
Downtime at audit: **~20 hours, undetected.**

**C4 — Host sleep silently destroys data collection.** `pmset -g`:
`sleep 1` (one-minute idle sleep), `powernap 1`, `tcpkeepalive 1`,
**767 sleep/wakes since boot**. Orderbook capture drops from 4,280 rows/hour
awake to 20–90 rows/hour asleep.

### HIGH

**H1 — Health checks lack volume floors.** `daily_ops_check.sh` validates row
*uniqueness* but never row *count* against expectation. `archiving/run_archiver.py:107`
marks a source DEGRADED only when `lag > 3× interval` — and Power Nap produces a
successful poll often enough to keep lag low. Health is structurally incapable
of detecting the actual failure mode.

**H2 — Tests are not hermetic.** `tests/test_public_candidate_pipeline.py`
uses `tmp_path`/`monkeypatch.chdir`, yet the risk manager still loads real
learning state: the failure log shows
`clean_strategy_expectancy (momentum_breakout, trades=45, exp=-0.016)` and
`kill-switch: symbol paused by expectancy (BTCUSDT)`. On CI (no history) the
plan is EXECUTABLE and the test passes; locally it is BLOCKED and the test
fails. **CI green is not evidence.**

**H3 — CI exercises a code path production never takes.** Because the test
fixture hand-builds `context`, CI never serializes a real `ContractSpec`. The
suite is green on the exact commit that has been silently broken in production
for six days.

**H4 — 3 failing tests on a clean `main`.** `test_legacy_inventory` (worktree
pollution) and two `test_public_candidate_pipeline` cases (state pollution).
Documentation asserts "260/260 green"; actual is **260 passed, 3 failed**.

**H5 — `app/runner.py` is a 1,616-line god object** with ~40 try/except sites
and inconsistent recovery semantics between loops.

### MEDIUM

**M1 — Live API credentials present on the host.** `.env` holds
`BITGET_API_KEY`, `BITGET_API_SECRET`, `BITGET_API_PASSPHRASE`, `OPENAI_API_KEY`,
`DASHBOARD_PASSWORD`. Correctly gitignored and absent from history (verified),
and MODE 1 blanks them at launch — but they are unnecessary for the current
mode and represent standing blast radius.
**M2 — Python version drift** (3.11 bot vs 3.12 contract/CI/archiver).
**M3 — Log hygiene.** `logs/forward_paper.out` is 105 MB; `logs/` is 1.2 GB;
`reports/` is 1.1 GB — 2.3 GB of untracked artefacts on a disk at 88%.
**M4 — `logs/watchdog.out` emits `getcwd: cannot access parent directories`
continuously** (last write 3 minutes before audit). A monitoring component has
been failing on every invocation, indefinitely, in a loop.
**M5 — Missing archive days.** `funding_settlements` has **no file at all** for
2026-07-21, 07-23 and 07-25 — hard gaps, not thin days.

### LOW

**L1 —** Stale root artefacts (8.4 MB) and two zero-byte tracked files (`0`,
`mapstructuur.txt`).
**L2 —** `.claude/` (126 MB) not gitignored.
**L3 —** Mixed Dutch/English across code comments, docs and log messages.
**L4 —** `daily_ops_check.sh` uses the fragile `A || B && C || D` idiom
(correct today, brittle under edit).

---

## PART 4 — TESTING REVIEW

| Dimension | State | Evidence |
|---|---|---|
| Unit tests | **GOOD** | 263 tests, 40 files, strong safety coverage (`test_execution_safety`, `test_position_lifecycle_safety`, `test_startup_safety`, `test_config_security`, `test_logging_security`) |
| Integration | **WEAK** | Fixtures bypass the production snapshot factory; the one seam that broke has no test |
| Hermeticity | **FAILING** | Real learning state and nested worktrees leak into tests |
| Runtime validation | **ABSENT** | Nothing asserts that a running system is producing output |
| CI | **PRESENT, MISLEADING** | Green on a commit broken in production for 6 days |
| Deployment validation | **NEVER EXECUTED** | Preflight/rollback scripts exist; zero runs |
| Failure testing | **PARTIAL** | `test_forward_paper_crash_resume` exists; no network-partition, no sleep/resume, no DNS-failure test |
| Recovery testing | **ABSENT** | Keepalive never invoked once |
| Rollback testing | **ABSENT** | Required by gate C2; not performed |
| Coverage measurement | **ABSENT** | No coverage tooling configured |

**Could this safely run for months?** No. It could not safely run for one week —
and demonstrably did not. Over the last seven days it accumulated three
concurrent silent failures and reported healthy throughout.

The three highest-value missing tests, in order:

1. **Serialization contract:** every `MarketSnapshot` produced by
   `build_snapshot_from_inputs` must survive `json.dumps`. One assertion would
   have prevented C1 entirely.
2. **Production-factory pipeline test:** drive the funnel with a snapshot from
   the real factory, assert exactly one `TRADE_OPENED`.
3. **Scan-loop resilience:** inject `BitgetRetryableError` into
   `fetch_contracts`, assert the loop survives N cycles.

---

## PART 5 — DEPLOYMENT REVIEW

**Not production-grade. It has never been executed.**

| Element | State |
|---|---|
| `bootstrap_mac.sh` | Written, arch-aware (arm64 `/opt/homebrew`, x86_64 `/usr/local`), CI-linted |
| `deploy_runner.sh` | Written; preflight + annotated-tag + backup-ref rollback |
| `verify_checkout.sh`, `verify_repository_hygiene.sh` | Working (`repository_hygiene=PASS`) |
| Tag `runner-v2026.07.19.1` | Created 2026-07-19 10:07, **pushed to origin** |
| **Actual deployments** | **ZERO** |
| Intel Runner | Not verifiable from this host; the archiver is running on **this** Mac (arm64 Homebrew path), so the documented migration did not happen |
| Rollback drill | Never performed (gate C2 fails) |
| Reboot recovery | Never tested (gate C6 fails) |
| Python | Bot 3.11 / contract 3.12 — drift unresolved |

The engineering is sound and the contracts (`docs/RUNNER_MIGRATION.md`) are
clear: GitHub never transports secrets, state or logs. But a deployment pipeline
that has never run is an untested code path, and the entire ROADMAP entry for
"MORGEN (2026-07-19)" — ten numbered steps — shows **zero** completed.

**Single point of failure:** one consumer laptop, which sleeps, runs the
research environment, the archiver, the bot and the developer's interactive
session simultaneously.

---

## PART 6 — RUNTIME REVIEW

### Observed state at 2026-07-25 07:47 UTC

| Subsystem | Reported | Actual |
|---|---|---|
| Forward paper | "ACTIVE since 07-18" | **DEAD since 2026-07-24 11:57 UTC** (~20 h) |
| Forward-paper output | "MODE 1 productive" | **0 events, 0 trades, 0 outcomes in 7 days** |
| Archiver | `status: OK` ×3 sources | Alive, capturing **~2%** of nominal |
| Heartbeat | freshness-checked | **71,790 s stale** |
| Keepalive | "available" | **never executed** |
| Supervisor | "adoption pending" | `state/supervisor.stop` present since 07-15 |
| Watchdog | notify-only | **erroring on every invocation** (`getcwd` failure) |
| Hash chain | valid | **genuinely valid** — 75,041 events, 0 dup ids, 0 violations |

### Failure modes observed

1. **Uncaught exception on transient DNS** → process death, no restart (C2/C3).
2. **Host sleep** → both processes freeze; health resumes as if nothing happened (C4).
3. **Serialization failure** → caught, logged, swallowed; no counter, no alert (C1/W3).
4. **574 incomplete funnel lifecycles** — consistent with candidates whose
   downstream paper linkage never materialised (the C1 symptom, visible in
   telemetry, never surfaced by any check).

### What is genuinely working

The funnel telemetry is the one subsystem that behaves exactly as advertised:
75,041 events, hash chain valid, zero duplicate event ids, zero lifecycle
violations, zero missing required fields. It is the only trustworthy evidence
source in the system, and it is good enough to have diagnosed this outage on
day one had anyone queried it.

---

## PART 7 — DATA PLATFORM REVIEW

### 7.1 Orderbook — the critical dataset

Nominal: 12 symbols × 8,640 snapshots/day (10 s) = **103,680 rows/day**.

| Date | Rows | % of nominal | Note |
|---|---:|---:|---|
| 2026-07-18 | 42,952 | 41% | partial day, started 14:27 |
| 2026-07-19 | 68,404 | 66% | **only substantially complete day** |
| 2026-07-20 | 2,265 | 2.2% | |
| 2026-07-21 | 1,513 | 1.5% | |
| 2026-07-22 | 2,599 | 2.5% | |
| 2026-07-23 | 1,712 | 1.7% | |
| 2026-07-24 | 2,229 | 2.2% | |
| 2026-07-25 | 2,255 | partial | 07:00 UTC hour alone = 1,349 (host awake) |

**Total: 123,929 rows ≈ 1.20 days-equivalent of full-rate data after 7 calendar
days of "24/7 collection."**

Hourly proof, 2026-07-23 (asleep) vs 2026-07-19 (awake): every hour on 07-23
returns 4–86 rows against a 4,280/hour baseline. Today: hours 00–06 UTC return
32–157 rows; hour 07, when the host woke, returns 1,349 and the live log shows
merge-depth calls at 1.2 req/s with `status=200` — exactly nominal. **The code
is correct; the host is asleep.**

### 7.2 Other sources

| Source | Total rows (7 d) | Assessment |
|---|---:|---|
| Funding | 4,281 | Same 96% collapse after 07-19 |
| Funding settlements | 1,236 | **Whole days missing: 07-21, 07-23, 07-25** |
| Liquidations (Bybit WS) | **766 events** | 545 on 07-19 alone; 5–29/day since; 111 reconnects |

### 7.3 Integrity

Positive: dedupe is working (0 duplicates across all sources, all days),
schema is rich and correct (`ts_utc`, `recv_ts_ms`, `exchange_ts_ms`,
`best_bid/ask`, `spread_bps`, 50-level depth), rotation and gzip function,
`seq_available: false` is honestly recorded.

Negative: **no lineage, no manifest, no completeness record.** Nothing in the
archive states what *should* have been collected, so a 98% shortfall is
indistinguishable from a quiet market. Gzip timestamps betray the pattern —
the 07-20 file was rotated on 07-21 13:08, the 07-21 file on 07-22 11:51 —
i.e. rotation fired whenever the host next woke.

### 7.4 How much more data is needed

Phase B exit criterion is **≥ 28 days per source with no gaps > 3× interval**.

- **Held today: ~1.2 days.** Days with zero gaps > 30 s: **at most one** (07-19,
  and even that has a degraded 21:00–23:00 tail).
- **Remaining at 100% capture: 27–28 more days.**
- **Remaining at the current 17% effective rate: ~164 calendar days (≈ 5.5 months).**

For liquidation-cascade research specifically, 766 raw events — of which ~690
come from 1.5 good days — is roughly two orders of magnitude short of what a
cascade event study needs. **Phase B has not started. Day 1 begins when
capture is fixed and verified.**

---

## PART 8 — RESEARCH REVIEW

### 8.1 The ledger

| Phase | Family | Verdict | Confidence |
|---|---|---|---|
| 2–3 | Strategy-level (scalp, momentum, continuation, reclaim) | **REJECTED** | High — 367 live trades, exchange truth |
| 4A | OHLCV directional | **REJECTED** | High — dev results inverted in replication |
| 4B | Funding / Open Interest | **BLOCKED → REJECTED** | Medium — historical OI unobtainable |
| 4C | Basis / mark-index divergence | **REJECTED** | High — 2 y synchronised, BH-corrected |
| 4D-1 | Orderbook imbalance @2.5 min | **REJECTED** | High — clean pre-reg, adequate power |
| 4D-2 | Time-of-day / session | **REJECTED** (0/30 BH-significant) | Medium — **not reproducible from main** |
| 4D-3 | VWAP deviation reversion | **REJECTED** (BH-p ≥ 0.26, wrong sign) | Medium — **not reproducible from main** |
| — | Sweep-and-reverse mechanised | **REJECTED** (≈ −0.3R everywhere) | High |

### 8.2 Assessment of methodology

This is the strongest part of the project, and it should be said plainly: the
research discipline is better than most funded teams achieve. H-4D-1's
pre-registration specifies mechanism, direction, DEV/REP split *by calendar
date before testing*, BH correction within family, economic thresholds
**above** cost, an explicit power calculation, named bias sources, and
pre-committed failure conditions. The mid-study amendment — discovering that
`market_context.csv` never populated orderbook columns and switching source
**before** any outcome inspection, documenting it in place — is exemplary
practice.

The conclusions are **valid and should not be revisited.** H-4D-1's finding
(+2.45 bps DEV → +1.15 bps REP at 15 m, against a 14 bps cost floor) is an
informative rejection at adequate power, not a power failure.

### 8.3 Research gaps

**G1 — Reproducibility is broken (blocks gate A8).** `main` carries only
`research/basis_data.py` and `research/h4d1_imbalance_study.py`. H-4D-2 and
H-4D-3 verdicts are asserted in `CHANGELOG.md` while their code sits on an
unmerged branch. `docs/RESEARCH_JOURNAL.md` on main still reads
*"H-4D-2 … Nog niet getest"* — the ledger contradicts the changelog **in the
same repository**.

**G2 — `reports/analysis/` is gitignored.** Raw run artefacts backing every
verdict are excluded from version control. The chain of evidence is one
`rm -rf` from gone.

**G3 — No pre-registrations written for Phase C.** MASTER_PLAN names three
priority families; zero registration documents exist. This work requires **no
data** and could have been done during the outage.

**G4 — Regime concentration.** Every rejection was measured in one regime
(chop/bearish grind, ~8 days for H-4D-1). Correctly flagged as residual
uncertainty; unresolved.

### 8.4 New hypotheses

**None are justified today.** Proposing hypotheses while holding 1.2 days of
data would be exactly the speculative research the mission forbids. The correct
research activity for the next two weeks is to **write pre-registrations**
(which require no data) and **fix collection** (which produces it).

---

## PART 9 — STRATEGY REVIEW

Five detectors are registered in `app/runner.py`, plus a fallback
`adaptive_momentum_continuation`.

| Strategy | Edge hypothesis | Live evidence | Forward paper | Verdict |
|---|---|---|---|---|
| `low_vol_reclaim` | Compression → reclaim continuation | 278 exchange trades, 91 W / 187 L (**24.5% WR**) | 286 candidates, **0 executable** | No edge; fee drag dominant |
| `momentum_breakout` | Range break + volume expansion | 48 trades, 23 W / 25 L | 134 candidates, **0 executable** | Expectancy −0.016; self-throttled |
| `momentum_breakdown` | Symmetric short | 30 trades, 10 W / 20 L | 81 candidates, 36 executable | Only detector reaching planner |
| `trend_continuation` | Pullback in trend | 14 trades, 3 W / 11 L | 43 candidates, **0 executable** | n too small; negative |
| `liquidity_sweep_reversal` | Stop-hunt reversal | 1 trade, 0 W / 1 L | 11 candidates, **0 executable** | Mechanised form rejected (≈ −0.3R) |
| `adaptive_momentum_continuation` | Fallback | 0 | 0 | Absent from funnel; unmeasured |

**Cross-cutting finding.** Four of five detectors convert **zero** candidates
into executable plans. `docs/STRATEGY_FUNNEL_FINDINGS.md` documents this, and
the reject analysis shows the dominant blockers are not market conditions but
the system's own state: *"Negative expectancy detected"* (21), *"expectancy-watch:
strategy weak but not hard-paused"* (15+), *"kill-switch: symbol paused by
expectancy"*. **The risk engine has learned from the edgeless live phase that
its own strategies are bad, and now suppresses them** — so forward paper cannot
observe them even if the serialization bug were fixed.

This matters more than it first appears: fixing C1 alone will **not** restore
meaningful forward-paper flow. `MASTER_PLAN`'s claim of "24 complete pipeline
passages in ~25 min" and low_vol_reclaim as "primary forward-paper workhorse"
is contradicted by the funnel: low_vol_reclaim produced **0 executable plans**.

**Can any of these go live?** No — and none should be worked on to that end.
They are correctly framed in `README.md` as observation instruments. Their
purpose is to exercise the pipeline, and for that purpose the expectancy
gating must be **neutralised in MODE 1** (not removed — bypassed under an
explicit forward-paper flag), or the pipeline cannot be observed at all.

---

## PART 10 — LIVE TRADING READINESS

**Can this system safely trade live? NO. Emphatically, and not close.**

Official gate count in `GO_LIVE_CHECKLIST.md` at 2026-07-18 was
*A 0/9 · B 0/5 · C 1/7 · D 0/5 · E 0/3*. **That count was wrong.** The single
claimed pass — C7, "archiver data continuity intact" — is falsified by Part 7.

**Corrected standing: A 0/9 · B 0/5 · C 0/7 · D 0/5 · E 0/3 = 0 of 24.**

### Blockers by class

**Statistical (absolute, blocks everything else)**
1. No proven edge. Every family tested is rejected.
2. No `EDGE_ACCEPTANCE_REPORT` — no candidate exists to write one about.
3. Insufficient data: 1.2 days of 28 required.
4. Research on main is not reproducible (A8 fails structurally).
5. All rejections come from a single market regime.

**Software**
6. Forward-paper writer non-functional (C1).
7. Scan loop dies on transient network errors (C2).
8. Three tests red on main; suite non-hermetic (H2/H4).
9. CI validates a path production does not take (H3).
10. Production feature-factory → paper-store seam untested (W4).

**Statistical validation infrastructure**
11. Zero forward-paper trades ever recorded ⇒ B1–B5 unreachable.
12. No parity harness (B4 requires one; it does not exist).
13. Risk engine suppresses 4 of 5 detectors ⇒ no flow to validate (W5).

**Operational**
14. No supervision; no automatic restart (C3).
15. Documented recovery requires `tmux`, which is not installed.
16. Host sleeps; collection halts (C4).
17. Watchdog broken on every invocation (M4).
18. `daily_ops_check` has never been run 14 consecutive days (C3 gate).

**Risk / configuration**
19. `ACCOUNT_EQUITY_USDT` fallback stale (equity 56.64 observed in test run).
20. Kill-switch / daily-loss-cap / weekly-freeze never exercised on a runner.
21. Fee-drag re-analysis impossible without a candidate.

**Monitoring**
22. No alerting channel of any kind.
23. Health checks cannot detect functional failure (H1/W2).
24. Fail-closed paths are silent (W3).

**Deployment / recovery**
25. Zero deployments performed; rollback never drilled (C2).
26. Reboot recovery never tested (C6).
27. Python version drift between runtime and CI (M2).

**Data**
28. 98% shortfall; whole days missing for funding settlements.
29. No completeness manifest or lineage.
30. Cross-venue liquidation assumption not yet encoded in any registration.

### Honest framing

The distance to live is **not** primarily a software distance. Even with every
software blocker fixed tomorrow, the project would still need ~28 days of clean
collection, then a 2–6 week pre-registered research cycle that will probably
also reject, then 4 weeks of candidate forward paper, then limited live. The
software blockers matter because they are currently **consuming that calendar
without producing anything**.

---

## PART 11 — RISK REVIEW

**R1 — Silent-failure risk (REALISED, CRITICAL).** Three concurrent failures ran
5–7 days while every indicator read healthy. This is the defining risk of the
project, and it has already fired. It is more dangerous than any market risk,
because it corrupts the evidence base on which every future decision rests.

**R2 — False confidence from documentation (REALISED, CRITICAL).** `MASTER_PLAN.md`
states Phase B is "ACTIEF, dag 1 van ~28-56" and Phase A is "AFGEROND" with
"260/260 tests groen". A reader — including the owner returning after a week,
and including any future AI agent given these files as context — would conclude
the project is on schedule. It is not. Documentation asserting a state that
instrumentation does not verify is an active hazard.

**R3 — Time risk (HIGH).** Six days of zero commits, ~1.2 days of data captured.
The scarce resource is calendar, and it is being spent at ~17% efficiency.

**R4 — Single-host infrastructure risk (HIGH).** One consumer laptop runs
collection, execution, research and interactive development. It sleeps, it is
carried between locations, its DNS is intermittent (347 `NameResolutionError`
occurrences in the archiver log alone).

**R5 — Evidence-chain fragility (HIGH).** `reports/analysis/` is gitignored;
research code for two rejected families exists only on an unmerged branch;
`logs/` and `reports/` total 2.3 GB on a disk at 88%. The proof behind the
verdicts is not durable.

**R6 — Overfitting / multiple-testing pressure (MEDIUM, well-managed).** Eight
families rejected creates real pressure to loosen criteria. Current mitigations
(pre-registration before data, BH within family, permanent ledger, explicit
no-recycling rule) are strong. The risk is not the method — it is that after
another rejection someone will "just look" at the data first.

**R7 — Data leakage (LOW, well-managed).** H-4D-1 correctly used the open of the
first fully-subsequent candle and pre-committed the DEV/REP split by date.

**R8 — Self-referential learning (MEDIUM).** Risk gating derived from the
system's own edgeless trading history now suppresses the pipeline. Left
unaddressed, the system will converge on trading nothing and call it discipline.

**R9 — Exchange dependency (MEDIUM).** Liquidations come from Bybit, not Bitget.
Any cascade edge found is a cross-venue inference.

**R10 — Human-error / key-person risk (MEDIUM).** All operations are manual,
undocumented in execution (no ops logbook exists despite gate C3 requiring 14
days of it), and dependent on one person remembering to run scripts.

**R11 — Credential blast radius (MEDIUM).** Live trading keys sit in `.env` on a
laptop for a system that is explicitly not permitted to trade.

**R12 — Unknown unknowns.** The instrumentation gap is the unknown-unknown
generator. Until the system can prove it is producing expected output, assume
further undetected failures exist. Given three found in one audit, the prior
should be high.

---

## PART 12 — DOCUMENTATION REVIEW

The documentation set is unusually well-structured — modes, gates, runbooks,
incident classes, a permanent research ledger. The *architecture* of the
documentation is a genuine asset. Its *accuracy* is the problem.

| Document | Verdict | Specific contradiction |
|---|---|---|
| `MASTER_PLAN.md` | **MATERIALLY FALSE** | "Phase B ACTIVE day 1 of 28-56" (1.2 days held); "Phase A COMPLETE, 260/260 green" (3 red); "low_vol_reclaim primary workhorse, 24 pipeline passages" (0 executable plans) |
| `PROJECT_STATUS.md` | **MATERIALLY FALSE** | "archiver runs 24/7" (2% capture); "Phase 5 ACTIVE since 07-18, strict-mode HEALTHY" (0 trades, process dead 20 h); "forward-paper runtime production-ready" |
| `ROADMAP.md` | **STALE** | Entire "MORGEN (2026-07-19)" 10-step runner plan: 0 done. "This week" items: 0 done |
| `GO_LIVE_CHECKLIST.md` | **STRUCTURALLY EXCELLENT, COUNT WRONG** | C7 claimed PASS; falsified. True score 0/24 |
| `GO_LIVE_RUNBOOK.md` | **SOUND** | Mode model is correct and worth keeping |
| `DAILY_OPERATIONS.md` | **ASPIRATIONAL** | Describes a cadence never executed; no ops logbook exists |
| `BEDIENING.md` | **PARTLY IMPOSSIBLE** | Instructs `tmux new -s fp-keepalive …`; tmux not installed |
| `CHANGELOG.md` | **ACCURATE BUT ORPHANED** | Records H-4D-2/3 verdicts whose code is not on main |
| `docs/RESEARCH_JOURNAL.md` | **INTERNALLY CONTRADICTORY** | Says H-4D-2 "Nog niet getest"; CHANGELOG says rejected |
| `docs/JOURNAL.md` | **STALE** | Last substantive entry 2026-07-16 |
| `README.md` | **MOSTLY ACCURATE** | Honest about "no edge"; overstates operational reality |
| `AGENTS.md` | **SOUND, UNENFORCED** | "Always run tests before proposing merge" — 3 are red |
| `docs/LEGACY_MODULES.md` | **ACCURATE** | Best-maintained document in the set |

**Root cause of the drift:** documentation is written from *intent at time of
writing* and never re-validated against instrumentation. The fix is not more
documentation — it is to make the status document **generated** from measured
values, so it cannot assert a state the system does not exhibit.

---

## PART 13 — WHAT WOULD YOU CHANGE?

Inheriting this today, in priority order:

**Reverse: "the platform is complete, now we collect data."** Phase A was
declared complete on the strength of merged PRs and a green CI run, not on
evidence that the platform produces correct output under real conditions. It
produced zero forward-paper trades and 2% of its data. **A platform is complete
when it has demonstrated N consecutive days of verified expected output — not
when its tests pass.**

**Rebuild: the health and observability layer.** Every check must compare
*actual vs expected volume*, not just liveness and well-formedness. Health must
degrade when output stops. This is roughly two days of work and it is the
highest-leverage two days available.

**Rebuild: process supervision.** launchd with `KeepAlive`, `RunAtLoad`, and a
`caffeinate`-backed power assertion — not tmux, not a manually-invoked
keepalive. If the platform cannot survive a lid close, it cannot survive a month.

**Simplify: delete the risk engine's authority in MODE 1.** Expectancy gating,
kill-switches and probe-sizing are correct for live capital and actively harmful
for observation. Gate them behind the mode flag.

**Delete outright:** `agents/`, `agents_v3/` (untracked), `app/dashboard.py`
(can rewrite `.env`), `scripts/run_optimizer.py` and `backtesting/`
(contradicts pre-registration doctrine), all root artefacts, 14 of 18 worktrees,
~35 of 45 branches. This is roughly 130 MB of nested worktrees and several
thousand lines whose only current function is to slow down navigation and break
`test_legacy_inventory`.

**Redesign: `app/runner.py`.** 1,616 lines, ~40 exception sites, inconsistent
recovery semantics between two loops in the same class. Extract the scan loop
into a supervised iteration primitive with uniform error handling — the
position monitor already demonstrates the right pattern 20 lines above the
place where it is missing.

**Redesign: research artefact storage.** Un-ignore `reports/analysis/`, or
commit result JSONs alongside each verdict. A rejection you cannot reproduce is
an opinion.

**Reverse: single-host operation.** Either complete the Intel runner migration
(the scripts are written and the tag is pushed — this is hours of work, not
days) or accept that Phase B runs on a machine that sleeps and stop calling it
24/7.

**Keep, and protect:** the funnel telemetry (the only trustworthy component),
the research protocol, the mode model in `GO_LIVE_RUNBOOK.md`, the gate
structure in `GO_LIVE_CHECKLIST.md`, and the safety test suite.

---

## PART 14 — PROJECT MATURITY

Percentages are against *"demonstrably fit for a statistically defensible live
deployment"*, not against "code written."

| Area | % | Justification |
|---|---:|---|
| Architecture | **70** | Clean subsystem separation, sound mode model, AST-guarded archiver isolation. Penalised for the 1,616-line runner, an unenforced parity claim, and an untested production seam |
| Infrastructure | **40** | CI works; deploy scripts and tag exist. No supervision, no alerting, one sleeping host, zero deployments |
| Data Platform | **20** | Collector code is correct and dedupe/rotation/retention work. 1.2 of 28 days held; no completeness validation; whole days missing |
| Research Platform | **45** | Protocol is top-decile. Not reproducible from main; artefacts gitignored; two verdicts stranded on a branch |
| Feature Engineering | **60** | Unified engine exists and is contract-tested in isolation — but emits a non-serializable object that breaks the consumer, and no test crosses that seam |
| Signal Generation | **55** | Five detectors, richly instrumented, deterministic ids. No edge; 4 of 5 produce zero executable plans |
| Risk Engine | **70** | Comprehensive and genuinely well-tested (kill-switches, caps, freeze, exposure). Penalised for self-referential gating that suppresses observation |
| Execution Engine | **65** | Mature from the live phase (TP/SL lifecycle, reconciler, maker entry). Dormant since 07-13, unexercised, one known unmerged fix outstanding |
| Forward Paper | **10** | Architecturally complete, functionally dead. 0 trades in 7 days. Its output is the input to Phase D |
| Monitoring | **20** | Checks exist and are cheap to run, but were blind to all three live failures. Watchdog broken; no alerting |
| Deployment | **30** | Preflight, annotated tags, backup-refs, rollback — all written, none executed. Untested code |
| Operations | **20** | Documented in detail, executed almost never. Keepalive: 0 runs. Ops logbook: absent. Recovery path requires absent software |
| Validation | **15** | 263 tests, but non-hermetic, CI-green-while-broken, no coverage measurement, no runtime validation, no rollback/recovery drills |
| Documentation | **45** | Excellent structure and discipline; materially false on current state in the three most load-bearing documents |
| Live Readiness | **0** | 0 of 24 gates. Previously reported 1/24 was incorrect |
| **Overall Project** | **~30** | Weighted toward what gates live: evidence, data and validated operation — all of which are the weakest areas |

The gap between "code written" (which would score ~70) and "evidence produced"
(~15) **is** the project's actual status. Everything built is real; almost
nothing built has been shown to work in the conditions it must work in.

---

## PART 15 — EXECUTIVE SUMMARY

### 1. Where are we today?

We have a well-engineered platform that is **not producing evidence**. Phase A
was declared complete on merged PRs and a green CI run; Phase B was declared
active on 2026-07-18. In reality, since 2026-07-19 the forward-paper engine has
written **zero** trades, the archive has captured **~2%** of its nominal rate,
and since 2026-07-24 11:57 UTC the bot has been **dead**. We hold **1.2
days-equivalent** of the 28 days Phase B requires. Every monitor reported
healthy throughout. The correct statement of position is: **Phase B has not
started; the calendar has been running without it.**

### 2. The single largest bottleneck

**The absence of output verification.** Not the missing edge — that is the
long-term problem and it is honestly acknowledged. The bottleneck is that
nothing in the system compares *what was produced* to *what should have been
produced*. That single gap is why three independent failures survived a week
undetected, why the documentation is materially false, and why the last seven
days produced roughly one day of data and zero paper trades. Every downstream
phase consumes output this system is currently not generating.

### 3. Our greatest strength

**Research discipline, and the funnel telemetry that could support it.** The
pre-registration protocol — mechanism and direction fixed before data, DEV/REP
split by calendar date, BH correction within family, economic thresholds set
above cost, power stated up front, failure conditions pre-committed, the
mid-study source amendment documented before any outcome inspection — is better
than most funded quant teams achieve. Eight rejected families with intact
integrity is a real asset, not a failure. The hash-chained funnel (75,041 events,
chain valid, zero duplicates, zero violations) is the one component that does
exactly what it claims.

### 4. Our greatest weakness

**The project trusts its own documentation over its own instrumentation.**
Status is asserted from intent at time of writing and never re-derived from
measurement. This produced a MASTER_PLAN describing a Phase B that was not
happening, a PROJECT_STATUS describing a 24/7 archiver capturing 2%, and a
live-gate checklist crediting a passed gate that was falsified. Left unfixed,
every future decision — including a go-live decision — rests on assertions
nobody has verified.

### 5. What should NEVER be worked on right now

- **Any new strategy, detector, or parameter change.** There is no edge and no
  mechanism to measure one.
- **Any new research hypothesis.** With 1.2 days of data, proposing hypotheses
  is speculation the mission explicitly forbids.
- **Backtesting or the optimizer.** They contradict pre-registration doctrine.
- **Dashboards, agents_v3, UI, ML models.** Zero contribution to the evidence chain.
- **Anything touching live execution.** It is correctly off; leave it off.
- **Refactoring `app/runner.py` for elegance.** Fix the two specific defects; do
  not open a 1,616-line rewrite while collection is broken.

### 6. What MUST be worked on immediately

In strict order — each item is a precondition for the next being meaningful:

1. **Stop the data loss** (power management + supervision). Every hour of delay
   permanently costs an hour of irreplaceable microstructure data.
2. **Fix the `ContractSpec` serialization defect** and its regression test.
3. **Give health checks volume floors** so that "PASS" means "producing expected
   output," not "process alive."
4. **Arm automatic restart** via launchd, not tmux.
5. **Neutralise expectancy gating in MODE 1** so the pipeline can actually be
   observed.
6. **Correct the documentation** to measured reality and make status generated,
   not asserted.

### 7. How many months until a statistically defensible live deployment could be considered?

**Best case: 5–6 months. Realistic: 9–14 months. Most likely outcome: no live
deployment, because no edge survives.**

| Stage | Duration | Assumption |
|---|---|---|
| Platform repair + verified collection | 2 weeks | Fixes land and are proven by instrumentation |
| Phase B: 28 days clean data | 4 weeks | Runs only after capture is verified — day 1 is ~2026-08-08 |
| Phase C: pre-registered cycle 1 | 2–6 weeks | Prior probability of PASS: **low** |
| Phase C: cycles 2–3 if rejected | +4–12 weeks | Historically 8 of 8 families rejected |
| Phase D: candidate forward paper | 4–6 weeks | Only if C passes |
| Phase E: limited live | 4–8 weeks | Only after full gate + explicit authorisation |

The honest framing: **the calendar is dominated by data collection and by the
probability that the answer is "no edge."** Eight families have been rejected
with good methodology. The professional expectation is that the microstructure
families may also reject — and the project's greatest achievement so far is
that it is built to say so out loud rather than to trade anyway.

---

## PART 16 — NEW MASTER ROADMAP

The previous roadmap is void: its "tomorrow" (2026-07-19) never happened, and
its "this week" assumed collection that was not occurring. Replacement below.
**Phase numbering restarts to avoid confusion with the old A–G scheme.**

---

### PHASE 0 — TRUTH & TRIAGE (2 weeks · 2026-07-25 → 2026-08-08)

**Objective.** Make the platform demonstrably produce what it claims, and make
its status impossible to overstate.

**Deliverables.** ContractSpec fix + serialization regression test; scan-loop
resilience; launchd supervision with power assertion; volume-floor health
checks; MODE 1 expectancy bypass; corrected documentation; repository cleanup;
generated status report.

**Acceptance criteria.**
- Orderbook capture ≥ **95%** of nominal (≥ 98,500 rows/day) for **7 consecutive days**.
- Forward paper writes **≥ 1 TRADE_OPENED per 24 h** for 7 consecutive days.
- Bot uptime **≥ 7 days** with no manual intervention, across ≥ 1 forced reboot.
- `daily_ops_check.sh` exits 0 for 7 consecutive days, **and** fails when
  injected with a 50% volume shortfall.
- Test suite green on a clean checkout **and** on this host.

**Dependencies.** None. This phase is unblocked and is the only unblocked work.

**Risks.** Fixing C1 alone does not restore flow (W5 must also be addressed);
host may be unsuitable for 24/7 regardless (mitigation: complete runner migration).

**Effort.** ~7 engineering days across 14 calendar days.

**Definition of Done.** Seven consecutive days where every acceptance metric is
met **and machine-verified**, with the evidence written to `reports/`.

---

### PHASE 1 — VERIFIED COLLECTION (4 weeks · 2026-08-08 → 2026-09-05)

**Objective.** Accumulate the 28-day microstructure dataset Phase C requires.

**Deliverables.** 28 days × 3 sources at ≥ 95% coverage; weekly automated
data-quality report; per-day completeness manifest with hashes; retention and
disk trend validated; **≥ 2 written pre-registrations** (liquidation-cascade
dynamics; 10 s orderbook imbalance) committed **before any outcome inspection**.

**Acceptance criteria.**
- ≥ 28 days per source, no gap > 3× interval, ≥ 95% of nominal rows per day.
- Zero whole-day gaps (the funding-settlement failures of 07-21/23/25 must not recur).
- Weekly report generated automatically, 4 consecutive weeks, no manual steps.
- 2 pre-registrations committed with power analyses, timestamped before results.

**Dependencies.** Phase 0 Definition of Done. **Do not start counting before it.**

**Risks.** Host instability (mitigation: runner migration during this phase, at
low cost since the tag and scripts already exist); regime concentration
(mitigation: record the regime, do not extend indefinitely chasing variety).

**Effort.** ~4 days of engineering; the rest is calendar. **Pre-registration
writing is the primary intellectual work of this phase and requires no data.**

**Definition of Done.** 28 days of verified data + 2 committed pre-registrations.

---

### PHASE 2 — RESEARCH ON OWN DATA (2–6 weeks per cycle · from 2026-09-05)

**Objective.** Execute pre-registered cycles until one candidate clears every
gate, or the families are honestly exhausted.

**Deliverables per cycle.** Execution exactly as registered; results JSON
**committed** (not gitignored); falsification battery on any positive; verdict
in `docs/RESEARCH_JOURNAL.md`; on PASS, an `EDGE_ACCEPTANCE_REPORT`.

**Acceptance criteria.** Gates A1–A9 of `GO_LIVE_CHECKLIST.md`, unmodified.

**Dependencies.** Phase 1 DoD. Research scripts must run **from `main`** —
resolve the stranded H-4D-2/3 branch before this phase begins.

**Risks.** Multiple-testing accumulation across cycles (mitigation: BH per
family, limited families per data window, everything in the ledger);
post-rejection pressure to loosen criteria (mitigation: criteria are already
committed in writing and must not be edited after seeing data).

**Effort.** 5–10 days per cycle.

**Definition of Done.** `EDGE_ACCEPTANCE_REPORT` all-PASS, **or** a documented
rejection and an explicit decision to continue or stop.

---

### PHASE 3 — CANDIDATE FORWARD PAPER (4–6 weeks)

**Objective.** Prove the accepted edge survives execution reality.

**Deliverables.** Strategy implemented with minimum parameters, **frozen** with
a config hash and pinned commit before start; full signal→plan→forward test
coverage; parity report (signal time vs executable time, assumed vs observed
spread and slippage, fill assumptions).

**Acceptance criteria.** Gates B1–B5. Result inside the pre-registered
expectation band; forward paper demonstrably **not** more favourable than the
exchange; zero unexplained stops.

**Dependencies.** Phase 2 PASS. Also requires the parity harness, which **does
not exist and must be built** — schedule it in Phase 1 or early Phase 2.

**Risks.** Silent regime shift (mitigation: monthly decomposition);
mid-run tuning (**forbidden** without re-registration and restart).

**Effort.** 5 days implementation, 4–6 weeks observation.

**Definition of Done.** ≥ 4 weeks, pre-determined n reached, deviation inside
registered tolerance.

---

### PHASE 4 — OPERATIONAL HARDENING FOR LIVE (2 weeks, parallel with Phase 3)

**Objective.** Close every C and D gate before capital is discussed.

**Deliverables.** Runner deployed from an annotated tag; rollback drilled
(deploy → rollback → deploy); 14 consecutive days of `daily_ops_check` PASS in
a written ops logbook; kill-switch, daily-loss cap, weekly freeze and exposure
caps exercised **on the runner**; reboot recovery proven; risk config rebuilt
from current equity; fee-drag analysis for the specific candidate; credential
rotation procedure executed once.

**Acceptance criteria.** C1–C7 and D1–D5 all objectively PASS with archived evidence.

**Dependencies.** Phase 0 supervision work; runs in parallel with Phase 3.

**Risks.** Treating this as paperwork. **Every gate here failed silently once
already**; each requires a real drill, not a checkbox.

**Effort.** ~8 engineering days.

**Definition of Done.** 24 of 24 gates PASS except E (authorisation).

---

### PHASE 5 — LIMITED LIVE (4–8 weeks)

**Objective.** Measure real fills and costs at the smallest meaningful size.

**Preconditions.** All of A–D PASS **plus** separate, explicit, written owner
authorisation. No exceptions, no partial starts.

**Deliverables.** Exchange-minimum sizing, one symbol cluster, max 1 position,
isolated margin, fixed max leverage; live-vs-paper parity monitor; first trade
manually supervised end-to-end (fill → immediate SL/TP → reconciliation).

**Acceptance criteria.** ≥ 4 weeks inside the expectation band; measured fee
drag below registered margin; every safety event handled correctly.

**Risks.** Execution reality worse than paper — this is the *expected* finding
and the reason probe size exists.

**Definition of Done.** 4 weeks inside band, zero critical incidents.

---

### PHASE 6 — PRODUCTION LIVE & CONTROLLED SCALING (from Phase 5 exit)

Unchanged in substance from the existing `MASTER_PLAN.md` Phases F and G, which
are sound: stepwise scaling (exchange minimum → small fixed risk fraction →
limited expansion → production allocation), each step requiring minimum
duration, minimum trades, a band check and separate authorisation. Never scale
after a winning streak or to recover a loss. Monthly re-testing of the edge;
drawdown limits never relaxed without new evidence.

---

## PART 17 — EXECUTION PLAN, NEXT 14 DAYS

Ordered exactly as it must be executed. **T1–T3 are the same day**, because
every hour of delay costs an irreplaceable hour of microstructure data.
All work follows `AGENTS.md`: branch, small reviewable patch, tests, PR.

---

### T1 — Stop the data loss (power management)

- **Objective.** Prevent the host from sleeping while collection runs.
- **Why it matters.** This is the single largest source of loss in the project:
  98% of nominal data, ~5.8 days of the last 7. No other task recovers more value.
- **Files.** `scripts/start_archiver.sh`, `scripts/start_forward_paper.sh`,
  new `scripts/lib/power_assertion.sh`; document in `BEDIENING.md`.
- **Expected output.** Both launchers acquire a `caffeinate -dimsu -w $PID`
  assertion bound to the child's lifetime, so the assertion dies with the process.
- **Acceptance criteria.** `pmset -g assertions` shows `PreventUserIdleSystemSleep`
  held while either process runs; orderbook rows for a full lid-closed night
  ≥ 95% of nominal (≥ 4,100/hour).
- **Validation.** Start archiver, close lid 60 min, reopen, count rows per hour
  for that window. Must be ≥ 4,100/hour, not 20–90.
- **Rollback.** Remove the assertion call; behaviour returns to today's.
- **Risk.** LOW. Battery drain and thermals on a laptop; note it, accept it.
- **Duration.** 2 hours.
- **Impact.** **VERY HIGH** — converts collection from 2% to ~100%.

---

### T2 — Fix the `ContractSpec` serialization defect

- **Objective.** Make forward paper write trades again.
- **Why it matters.** Phase 5 has produced zero output for 7 days. Every
  downstream gate (B1–B5, Phase 3) consumes this output.
- **Files.** `market_features/engine.py:263`, `forward_paper/service.py:163`,
  `forward_paper/store.py:30`, `tests/test_unified_feature_engine.py`,
  new `tests/test_snapshot_serialization_contract.py`.
- **Expected output.** Two independent defences: (a) the engine stores a plain
  dict for `"instrument"` (`dataclasses.asdict`, minus the `raw` blob) rather
  than the `ContractSpec` object; (b) `store._json` gets a `default=` fallback
  so no future object can silently kill a write. **Both** — (a) fixes this bug,
  (b) prevents the class of bug.
- **Acceptance criteria.** `json.dumps` succeeds for every snapshot from
  `build_snapshot_from_inputs`; a live scan producing an EXECUTABLE plan yields
  exactly one `TRADE_OPENED`; `data_store/forward_paper_events.jsonl` is non-empty.
- **Validation.** Run the new contract test; then run one real scan cycle and
  assert `grep -c FORWARD_PAPER_FAILED_CLOSED` on new log output is 0.
- **Rollback.** Revert the commit; system returns to today's (broken) state — no
  data is at risk because none is being written.
- **Risk.** LOW-MEDIUM. Changing snapshot context shape could affect consumers
  that read `context["instrument"]`; grep before changing.
- **Duration.** 3 hours.
- **Impact.** **VERY HIGH** — unblocks all forward-paper evidence.

---

### T3 — Make the scan loop survive transient network failure

- **Objective.** Stop the process dying on a DNS blip.
- **Why it matters.** Caused a 20-hour undetected outage; 347
  `NameResolutionError` occurrences show this host's DNS is genuinely flaky.
- **Files.** `app/runner.py` (lines ~408–413 loop, ~610 `fetch_contracts`),
  new `tests/test_scan_loop_resilience.py`.
- **Expected output.** A `_scan_cycle_iteration()` wrapper mirroring the
  existing `_position_monitor_iteration()` pattern; `fetch_contracts` moved
  inside the guarded block that already consults `_is_network_resolution_error`;
  consecutive-failure counter that marks the runtime UNHEALTHY after N failures
  rather than exiting.
- **Acceptance criteria.** Injecting `BitgetRetryableError` into
  `fetch_contracts` for 5 consecutive cycles leaves the process alive and the
  heartbeat advancing; the 6th succeeds and normal operation resumes.
- **Validation.** New unit test with a patched fetcher; plus a live test using
  `networksetup` to drop DNS for 60 s and confirming survival.
- **Rollback.** Revert; behaviour returns to fail-fast.
- **Risk.** MEDIUM. Swallowing errors can mask real faults — mitigated by the
  failure counter and by T4's volume floors, which will catch a wedged loop.
- **Duration.** 3 hours.
- **Impact.** **HIGH** — removes the most common cause of unplanned downtime.

---

### T4 — Volume floors in every health check

- **Objective.** Make "PASS" mean "producing expected output."
- **Why it matters.** This is the root cause of the audit. All three failures
  were individually cheap to fix and expensive only because nothing noticed.
- **Files.** `scripts/daily_ops_check.sh`, `archiving/run_archiver.py` (health
  computation), `scripts/check_forward_paper.sh`, `tests/test_ops_scripts.py`.
- **Expected output.** Expected-row computation from config
  (`symbols × 86400 / interval`, prorated for partial days); FAIL below 90%,
  WARN below 98%. Forward-paper check FAILs when zero `TRADE_OPENED` in 24 h
  while EXECUTABLE plans exist. Archiver status becomes DEGRADED on volume
  shortfall, not only on lag.
- **Acceptance criteria.** Running the check against 2026-07-23's archive
  (1,712 rows) **must FAIL**. Against 2026-07-19 (68,404) it must PASS. This is
  a regression test against real historical data.
- **Validation.** Point the check at each archived day and assert the verdicts.
- **Rollback.** Revert; checks return to liveness-only.
- **Risk.** LOW. Risk is false alarms during genuine exchange outages — acceptable.
- **Duration.** 4 hours.
- **Impact.** **VERY HIGH** — this is the control that would have caught all three failures.

---

### T5 — Real supervision via launchd

- **Objective.** Automatic start at boot, automatic restart on death, without a human.
- **Why it matters.** The documented mechanism requires `tmux`, which is not
  installed; the keepalive has never run once; `state/supervisor.stop` has been
  in place since 07-15.
- **Files.** `scripts/com.cgc.archiver.plist.template` (new),
  `scripts/com.cgc.forwardpaper.plist.template` (new),
  `scripts/install_launchd.sh`, `DAILY_OPERATIONS.md`, `BEDIENING.md`;
  remove `state/supervisor.stop`; retire the tmux instruction.
- **Expected output.** Two launchd agents with `RunAtLoad`, `KeepAlive`
  (`SuccessfulExit=false`), `ThrottleInterval` ≥ 60 s, correct
  `WorkingDirectory`, stdout/stderr to `logs/`. Keepalive's fail-closed rule
  (3 restarts / 30 min) preserved as a guard against crash loops.
- **Acceptance criteria.** `kill -9` on either process ⇒ automatic restart
  within 90 s; full reboot ⇒ both processes running with no login action;
  crash loop ⇒ throttle engages and a human-visible marker is written.
- **Validation.** Kill test ×3, then a real reboot. Both must be evidenced in
  the ops logbook (which this task also creates).
- **Rollback.** `launchctl unload` both agents; revert to manual start.
- **Risk.** MEDIUM. A restart loop could mask a persistent fault — mitigated by
  throttling plus T4's volume floors.
- **Duration.** 4 hours + 1 reboot window.
- **Impact.** **VERY HIGH** — converts "24/7" from an aspiration into a property.

---

### T6 — Neutralise expectancy gating in MODE 1

- **Objective.** Let the pipeline actually produce executable plans during observation.
- **Why it matters.** Four of five detectors convert **zero** candidates to
  executable plans, blocked by the system's own live-phase expectancy history
  and symbol kill-switches. Fixing T2 without this yields a working writer with
  nothing to write. This directly contradicts `MASTER_PLAN`'s claim that
  low_vol_reclaim is the "primary forward-paper workhorse."
- **Files.** `risk/risk_manager.py`, `app/config.py`, `.env.example`,
  `tests/test_risk_manager.py`.
- **Expected output.** A `FORWARD_PAPER_BYPASS_EXPECTANCY_GATES` flag, **default
  false**, forced **true** only when `FORWARD_PAPER_ONLY=true`. Bypasses
  expectancy throttling, strategy probes and expectancy kill-switches. Does
  **not** touch structural limits (max positions, notional caps, leverage) or
  any live-mode path.
- **Acceptance criteria.** In strict forward-paper mode, ≥ 1 executable plan per
  24 h across ≥ 3 distinct detectors. In live mode, the risk manager's behaviour
  is bit-identical to today — proven by test.
- **Validation.** New test asserting the flag cannot be true when
  `FORWARD_PAPER_ONLY=false`; then 24 h of live observation counting executable
  plans per detector.
- **Rollback.** Set flag false; immediate return to current gating.
- **Risk.** **MEDIUM-HIGH — the most dangerous task in this plan.** It weakens
  risk controls, which `AGENTS.md` forbids without explicit approval. Mitigation:
  strictly mode-gated, default off, technically impossible to enable in live mode,
  covered by a dedicated test. **Requires explicit owner sign-off before merge.**
- **Duration.** 4 hours.
- **Impact.** **HIGH** — without it, forward paper observes an empty funnel.

---

### T7 — Restore the test suite to green and hermetic

- **Objective.** Make CI a trustworthy signal.
- **Why it matters.** CI passed green on the exact commit that had been broken
  in production for six days. A test signal that cannot detect total functional
  failure is worse than none, because it is believed.
- **Files.** `tests/test_legacy_inventory.py`, `tests/test_public_candidate_pipeline.py`,
  `tests/conftest.py`, `pytest.ini`, `.gitignore`.
- **Expected output.** Legacy inventory scan excludes `.claude/` and any nested
  worktree. A session-scoped fixture isolates learning/expectancy state so the
  risk manager cannot read real history. `.claude/` added to `.gitignore`.
- **Acceptance criteria.** `pytest -q` returns **263 passed, 0 failed** on this
  host **and** on a clean checkout. The same result on CI.
- **Validation.** Run locally; run in a fresh `git clone` to a temp directory;
  confirm CI green.
- **Rollback.** Revert; suite returns to 3 red.
- **Risk.** LOW.
- **Duration.** 3 hours.
- **Impact.** **HIGH** — restores the ability to trust any future green run.

---

### T8 — Close the production-factory → paper-store seam

- **Objective.** Test the join that broke, so it cannot break again unnoticed.
- **Why it matters.** PR #7 claimed "field-exact parity, contract-tested." No
  test crosses this seam; the fixture hand-builds `MarketSnapshot`. That gap is
  exactly where C1 lived for six days.
- **Files.** `tests/test_public_candidate_pipeline.py`,
  new `tests/test_production_snapshot_pipeline.py`.
- **Expected output.** A test that builds its snapshot via
  `build_snapshot_from_inputs` with a realistic `LiveMarketContext` (including a
  populated `ContractSpec`), drives the full funnel, and asserts exactly one
  `TRADE_OPENED` plus one complete outcome row.
- **Acceptance criteria.** The new test **fails** on the pre-T2 commit and
  **passes** after T2. Demonstrating both directions is the deliverable.
- **Validation.** `git stash` the T2 fix, run, observe failure; restore, run, observe pass.
- **Rollback.** Delete the test (not advised).
- **Risk.** LOW.
- **Duration.** 3 hours.
- **Impact.** **HIGH** — converts the parity claim from marketing into a contract.

---

### T9 — Weekly data-quality report (automated)

- **Objective.** Produce the Phase 1 evidence artefact without manual work.
- **Why it matters.** An outstanding ROADMAP deliverable, and the record that
  Phase C's data sufficiency argument will rest on.
- **Files.** new `scripts/data_quality_report.py`, `scripts/daily_ops_check.sh`,
  new `tests/test_data_quality_report.py`.
- **Expected output.** Per source per day: expected rows, actual rows, coverage %,
  gaps > 3× interval with timestamps, dedupe ratio, field-fill rates, exchange-lag
  distribution, SHA-256 per day file. Written to `reports/data_quality/YYYY-WW.json`
  plus a Markdown summary. **Committed, not gitignored.**
- **Acceptance criteria.** Running it over 2026-07-18 → 07-25 reproduces this
  audit's Part 7 numbers exactly (1.2 days-equivalent, the 07-21/23/25 funding
  settlement gaps). That is the correctness test.
- **Validation.** Compare output against the figures in this report.
- **Rollback.** Remove the script; no runtime dependency.
- **Risk.** LOW.
- **Duration.** 5 hours.
- **Impact.** **HIGH** — makes Phase 1 exit criteria machine-checkable.

---

### T10 — Documentation correction and generated status

- **Objective.** Make it impossible for status to overstate reality again.
- **Why it matters.** Risk R2. A returning owner — or a future agent handed
  these files — currently reads that Phase B is on track. It is not.
- **Files.** `PROJECT_STATUS.md`, `MASTER_PLAN.md`, `ROADMAP.md`,
  `GO_LIVE_CHECKLIST.md`, `BEDIENING.md`, `docs/RESEARCH_JOURNAL.md`,
  `docs/JOURNAL.md`, new `scripts/generate_project_status.py`.
- **Expected output.** `PROJECT_STATUS.md` regenerated from measured values
  (uptime, coverage %, paper trades, gate counts, test results) with a
  "generated at" stamp and a hand-written interpretation section clearly
  separated. Gate count corrected to **0/24**. The `tmux` instruction replaced
  with launchd. `RESEARCH_JOURNAL.md` reconciled with `CHANGELOG.md` on H-4D-2/3.
- **Acceptance criteria.** No status claim in any document lacks either a
  generated value or a named evidence file. Re-running the generator after a
  deliberate 1-hour outage shows the outage.
- **Validation.** Diff generated output against this report's Part 7 and Part 10.
- **Rollback.** Documentation only; revert freely.
- **Risk.** LOW.
- **Duration.** 5 hours.
- **Impact.** **HIGH** — removes the project's most persistent hazard.

---

### T11 — Repository and branch cleanup

- **Objective.** Reduce the surface to what is actually maintained.
- **Why it matters.** 18 worktrees and 45 branches actively break a test, inflate
  every search, and hide which code is real. Also recovers ~2 GB on a disk at 88%.
- **Files/paths.** `.claude/worktrees/*` (7 nested), 10 sibling worktrees,
  ~35 stale branches, root artefacts (`full_tree.txt`, `project_inventory.txt`,
  `python_bestanden.txt`, `report_bestanden.txt`, `mapstructuur.txt`, `0`),
  `logs/` rotation, `reports/` archival.
- **Expected output.** ≤ 8 remote branches; ≤ 2 worktrees; root artefacts removed
  from git; log retention enforced; `reports/analysis/` **un-ignored** so research
  evidence becomes durable (R5).
- **Acceptance criteria.** `git worktree list` ≤ 2; `git branch -r` ≤ 8;
  `test_legacy_inventory` passes without exclusions; working tree < 2 GB.
- **Validation.** Full test suite after cleanup; `verify_repository_hygiene.sh` PASS.
- **Rollback.** Branches are archived as tags (`archive/<name>`) before deletion —
  nothing is unrecoverable. **Merge or explicitly close
  `claude/mystifying-meninsky-6c9e67` (CLOSED_SYNCED backfill) before pruning.**
- **Risk.** MEDIUM — deletion is involved. Mitigation: tag-before-delete, and the
  owner confirms the branch list before any deletion runs.
- **Duration.** 4 hours.
- **Impact.** MEDIUM — no functional gain, large maintainability and clarity gain.

---

### T12 — Seven-day verified stability run

- **Objective.** Prove Phase 0 is actually done.
- **Why it matters.** Phase A was declared complete without this step, which is
  precisely why this audit exists. **Phase 1 does not start until this passes.**
- **Files.** None (observation only); ops logbook entries.
- **Expected output.** 7 consecutive days meeting every Phase 0 acceptance
  criterion, including one deliberate reboot and one deliberate `kill -9`.
- **Acceptance criteria.** Orderbook ≥ 95% nominal all 7 days; ≥ 1 paper trade
  per 24 h; zero unexplained stops; `daily_ops_check` exit 0 for 7 days; the
  injected-shortfall test still FAILs correctly.
- **Validation.** `scripts/data_quality_report.py` over the window + the logbook.
- **Rollback.** N/A — if it fails, Phase 0 is not done and the cause is fixed first.
- **Risk.** LOW technically; **HIGH to schedule** — the temptation will be to
  declare victory on day 3 and start counting Phase 1. Do not.
- **Duration.** 7 calendar days, ~30 min/day of checking.
- **Impact.** **VERY HIGH** — this is the gate that was skipped last time.

---

### Schedule

| Day | Work |
|---|---|
| 1 | T1, T2, T3 — **all three same day**; data loss stops immediately |
| 2 | T4 (volume floors) |
| 3 | T5 (launchd) + reboot drill |
| 4 | T6 (expectancy bypass) — **owner sign-off required before merge** |
| 5 | T7 (test suite green + hermetic) |
| 6 | T8 (production-factory seam test) |
| 7 | T9 (data-quality report) |
| 8 | T10 (documentation + generated status) |
| 9 | T11 (repo cleanup) |
| 10–14 | T12 (verified stability run, running since day 5) |

**Total engineering effort: ~40 hours over 14 calendar days.** The remainder is
observation, which is the point.

**What is deliberately absent:** no new strategies, no research runs, no
backtests, no optimiser work, no dashboards, no ML, no runner migration
(deferred to Phase 1, where it costs little because the tag and scripts already
exist). Every task above either stops a loss, produces evidence, or makes a
failure visible.

---

## PART 18 — EXECUTION MODE

From this point I own the technical direction of this project. The operating
rules I will hold myself to:

**1. Instrumentation outranks assertion.** I will not report a component as
working because its code is correct, its tests pass, or its PR merged. I will
report it as working when it has demonstrated expected output over a stated
window, and I will name the measurement. When I cannot measure something, I will
say "unverified" rather than "done."

**2. Every phase exits on evidence, not on calendar or effort.** Phase 1 does
not begin because Phase 0's tasks are closed; it begins when T12 passes. This is
the specific discipline that failed last time, and it failed because "260/260
tests green" was accepted as proof of a working platform.

**3. I will not protect prior decisions, including my own.** Three findings in
this audit contradict recently-merged work: PR #7's parity claim is unenforced
at the seam that mattered, PR #12's keepalive has never executed, and the
declared Phase A completion was premature. Those are stated plainly above and
will be stated plainly again if they recur.

**4. Safety controls are never weakened silently.** T6 weakens risk gating in
MODE 1. It is flagged as the most dangerous task in the plan, it is mode-locked
and default-off, and it does not merge without explicit owner sign-off. Any
future change touching `risk/`, `execution/` or credentials gets the same
treatment, per `AGENTS.md`.

**5. Research integrity is not negotiable.** Pre-registration before data;
no parameter tuning after outcomes; no recycling rejected families without new
data *and* a new mechanism; rejections recorded as first-class results.
The existing protocol is the best thing this project has — I will not relax it
to make progress look faster.

**6. I will actively resist scope.** The correct answer to most proposals for
the next month is "not now." New detectors, dashboards, ML, optimisation and
speculative research all fail the only test that matters: *does this move us
toward a statistically validated autonomous trading system?* Fixing collection
passes. A new strategy does not.

**7. I will say "no edge" as often as the data says it.** Eight families
rejected. The professional expectation is that microstructure may also reject.
The purpose of this platform is to find out honestly and cheaply — not to
eventually find a reason to trade.

### Immediate next action

T1, T2 and T3 on a single branch, today. Until they land, every hour costs an
hour of irreplaceable data and produces no forward-paper evidence.

---

*End of report. Generated 2026-07-25 from commit `6e57b2a`. All figures are
reproducible from the working tree, `data/archive/`, `logs/`, `state/` and
`git` at that commit.*
