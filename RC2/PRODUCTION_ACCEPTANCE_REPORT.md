# PRODUCTION ACCEPTANCE REPORT

**Date:** 2026-07-27 · **Authority:** Principal SRE / Production Release Authority
**Baseline:** `rc2-hardened-platform` (`e0c4aee`) · **Engine:** FROZEN, 0 runtime files changed

# VERDICT: NOT READY FOR PRODUCTION

Four of the eight Phase-9 gate criteria are not met. Two RC2 controls were found
**defective under test** — this acceptance run disproved work that RC2 claimed.

---

## Phase 9 — Final Production Gate

| # | Criterion | Result | Evidence |
|---|---|---|---|
| 1 | Supervisor survives restart as designed | **NOT MET** | Process-level recovery works (60 s, SIGKILL → new PID, 1 engine, assertion rebound). **Boot persistence is broken** — see D1. |
| 2 | Monitoring detects component failures | **MET** | Watchdog with engine down reported 6 findings incl. stale heartbeat and dead monitor; with engine up reported it alive and heartbeat fresh (17 s). |
| 3 | External alerting is operational | **NOT MET** | No provider configured. `send_alert` returns rc=3 "NO PROVIDER CONFIGURED — DEGRADED". **Zero external deliveries demonstrated.** |
| 4 | Configuration isolation prevents accidental live execution | **PARTIALLY MET** | Manual path fully verified (6/6 guard tests). **Supervised restarts bypass it** — see D2. |
| 5 | Recovery procedures validated | **PARTIALLY MET** | Forced termination ✅, launcher abort ✅, duplicate-start refusal ✅. Reboot, power loss, network/DNS injection **not tested**. |
| 6 | Rollback procedures validated | **PARTIALLY MET** | 4 of 5 RC2 commits revert cleanly; `a316d45` **conflicts**. RC1 tag reachable. Never rehearsed end-to-end. |
| 7 | Logging sufficient for incident reconstruction | **PARTIALLY MET** | Startup, API latency (87 lines), heartbeat with session_id, launcher decisions, 22 alert entries, event chain valid. **No order-level logging** (RC2 Phase 6 not done). |
| 8 | No unresolved Critical operational blockers | **NOT MET** | R1 OPEN (and its fix defective), R2 OPEN and untouched. |

**Met: 1 · Partially met: 4 · Not met: 3.**

---

## Defects found during acceptance

### D1 — RC2's boot-persistence fix does not work (CRITICAL)

Installed the launchd agent and it **failed to execute**:

```
launchctl list  →  -   126   com.cgc.forward        (126 = cannot execute)
shell-init: error retrieving current directory: getcwd: cannot access parent
            directories: Operation not permitted
/bin/bash: .../deploy/launchd/forward_agent.sh: Operation not permitted
```

Cause: the repository lives under `~/Desktop/`, a macOS **TCC-protected** directory.
launchd-spawned processes cannot read it without Full Disk Access, which is a GUI
consent action that cannot be automated.

**This was already documented in the repository.** `scripts/run_supervised.sh`
states *"geen macOS TCC-problemen zoals bij launchd — Bewust GEEN launchd"*
(deliberately NOT launchd), and `com.cgc.tradingbot.plist.template` records two
earlier launchd approaches that both failed.

RC2 Phase 3 reintroduced a known-failed approach without consulting that
documentation. **R1 is therefore NOT mitigated.** The broken agent was uninstalled;
`launchctl list` confirms it is no longer loaded.

### D2 — RC2 configuration isolation does not apply to supervised restarts (HIGH)

`scripts/forward_paper_keepalive.sh:46` restarts through
`scripts/start_forward_paper.sh`, **not** the new `scripts/launch_forward.sh`.

Verified: after the forced-termination recovery, `state/forward_paper_runtime.state`
lacked the `commit=` line that `launch_forward.sh` writes, confirming the old
launcher ran. The recovered engine therefore loaded the **ambient `.env`**
(`MAX_SYMBOLS=40`, `MAX_OPEN_POSITIONS=4`) rather than `.env.forward` (1 and 1).

**Safety is not compromised** — `start_forward_paper.sh` still forces
`FORWARD_PAPER_ONLY=true`, `EXECUTION_ENABLED=false` and blanks credentials, and
0 private/order calls were logged. But the **pilot exposure ceilings are not
enforced on any automatic restart**, so RC2's isolation covers only manual starts.

---

## What was verified as working

| Area | Evidence |
|---|---|
| Forward launcher guards | 7 guards passed in sequence on a real start (clean tree, venv, no duplicate, disk, forward invariants, pilot limits) |
| Live launcher blocking | aborts at layer 1: "2 Critical risk(s) still OPEN" |
| Guard rejects live config in forward mode | aborted |
| Guard rejects ambient `.env` posture | aborted: "MAX_SYMBOLS must be <=1 (got '40')" |
| Duplicate-start refusal | aborted |
| Forced termination recovery | SIGKILL 97555 → recovered 99573 in **60 s**, 1 engine, assertion rebound |
| Watchdog detection | engine down → 6 findings; engine up → alive, heartbeat fresh 17 s |
| Watchdog self-observability | writes `state/watchdog_heartbeat.json` |
| Event-chain integrity | valid throughout |
| Evidence archive restore | digest OK; **63/63 files verified** after extraction |
| Engine frozen | 0 runtime files changed; safety: 0 private/order calls |

---

## Updated blocker status

| Blocker | Prior | Now | Evidence |
|---|---|---|---|
| **R1** no recovery after host restart | OPEN (RC2 claimed mitigated) | **OPEN — fix defective** | launchd exit 126, TCC; agent removed |
| **R2** host sleep | OPEN | **OPEN — untouched** | not addressed in RC2 or here |
| **R3** nothing watches the monitor | mitigated | **MITIGATED, verified** | watchdog detected dead monitor |
| **R4** heartbeat freshness unused | mitigated | **MITIGATED, verified** | detected 9 h stale heartbeat |
| **R5** live order path never executed | OPEN | **OPEN** | 0 order calls |
| Config single-variable exposure | mitigated | **PARTIAL** | manual path only (D2) |
| External alerting | added | **NOT OPERATIONAL** | rc=3, no provider |
| Off-host backup | absent | **ABSENT** | Phase 8 not done |

---

## Remaining blockers for production

1. **R2 host sleep** — untouched; a 22.19 h suspension occurred despite a power assertion.
2. **R1 boot persistence** — launchd is blocked by TCC at this repository path.
3. **External alerting** — no provider; zero deliveries demonstrated.
4. **D2** — automatic restarts bypass RC2 configuration isolation.
5. **Order-level logging absent** — live incidents would not be fully reconstructable.
6. **R5** — live order path never executed.
7. **No off-host backup** — total host loss still loses the event store and state.

## Go-live recommendation

**Do not proceed.** Live execution must remain disabled.

The minimum credible path: relocate the repository outside `~/Desktop` (or grant
Full Disk Access) so boot persistence can work at all, then verify it with a real
reboot; address R2; point the supervisor at `launch_forward.sh`; configure an alert
provider and demonstrate delivery; then re-run a full-duration reliability
validation before reconsidering.

## System state at report time

Engines 0, supervisors 0, caffeinate 0, launchd agent unloaded, working tree clean,
`EXECUTION_ENABLED=false`, `.env.live` absent, authorisation token absent.
