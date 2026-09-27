# 15 — Production Deployment Report — **HALTED AT GATE**

**Report date:** 2026-07-27T19:26:54Z
**Role:** Production Deployment Engineer
**Requested action:** configure the production environment for controlled Live v1
**Action taken:** **none — deployment halted before any configuration change**

---

## 1. Why this deployment did not proceed

The deployment instruction opens with a gate:

> *"Only continue if every Critical blocker has been resolved or explicitly accepted."*

**The gate is not satisfied.** Verified against evidence, not assumption:

| Blocker | Required | Actual | Evidence |
|---|---|---|---|
| **R1** — no recovery after host restart | resolved or accepted | **OPEN** | `docs/RISK_REGISTER.md` → `Status: OPEN`. `com.cgc.tradingbot` launchd job state `-` (not running). Supervisor in use is `exec bash scripts/forward_paper_keepalive.sh --loop 120` under `nohup`/tmux — does not survive reboot. |
| **R2** — host sleep suspends the process | resolved or accepted | **OPEN** | `Status: OPEN`. `pmset -g` still shows `sleep 1` (1-minute idle sleep). No independent assertion-liveness check exists. |
| Explicit acceptance | signed off | **NONE** | `docs/RISK_REGISTER.md` → `## Resolved` section: *"(none yet)"*. `11_KNOWN_RISKS.md`: *"No risk in this register has been accepted or waived."* |

Nothing has changed since the readiness assessment: `git diff cda8187..HEAD` over `app/`,
`clients/`, `execution/`, `risk/`, `strategies/`, `planning/`, `forward_paper/`,
`market_features/`, `data/`, `telemetry/` = **0 files**.

Per your own instruction, I stopped. **No environment variable, credential, permission,
limit or mode was changed.**

---

## 2. What verification did find — three material findings

Read-only verification was performed because it informs the decision. It produced three
findings that strengthen the halt.

### F1 — Live and Forward-Paper configuration are **not separated** (the task's first requirement)

There is exactly one config file. `.env.example` is a template, not a live/paper split.

```
.env          3 826 bytes   ← single ambient configuration
.env.example  3 301 bytes   ← template
```

### F2 — The ambient `.env` is already in a **live-ish posture**, and one variable is the only barrier

| Setting | `.env` value | Validated value | Assessment |
|---|---|---|---|
| `EXECUTION_ENABLED` | `false` | false | **the only barrier to real orders** |
| `EXECUTION_MODE` | **`LIVE`** | `DRY_RUN` | live posture |
| `FORWARD_PAPER_ONLY` | **unset → False** | `true` | not strict paper |
| `MAX_OPEN_POSITIONS` | **4** | **1** | 4× the validated exposure |
| `MAX_SYMBOLS` | **40** | **1** | 40× the validated scope |
| `MAX_LEVERAGE` | 3 | 5 (default) | lower, fine |
| `ACCOUNT_RISK_PER_TRADE_PCT` | 0.50 | 0.75 (default) | lower, fine |
| `MAX_DAILY_LOSS_PCT` / `HARD_DAILY_STOP_PCT` | 1.0 / 1.5 | 1.5 / 2.0 (default) | tighter, fine |

**A single flip of `EXECUTION_ENABLED` would run 40 symbols and 4 concurrent positions in
`LIVE` mode** — on a system whose live order path has never executed once, and which has
only ever been validated at 1 symbol and 1 position.

`scripts/start_forward_paper.sh` protects against this by forcing the safe values, but
anything started via `scripts/start_bot.sh` or a bare `python -m app.main` inherits the
ambient posture. The safety therefore depends on *which launcher is used*, not on the
configuration itself.

This is the single most important finding in this report.

### F3 — Order-level logging is thinner than production requires

Requirement: *every order request, exchange response, position update, stop update, fill,
error, reconnect, heartbeat, restart and state transition written to persistent logs.*

| Category | Status | Evidence |
|---|---|---|
| Order request | **PARTIAL** | no `ORDER_PLACED`-style log marker exists (0 files); orders persist to `LiveTradeJournalLogger` and `event_store` as structured records, but there is no log line |
| Exchange response | **PARTIAL** | `BITGET_API_LATENCY` logs method/path/status/latency and `BITGET_HTTP_ERROR` logs code/retryable — **response bodies are not logged** |
| Position update | IMPLEMENTED | `PositionUpdate` in 4 modules; never exercised (position manager disabled in paper) |
| Stop update | **VERIFIED** | `STOP_UPDATED` ×2 in the event store |
| Fill | VERIFIED (paper) | recorded as `simulated_fill` inside `TRADE_OPENED` ×3; no `ENTRY_FILLED` marker exists |
| Error | **VERIFIED** | 2 ERROR lines, both a real DNS incident |
| Reconnect / retry | **VERIFIED** | 3 × `BITGET_NETWORK_RETRY`, recovered |
| Heartbeat | **VERIFIED** | `state/runtime_heartbeat.json`, 1 177 cycles |
| Restart | **VERIFIED** | launcher banner + `logs/runtime.log` |
| State transition | **VERIFIED** | `EXIT_REASON_TRANSITION` ×2, plus full lifecycle event types |

