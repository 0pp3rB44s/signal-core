# RC2 — Release Notes

**Release:** RC2 — Production Hardened Platform (operational layer only)
**Base:** RC1 `rc1-forward-paper-validated` · **Engine:** FROZEN

# VERDICT: RC2 NOT READY FOR LIVE PILOT

The operational layer is materially better. The blocking condition is unchanged:
**R2 (host sleep) is untouched**, and R1's fix is **unverified by a real reboot**.

## Version summary

| | RC1 | RC2 |
|---|---|---|
| Engine | validated, frozen | **unchanged — 0 runtime files** |
| Config | one `.env`, live-ish, 1 barrier | separated, 4 authorisation layers |
| Boot persistence | none (`nohup`) | launchd agent (unverified by reboot) |
| Monitor self-monitoring | none | independent watchdog + own heartbeat |
| Alerting | none | library present, **no provider — DEGRADED** |
| Infrastructure tests | none | 25 passed / 0 failed |
| Suite | 339 passed | 339 passed |

## Operational improvements

1. **Configuration isolation.** `.env.forward` and `.env.live.example` with
   `env_guard.sh` asserting per-mode invariants independently of file contents.
   The guard rejects the ambient 40-symbol / 4-position posture the audit flagged.
2. **Two launchers that cannot cross modes**, with four independent live
   authorisation layers. Verified: live launch aborts at layer 1.
3. **Boot persistence** via launchd, two-level (launchd → keepalive → engine).
4. **Cross-component watchdog**, independent of what it watches, verified against
   the real post-mortem state.
5. **Off-host alerting** that degrades visibly rather than silently.

## Migration notes

- The legacy `.env` is untouched and still used by `start_bot.sh` /
  `start_forward_paper.sh`. **New work should use `scripts/launch_forward.sh`.**
  Until the legacy path is retired, the old single-config exposure still exists
  for anyone using the old launcher — the new guards do not retro-protect it.
- Install boot persistence: `bash deploy/launchd/install_forward_agent.sh`.
- Configure alerts: copy `deploy/alerting.env.example` → `state/alerting.env`.
- Schedule the watchdog separately from the supervisor.

## Rollback

Every phase is a separate commit and independently revertable:

| Phase | Commit | Rollback |
|---|---|---|
| 1 config isolation | `a316d45` (+ fix) | `git revert`; delete `.env.forward`, `.env.live.example` |
| 2 launchers | `bd5bf03` | `git revert`; old launchers untouched |
| 3 boot persistence | `5f862ef` | `bash deploy/launchd/uninstall_forward_agent.sh` then revert |
| 4 watchdog | `71f931a` | `git revert` |
| 5 alerting | `4dcea38` (+ fix) | `git revert` |

Nothing in RC2 is required by the engine, so reverting all of it returns the
system to exactly RC1 behaviour.

## Remaining risks

**Blocking:**
- **R2 host sleep — untouched.** A 22.19 h suspension happened despite an active
  power assertion. Not addressed in RC2.
- **R1 — mitigated, unverified.** The launchd agent has never survived a real reboot.
- **R5 live order path — never executed.** Unchanged by design.

**Also open:** alerting has no provider (DEGRADED); no off-host backup (Phase 8
not done); order-request and exchange-response payload logging absent (Phase 6
not done — would require editing the frozen engine); risk-gate blocking never
exercised (R7); R6, R8–R13 unchanged.

## Known limitations of RC2 itself

- **Phase 6 not done.** Order requests and exchange response bodies are still not
  logged. Implementing it means editing `execution/execution_service.py` and
  `clients/`, which the freeze forbids. Live forensics would be incomplete.
- **Phase 7 partial.** Kill/stop paths documented; **cancel-open-orders and
  position-flattening deliberately not implemented** — owner-only financial actions.
- **Phase 8 not done.** No off-host backup; total host loss still loses the event
  store, telemetry and state.
- **Phase 9 partial.** Mode/exposure defence in depth achieved; exchange-side
  permissions and API scopes are owner actions and unreviewed.
- **Reboot untested** — the single most consequential outstanding test.

## Final review

| Dimension | Assessment |
|---|---|
| Operational reliability | improved; unproven under reboot and sleep |
| Infrastructure quality | good — 25/25, all components abort rather than continue |
| Recoverability | process-level proven; host-level designed, unverified; no off-host backup |
| Observability | materially improved — the watchdog catches the real historical failures |
| Deployment safety | strong — 4 layers, verified blocking |
| Maintainability | good — separate commits, each revertable |
| Documentation | complete for what was built; honest about what was not |
| Auditability | strong — every change committed separately with rationale |
| Production readiness | **not reached** |

**RC2 NOT READY FOR LIVE PILOT.** Minimum to reconsider: address R2, verify R1 by
an actual reboot, configure an alert provider, and rehearse recovery.
