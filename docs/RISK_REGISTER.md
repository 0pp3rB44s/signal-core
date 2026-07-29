# Risk register — carried into Release Candidate 1

Risks recorded by the Forward-Paper close-out audit of 2026-07-27. Each entry states
evidence, impact and likelihood. **No fixes are implemented in RC1**; this register
documents what is carried forward.

Source: `validation_72h/archive/` → `reports/CLOSEOUT_REPORT.md`, Phase 12.

Register is append-only. Do not delete entries; mark them resolved with a date and the
commit that resolved them.

---

## CRITICAL

### R1 — No recovery after host restart

- **Evidence:** `kern.boottime` = `2026-07-27T10:54:07Z`, 16 s after the bot's
  `SIGTERM` (`last_shutdown.json`: exit 143). Nothing ran for the following 7.82 h.
  The supervisor was a `nohup` shell loop; `com.cgc.tradingbot` launchd job is not
  loaded for this harness.
- **Impact:** in live trading a reboot leaves open positions unmanaged indefinitely —
  stops and targets stop being enforced by the bot.
- **Likelihood:** High (observed once in 43 h).
- **Status:** ACCEPTED by owner (Bryon Kolkman) 2026-07-28 — pending first reboot
  verification. Implemented and component-verified (see "Verification round 2"
  below). The composite proof — launchd restoring the engine across a real boot —
  requires a live session that does not exist before the first launch, so it cannot
  be obtained beforehand. The owner accepts this residual for the first live
  session. Convert to RESOLVED once `logs/launchd_live.out` shows the agent
  restoring the engine after a real reboot. Implementation commit `7c0fb07`.

**Update 2026-07-28 — supervision implemented, reboot proof outstanding.**

*What was actually wrong:* there was no live supervisor at all. `com.cgc.tradingbot`
is notify-only — its `ProgramArguments` are `pgrep -f app.main || osascript -e
'display notification …'`, it has `RunAtLoad` and `StartInterval` but **no
`KeepAlive`**, and it starts nothing. The only job carrying `KeepAlive` was
`com.cgc.forward` (forward paper), which was not loaded. So both failure paths were
real: a reboot left the engine offline, and a crash left it offline.

*Implemented:* `com.cgc.live` — `deploy/launchd/com.cgc.live.plist.template`,
`deploy/launchd/live_agent.sh`, `install_live_agent.sh`, `uninstall_live_agent.sh`.
`RunAtLoad=true` covers boot; `KeepAlive/SuccessfulExit=false` covers crashes; the
agent stays in the foreground for the engine's lifetime so `KeepAlive` tracks the
engine rather than a script that already returned; `ThrottleInterval=60` prevents a
crash-loop. `scripts/stop_all.sh` now writes `state/live.stop` so a deliberate stop
is not resurrected.

*Authorisation is never self-granted:* the agent **restores** a session the owner
already authorised and cannot create one. It exits 0 (no respawn) unless
`state/LIVE_PILOT_AUTHORISATION` **and** `state/live_runtime.state` both exist, the
stop flag is absent, the tree is clean, and the host cannot idle-sleep.

*Verified:* `plutil -lint` OK; `bash -n` OK on all three scripts; both refusal paths
exercised and returned exit 0 with the correct reason (no session on record; stop
flag present).

**Verification round 2, 2026-07-28 — restart mechanics proven by experiment.**

A throwaway agent (`com.cgc.r1probe`) mirroring the live plist's exit contract was
bootstrapped on this host and this macOS build, and then removed:

| Experiment | Expected | Observed |
|---|---|---|
| A — job exits 0 (mirrors an agent refusal) | exactly 1 run, no respawn | **1 run in 25 s** |
| B — job exits 1 (mirrors an engine crash) | respawn every `ThrottleInterval`=10 s | **5 runs in 45 s**, at 21:26:17/27/37/47/57 |
| C — flip back to exit 0 | respawn loop stops | **0 runs in the following 25 s** |

So `KeepAlive/SuccessfulExit=false` + `ThrottleInterval` behave exactly as the live
agent's contract requires: a refusal never loops, a crash always restarts.

