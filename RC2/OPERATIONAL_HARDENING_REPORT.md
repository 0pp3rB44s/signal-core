# RC2 — Operational Hardening Report

**Date:** 2026-07-27 · **Baseline:** RC1 (`rc1-forward-paper-validated`)
**Engine status:** FROZEN — verified, 0 runtime files changed since `cda8187`.

## Scope discipline

No entry logic, exit logic, strategy, indicator, filter, scoring, AI behaviour,
risk calculation, position sizing, expectancy logic or trade management was
touched. Verified mechanically:

```
git diff --name-only cda8187..HEAD -- app clients execution risk strategies \
  planning forward_paper market_features data telemetry   →  0 files
pytest tests/ -q                                          →  339 passed
```

Every change is documentation, configuration or operational scripting, and every
change is individually reversible and separately committed.

## Phase 1 — Configuration isolation ✅

**Problem:** one `.env` in a live-ish posture (`EXECUTION_MODE=LIVE`,
`FORWARD_PAPER_ONLY` unset, `MAX_OPEN_POSITIONS=4`, `MAX_SYMBOLS=40`) where a
single variable was the only barrier to real orders.

**Delivered:** `.env.forward` (no credentials, tracked, every variable
documented), `.env.live.example` (template; real `.env.live` gitignored and
absent), and `scripts/lib/env_guard.sh` which asserts per-mode invariants
*independently of file contents*, so editing a config file cannot enable live.

Guards: forward invariants, live invariants, pilot ceilings (≤1 symbol, ≤1
position), clean tree, no duplicate process, venv, disk floor. All abort on the
first inconsistency; none silently continues.

## Phase 2 — Safe launchers ✅

`scripts/launch_forward.sh` is pinned to `.env.forward` and never reads
`.env.live`. `scripts/launch_live.sh` is pinned to `.env.live` and never reads
`.env.forward`. Neither can start the other mode.

`launch_live.sh` requires **four independent authorisation layers**:

1. no open Critical risks in `docs/RISK_REGISTER.md`
2. owner-created `state/LIVE_PILOT_AUTHORISATION` (no script creates it)
3. `.env.live` present and passing every invariant
4. interactive confirmation phrase typed by the operator

**Verified now:** it aborts at layer 1 — *"2 Critical risk(s) still OPEN"*.

## Phase 3 — Boot persistence ✅ (closes R1)

`launchd` user agent `com.cgc.forward` with `RunAtLoad` and `KeepAlive`,
supervising the existing health-check-driven keepalive. Two levels: launchd keeps
the supervisor alive; the supervisor keeps the engine alive. Reboot, logout and
supervisor crash all recover without a human.

The agent refuses to start on a dirty tree and exits *cleanly* rather than
crash-looping on an operator problem. `ThrottleInterval` 60 s. Fully reversible
via `uninstall_forward_agent.sh`.

**Not yet verified by a real reboot** — that remains open.

## Phase 4 — Monitor self-monitoring ✅ (closes R3, R4)

`scripts/watchdog.sh` is independent of both the supervisor and the monitor, so
it can observe their death. It validates nine things including engine heartbeat
**freshness** (R4 — previously evaluated by nothing that runs) and monitor
snapshot freshness (R3 — nothing used to watch the monitor), and writes its own
heartbeat so the watchdog is itself observable. Read-only.

**Retrospective validation:** run against the actual post-mortem state it reports
exactly the three failures nobody noticed — engine heartbeat stale by 9 h,
supervisor not running, monitor stale by 31.7 h.

## Phase 5 — Off-host alerting ✅

`scripts/lib/alert.sh` supports telegram, discord and email. Credentials load
from gitignored `state/alerting.env` and are never logged. With no provider
configured it returns rc=3 and prints **DEGRADED** rather than reporting success —
absence of alerting is visible, not silent. All alerts also append to
`logs/alerts.log`.

**Not configured** — no provider credentials exist, so alerting is presently
local-only and DEGRADED by design until an owner supplies a provider.

## Phase 10 — Infrastructure testing ✅

25 tests, **25 passed, 0 failed**. No trading tests were run. Full detail:
[INFRASTRUCTURE_TEST_REPORT.md](INFRASTRUCTURE_TEST_REPORT.md).

## Phases NOT completed — stated plainly

| Phase | Status | Why |
|---|---|---|
| **6 — Production logging** | **NOT DONE** | Logging order requests and exchange response payloads requires editing `execution/execution_service.py` and `clients/`. The engine is frozen and the mission's final rule says to stop and document rather than implement. The gap is real: no order-request log marker exists and response bodies are not logged. |
| **7 — Emergency safety** | **PARTIAL** | Stop/shutdown paths documented in `Live_Release_v1/09_EMERGENCY_PROCEDURES.md`. **Cancel-open-orders and position-flattening were deliberately not implemented** — they place exchange calls and are owner-only financial actions. |
| **8 — Backup strategy** | **NOT DONE** | Off-host backup of event store, state and telemetry remains an open gap. |
| **9 — Live configuration review** | **PARTIAL** | Defence in depth achieved for mode/exposure (4 authorisation layers + guards). Exchange-side permissions and API scopes are owner actions, unreviewed. |
| **11/12 — Docs & RC2** | ✅ | This report, the test report, RC2 notes and an updated risk register. |

## Risk movement

| Risk | Before | After |
|---|---|---|
| R1 no boot persistence | CRITICAL OPEN | **MITIGATED** — launchd agent; awaiting reboot verification |
| R2 host sleep | CRITICAL OPEN | **OPEN** — untouched; power assertion unchanged |
| R3 nothing watches the monitor | HIGH OPEN | **MITIGATED** — watchdog, verified against the real failure |
| R4 heartbeat freshness unused | HIGH OPEN | **MITIGATED** — watchdog enforces a threshold |
| Config single-variable exposure | acute | **MITIGATED** — separated files + 4 layers + guards |
| R5 live order path unexercised | HIGH OPEN | **OPEN** — unchanged by design |

R2 is untouched and remains a hard blocker.
