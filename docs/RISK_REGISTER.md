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
- **Status:** OPEN.

### R2 — Host sleep suspends the trading process undetected

- **Evidence:** 22.19 h log gap (`2026-07-26 14:38:21` → `2026-07-27 12:49:44`), same
  PID 96180, no error line, resuming with `report_age_hours=25.1`. A
  `caffeinate -ims -w 96180` assertion was held and did not prevent it. `pmset` shows
  system idle sleep at 1 minute.
- **Impact:** an open position is unmanaged for hours while the process believes it is
  running; stop-losses are never evaluated.
- **Likelihood:** High on this hardware.
- **Status:** OPEN. Mechanism not fully established — whether the assertion process
  died first or was held and proved insufficient is **evidence not available** (process
  table lost at reboot; OS sleep records rotated).

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