Also verified directly against `com.cgc.live`: bootstrapped (`state = active`,
`runs = 1`, `last exit code = 0`); plist on disk at
`~/Library/LaunchAgents/com.cgc.live.plist` with `RunAtLoad = true`,
`KeepAlive.SuccessfulExit = false`, `WorkingDirectory` = the repo; stop-flag branch
refuses with exit 0; no-session branch refuses with exit 0; `.env.live` sources
cleanly (`EXECUTION_MODE=LIVE`, `MAX_SYMBOLS=1`, `MAX_OPEN_POSITIONS=1`,
credentials present); heartbeat writes `process_started` on engine start.

- **Residual risk — one link, and it cannot be closed yet.** Every component of the
  restart chain is now proven independently. What is *not* proven is the composite at
  a real boot: launchd loading the agent at login → the agent restoring the engine.
  A reboot performed **now** would not prove it either: `state/live_runtime.state`
  does not exist, so the agent would correctly hit the "no session on record" branch
  and exit 0. That demonstrates agent loading, not engine recovery — which is the
  substance of R1.

  The proof therefore requires this order: (1) an authorised live session runs and
  writes `live_runtime.state`; (2) the host reboots; (3) `logs/launchd_live.out`
  shows the agent restoring the engine. Step 1 is gated by LAYER 1, which blocks on
  R1 — a genuine circular dependency. **The designed exit is the one LAYER 1 already
  supports: an owner may record `**Status:** ACCEPTED …` in writing, launch, and then
  convert this entry to RESOLVED with the reboot evidence from step 3.** Until one of
  those two things happens, R1 stays PARTIALLY RESOLVED and correctly blocks.

  A wedged-but-alive engine remains outside `KeepAlive`'s reach; that case belongs to
  R2 and to the watchdog.

### R2 — Host sleep suspends the trading process undetected

- **Evidence:** 22.19 h log gap (`2026-07-26 14:38:21` → `2026-07-27 12:49:44`), same
  PID 96180, no error line, resuming with `report_age_hours=25.1`. A
  `caffeinate -ims -w 96180` assertion was held and did not prevent it. `pmset` shows
  system idle sleep at 1 minute.
- **Impact:** an open position is unmanaged for hours while the process believes it is
  running; stop-losses are never evaluated.
- **Likelihood:** High on this hardware.
- **Status:** RESOLVED FOR AC-POWERED OPERATION — 2026-07-29, verified by test.
  `disablesleep=1`, `sleep=0`; the Mac must remain connected to mains during LIVE
  operation. **Lid-close, AC loss and critical-battery outage remain operational
  constraints and are NOT covered.** Scope and evidence below. This entry was
  briefly and wrongly marked fully RESOLVED on 2026-07-28; that is retracted.
  The original 2026-07-27 mechanism was never fully established — whether the
  assertion process died first or was held and proved insufficient is **evidence
  not available** (process table lost at reboot; OS sleep records rotated).

**RETRACTION 2026-07-29 — R2 recurred in production, 8 hours after being closed.**

*What happened.* The host suspended the live engine for **3 h 00 m 29 s**, from
`03:25:52Z` (last API call) to `06:26:21Z` (funnel resumed). Evidence:

| Source | Record |
|---|---|
| `pmset -g log` | `2026-07-29 05:29:32 +0200  Sleep  Entering Sleep state due to 'Low Power Sleep': Using Batt (Charge:1%)` |
| `pmset -g log` | `2026-07-29 08:25:09 +0200  Wake from Hibernate [CDNVA] : due to acattach ... (Charge:3%)` |
| `logs/live.out` | last line `03:25:52Z`, first line after `06:25:05Z` (DNS failure, then recovery) |
| `funnel_events.jsonl` | single gap of 181.1 min, the only gap >7 min in the session |
| process table | PID 67526 unchanged throughout — the process never died, it was frozen |

*Why the 2026-07-28 fix did not cover it.* `sudo pmset -a sleep 0` disables
**idle** sleep. The event that fired was **critical-battery emergency sleep**,
which macOS performs regardless of `sleep 0` and regardless of held power
assertions. A `caffeinate` process (PID 67568) held `PreventUserIdleSystemSleep`
*and* `PreventSystemSleep` for the entire 9 h 13 m and did not prevent it.
Recovery required a human to physically attach the charger — `acattach` is the
wake reason. Nothing woke it on its own.

*Why nothing reported it.* `launchd` interval jobs do not fire while the host is
asleep, so a host-resident watchdog cannot observe its own host's suspension.
The engine heartbeat was also frozen at `process_started` (schema v1), so even
after wake there was no signal. Both are now improved (`com.cgc.watchdog` at 60 s;
heartbeat v2 committed but **not yet running**), and neither closes this vector.