For live forensics the two PARTIAL rows matter: if an order behaves unexpectedly you
would have status and latency but **not the exchange's actual response payload**.

---

## 3. Production Baseline Snapshot (candidate — no deployment performed)

| Field | Value |
|---|---|
| Snapshot UTC | 2026-07-27T19:26:54Z |
| Git commit | `e6f3738ece118f1a25a9be0c5107f7e5c832aa66` |
| Short / describe | `e6f3738` / `rc1-forward-paper-validated-1-ge6f3738` |
| Branch / tree clean | `main` / **yes** |
| Version | Live_Release_v1 (candidate) on `rc1-forward-paper-validated` |
| Host / OS / arch | MacBook-Air-van-Bryon-2.local / macOS 26.5.1 / arm64 |
| Python / venv | 3.11.15 / `.venv` |
| Config hash (ambient) | `a3ecf0423740dab3f04de45d5dc861cb…` |
| **Deployment operator** | **NOT SET — no deployment performed** |
| Running processes | none |
| Test suite | 339 passed |

Note the ambient config hash `a3ecf042…` differs from both the manifest hash
`17524444…` and the trade-stamped hash `d6f53802…`, which is risk R8 (configuration
identity not traceable) showing up again in practice.

---

## 4. What was changed

**Nothing.** No environment variable, credential, permission, risk limit, position limit,
logging setting, supervisor, heartbeat or health check was modified. No file outside this
report was written. `git diff` over all runtime code since the RC1 baseline: 0 files.

Consequently there is nothing to roll back. The rollback instruction for this report is:
delete this file. Full procedures remain in [06_ROLLBACK_GUIDE.md](06_ROLLBACK_GUIDE.md).

---

## 5. Deployment checklist — honest status

| # | Item | Status |
|---|---|---|
| 1 | Critical blockers resolved or accepted | **FAIL — gate not satisfied** |
| 2 | Live configuration separated from Forward Paper | **NOT DONE** (F1) |
| 3 | Environment variables verified | **VERIFIED — and found unsafe for live** (F2) |
| 4 | API configuration | NOT DONE — requires owner; I must not enter credentials |
| 5 | Permissions (trade-only, no withdrawal, IP allow-list) | NOT DONE — owner action on the exchange |
| 6 | Logging | **PARTIAL** (F3) |
| 7 | Monitoring | **FAIL** — no off-host alerting; nothing monitors the monitor (R3) |
| 8 | Supervisor | **FAIL** — not boot-persistent (R1) |
| 9 | Heartbeat | PASS WITH LIMITATION — truthful; freshness has no live consumer (R4) |
| 10 | Restart behaviour | PARTIAL — process restart proven (91 s/121 s); host restart fails (R1) |
| 11 | Risk limits | VERIFIED PRESENT — never exercised in a running system (R7) |
| 12 | Position limits | **MISMATCH** — ambient allows 4; only 1 validated |
| 13 | Max concurrent positions | **MISMATCH** — see 12 |
| 14 | Emergency stop | DOCUMENTED — no exchange-side kill switch; process exit cancels nothing |
| 15 | Rollback | DOCUMENTED — **never rehearsed** |
| 16 | Health checks | PASS — typed statuses verified live |
| 17 | Alerting | **FAIL** — none delivered off-host |

**Verified: 4 · Partial: 4 · Failed/not done: 9.**

---

## 6. Required decision

This is the owner's call, not mine. Three paths:

**A. Close the blockers first (recommended).** Implement boot-persistent supervision and
verified sleep prevention, re-run a full-duration reliability validation, then return to
deployment. Preconditions 1–4 in
[PRODUCTION_READINESS_ASSESSMENT.md](PRODUCTION_READINESS_ASSESSMENT.md).

**B. Explicitly accept R1 and R2 in writing.** The gate permits acceptance as an
alternative to resolution. Acceptance must be recorded in `docs/RISK_REGISTER.md` with
date and rationale, and means accepting that an open position may go unmanaged for hours
after a reboot or a sleep event — with no alert.

**C. Prepare only the non-enabling work.** Separate live/paper configuration into
distinct files and tighten the ambient posture (F2) **without** enabling execution. This
is reversible, reduces the single-variable risk immediately, and does not require the
gate to be open.

Regardless of path: **entering API credentials and enabling live execution are owner
actions.** I will not perform them.

---

## 7. Statement

The system was not deployed because your own gate forbids it, and verification found
that the ambient configuration is closer to live than the documentation implied: one
environment variable away from 40 symbols and 4 concurrent positions, on an execution
path that has never placed a single order.

Halting here is the correct outcome, not a failure of the deployment.