*What is actually in place today.* `pmset -g custom` reports `sleep 0` and
`disksleep 0` on both AC and Battery. `guard_assert_power_continuous()` refuses
to launch, and `live_agent.sh` refuses to restore a session, whenever idle sleep
is enabled — so a *regression* to `sleep > 0` blocks startup. The host is
currently on AC and charging.

**MITIGATION VERIFIED 2026-07-29 — scope is AC-powered idle sleep only.**

*Owner applied* `sudo pmset -c disablesleep 1`. Measured afterwards:

```
pmset -g   : SleepDisabled  1          (system-wide)
pmset -g custom : sleep 0, disksleep 0 on BOTH AC Power and Battery Power
pmset -g batt   : "Now drawing from 'AC Power'"; battery charging
pmset -g sched  : no scheduled sleep or shutdown (two wake timers only)
```

*Controlled observation, 11 minutes, 13:53:26Z → 14:05:09Z.* Deliberately longer
than `displaysleep=10`, so the display slept while the system did not — the
discriminating test for idle sleep.

| Signal | Before | After |
|---|---|---|
| Engine PID / uptime | 67526 / 16:03:10 | 67526 / **16:14:53** |
| `logs/live.out` | 9 388 881 B | **9 528 316 B**, 9 s old |
| Funnel decisions | 78 708 | **78 728** |
| Watchdog runs (60 s) | 58 | **70** |
| Watchdog heartbeat age | — | **19 s** |
| Dashboard loopback | HTTP 200 | **HTTP 200** |
| Order attempts | 0 | **0** |

`pmset -g log`: the most recent `Entering Sleep state` remains
`2026-07-29 05:29:32 +0200` — the original battery outage. **No new sleep
transition occurred during the window.**

- **Verified:** idle-sleep protection while on AC. The 2026-07-27 (22.19 h) and
  2026-07-29 (3.00 h) outages were both sleep events that this configuration
  now prevents for the idle case.
- **NOT verified — lid-close.** The lid was not closed; doing so would be an
  uncontrolled test against a live engine. `disablesleep=1` is expected to cover
  clamshell, but that is an expectation, not a measurement.
- **NOT verified — critical battery.** Not exercised, and deliberately so:
  reproducing it means draining the machine that is trading. The 2026-07-29
  outage fired at 1% charge; macOS performs that sleep regardless of
  `disablesleep`. **Battery was 12–16% and charging during this test — a thin
  buffer. AC loss would reach the critical threshold quickly.**
- **NOT solved — total power loss / network loss.** Out of scope for any
  host-resident control.
- **Detection remains host-local.** `launchd` interval jobs do not run while the
  host sleeps, so `com.cgc.watchdog` cannot report its own host's suspension.
  See `docs/OFF_HOST_DEADMAN.md` — designed, not deployed.

  **Operating constraint for the LIVE pilot: the Mac must stay on mains with the
  lid open.** Loss of AC converts this back to the unmitigated failure mode.

**Update 2026-07-28 — idle sleep eliminated at the host and enforced at launch.**

*Host state, measured:* `pmset -g custom` now reports `sleep 0` on **both** AC and
Battery. Earlier in this same session it read `sleep 1` on both; the owner applied
`sudo pmset -a sleep 0` in between. `pmset -g` confirms the effective value
`sleep 0`. The documented 22.19 h failure was an **idle** sleep, and idle sleep is
now off at the OS level, not merely papered over by `caffeinate` (which
`scripts/lib/power_assertion.sh` records as insufficient — the gap occurred with an
assertion held).

*What was missing:* nothing in the live path even checked. `assert_power_settings_sane`
existed but was called only by `start_archiver.sh` and `start_forward_paper.sh` —
`launch_live.sh` never called it, and it only printed a warning in any case.

*Implemented:* `guard_assert_power_continuous()` in `scripts/lib/env_guard.sh`, wired
into `launch_live.sh` LAYER 3, and re-checked by `live_agent.sh` before it restores a
session. It reads the host's actual `pmset` state and **aborts** unless
`disablesleep=1` or idle `sleep=0`. R2 is therefore enforced against the machine, not
against this document. A live engine can no longer be started — or resurrected by the
supervisor — onto a host that will suspend it.

- **Residual risk (two vectors, both narrower than the original):**
  1. **Clamshell sleep is still possible.** `disablesleep` is **not** set (`pmset -g`
     shows no `disablesleep` entry; 0 occurrences in `pmset -g custom`). `sleep 0`
     stops *idle* sleep only — closing the lid still suspends the host, and
     `caffeinate` cannot block that. Sustained operation requires the lid open, an
     external display, or `sudo pmset -a disablesleep 1`. Note that
     `guard_assert_power_continuous()` accepts `disablesleep=1` as well as
     `sleep=0`, so applying it is a strict improvement and needs no code change.
  2. **Detection of a wedged-but-alive engine is logged, not alerted.**
     `scripts/watchdog.sh` flags heartbeat age > 600 s, but alerting is
     **DEGRADED — no provider configured** (telegram/discord/email all unset), so a
     suspension that does occur is recorded locally and nobody is told. Tracked
     separately as R3/R4; it is the reason "undetected" is only partly retired here.

  **This risk depends on owner configuration and can silently regress.** If anyone
  later runs `sudo pmset -a sleep <n>` with n>0, the host becomes unsafe again — but
  `guard_assert_power_continuous()` re-reads `pmset` at every launch and inside
  `live_agent.sh` before any restart, so the regression blocks startup instead of
  producing another silent multi-hour gap.

---

## HIGH

### R3 — Nothing monitors the monitor

- **Evidence:** validation monitor stopped at `2026-07-26T12:09:03Z`; 21 snapshots
  where ~50 were due; no error logged, no alert raised.
- **Impact:** observability can fail silently, so absence of alerts cannot be treated
  as evidence of health — the precise failure mode this campaign existed to catch,
  recurring inside the monitoring layer.
- **Likelihood:** High (observed).
- **Status:** OPEN.

### R4 — Heartbeat freshness is never evaluated by a live consumer

- **Evidence:** heartbeat frozen at `2026-07-27T10:53:02Z` for 7.82 h with no
  consequence; a 22 h stale heartbeat produced no failure.
- **Impact:** the strongest available liveness signal is written but not acted upon.
- **Likelihood:** High.
- **Status:** OPEN.

### R5 — Live order path has never been executed

- **Evidence:** 0 order calls and 0 private endpoints across the entire campaign, by
  design (`PRIVATE EXCHANGE CALLS DISABLED`; launcher blanks credentials).
- **Impact:** order placement, rejection handling, partial fills, real slippage and
  exchange error codes are entirely unvalidated.
- **Likelihood:** Certain to matter at go-live.
- **Status:** OPEN by design — closing it requires a controlled live-order phase.

---

## MEDIUM

### R6 — Statistically meaningless trade sample

2 closed trades, both profit-lock stops, aggregate net −0.00875619. No take-profit was
reached, so `TP_TOUCH` and `PARTIAL_EXIT` are unexercised in live conditions. **Status:** OPEN.

### R7 — Risk-engine blocking never exercised

0 risk rejects in the run window (`RISK_DECISION` PASS 5 / FAIL 0). Gate behaviour is
unproven outside unit tests. **Status:** OPEN.

### R8 — Configuration identity not traceable

`manifest.json` records `config_version_hash 17524444…` while the hash stamped on the
actual trades is `d6f53802…`. The git commit does match (`cda8187`). Reproducing the
exact configuration of the run from the manifest alone is not possible. **Status:** OPEN.

### R9 — Funnel accounting discrepancy

`FORWARD_PAPER_LINK` PASS = 3 versus `EXECUTABLE_DECISION` PASS = 2: three trades
opened against two logged executable decisions. The event store is internally
consistent; the funnel counter is not. **Status:** OPEN, unexplained.

### R10 — Position left unresolved at freeze

`paper_2b760b2f13c268cf9af7` open with 20 events and no `TRADE_CLOSED`
(`unresolved_open_trade_count: 1`). In live trading the equivalent state is an
unmanaged position. **Status:** OPEN.

---

## LOW

### R11 — Stale PID files persist

`state/bot.pid` (96180), `validation_72h/monitor.pid` (90258),
`validation_72h/supervisor.pid` (90257) reference dead processes. Deliberately not
removed by the audit. **Status:** OPEN.

### R12 — Archiver stopped with the reboot and did not restart

No throughput threshold is defined, so archive degradation would not be flagged
(Phase 4 case G of the pre-flight gate remains unimplemented). **Status:** OPEN.

### R13 — Storage growth unbounded in principle

`logs/` 1.1 G and `reports/` 1.1 G at audit. Disk actually improved to 31 Gi free
during the run, so this did not bite. **Status:** OPEN, low priority.

---

## Mitigated in RC2 (2026-07-27) — not yet closed

Mitigation is not resolution. Each entry below has a control in place that is
tested, but none has been proven under the real-world condition it guards
against. Statuses above remain OPEN until that proof exists.

| Risk | Control added | Commit | Verification |
|---|---|---|---|
| **R1** no recovery after host restart | `launchd` agent `com.cgc.forward` (RunAtLoad + KeepAlive) supervising the keepalive | `5f862ef` | plist valid, scripts pass syntax; **NOT yet verified by a real reboot** |
| **R3** nothing monitors the monitor | `scripts/watchdog.sh`, independent of supervisor and monitor, with its own heartbeat | `71f931a` | verified against the real post-mortem state: detected the 31.7 h dead monitor |
| **R4** heartbeat freshness unused | watchdog enforces `MAX_HEARTBEAT_AGE_SEC` and alerts | `71f931a` | verified: detected a 9 h stale heartbeat |
| Config single-variable exposure (audit F2) | `.env.forward` / `.env.live` separation, `env_guard.sh` invariants, 4 authorisation layers | `a316d45`, `bd5bf03` | 8 isolation tests pass; guard rejects the ambient 40-symbol posture |
| Alerting absent | `scripts/lib/alert.sh` (telegram/discord/email) | `4dcea38` | self-test passes; **no provider configured — DEGRADED** |

**R2 (host sleep) is untouched and remains a hard blocker.**

---

## Live-execution blocker (2026-07-28)

### R14 — Duplicate live entry order after an ambiguous response

- **Evidence:** `clients/bitget_base_client.py:151` retried every HTTP method,
  including the order POST, on 408/429/500/502/503/504 and on transport errors.
  `execution/execution_service.py` contained zero occurrences of
  `client_oid`/`clientOid`, so Bitget had no key with which to deduplicate.
  Recorded as a live-deployment blocker in `8cdf51e`.
- **Impact:** an entry that Bitget accepted but whose response was lost would be
  resent, creating a **second real position** — breaching `MAX_OPEN_POSITIONS`,
  possibly unprotected.
- **Likelihood:** Certain over time; every 5xx or read timeout on the entry POST
  was a coin flip.
- **Status:** **RESOLVED 2026-07-28** — commits `f50dab5` (deterministic
  clientOid), `e679250` (intent persistence), `d1837e0` (retry classification),
  `612110e` (reconciliation + restart recovery), `2f86561` (deterministic tests).
  Design: `docs/LIVE_ENTRY_IDEMPOTENCY.md`.
- **Residual:** mocked tests prove code behaviour, not Bitget's. The first real
  order is owner-operated final verification (evidence list in §6 of the design
  doc). Bitget's own duplicate-`clientOid` rejection semantics are **not**
  exercised by these tests.

### R15 — Fail-safe close opened an opposite position instead of closing

- **Evidence:** `ExecutionService._fail_safe_close()` called
  `place_futures_market_order(trade_side="close")`. That function swallows
  `trade_side` in `**_` and hardcodes `"tradeSide": "open"`, deriving `holdSide`
  from the (inverted) side — so the emergency close of an unprotected position
  submitted a **new opening order in the opposite direction**.
- **Impact:** the safety mechanism guarding an unprotected position would have
  doubled exposure rather than removing it. Never triggered live (execution has
  been disabled since 2026-07-13).
- **Likelihood:** Certain whenever the fail-safe fired.
- **Status:** **RESOLVED 2026-07-28** — `612110e` routes the fail-safe through
  the verified reduce-only `close_futures_position()` path; regression test
  `tests/test_entry_path_audit.py::test_fail_safe_close_uses_the_reduce_only_path_not_a_new_opening_order`.

---

## Resolved

| Risk | Date | Resolving commits |
|---|---|---|
| **R14** duplicate live entry order | 2026-07-28 | `f50dab5`, `e679250`, `d1837e0`, `612110e`, `2f86561` |
| **R15** fail-safe close opened a position | 2026-07-28 | `612110e` |
