# 72-HOUR FORWARD-PAPER VALIDATION — PRE-FLIGHT REPORT

Phases 0–5 complete. The clock is running. The Phase 10 verdict is **not** issuable
until 72 real hours have elapsed.

- **Clock start:** 2026-07-25T14:02:10Z (local 16:02:10 CEST, +0200)
- **Planned end:** 2026-07-28T14:02:10Z
- **Baseline commit:** `221efa0` on `main`, working tree clean
- **Config hash:** `17524444fcd68923…`

---

## Phase 0 — baseline

| Item | Value |
|------|-------|
| Machine | MacBook-Air-van-Bryon-2, arm64, macOS 26.5.1 |
| Python / venv | 3.11.15 / `.venv` |
| Branch at entry | `fix/power-assertion-collection` @ `2466c5e` |
| Disk available | 23 Gi (89 % capacity) |
| Idle sleep | **1 minute** — power assertion is mandatory, not optional |
| Exchange connectivity | public candles OK, code 00000, 0.36 s |
| Paper store at entry | 0 events, chain VALID, 0 open positions |
| Archiver | running (pid 49787) with its own caffeinate |

**Blockers found and cleared before the clock started:**

1. **Dirty/unexplained tree** — 7 modified + 5 untracked files. Resolved by
   assembling an explicit baseline (below).
2. **Stale `state/bot.pid`** (pid 13587, from 2026-07-18, not running) — removed.
3. **Stale `state/supervisor.stop`** flag (2026-07-15) — removed.
4. **`forward_paper_only` was `False`** in the ambient config, and
   `EXECUTION_MODE=LIVE`. The strict launcher now forces both.
5. **The scan loop had no exception handling at all.** `run()` called
   `_scan_cycle()` bare, so any transient network error killed the process — the
   documented 20-hour undetected outage. The fix existed only in unmerged PR #14.

### Baseline assembly

Three open PRs each fixed a distinct prerequisite and touched disjoint files, so
all three were merged into `main` locally (not pushed, PRs left open):

| PR | Fixes | Why the run needs it |
|----|-------|----------------------|
| #13 | ContractSpec serialization | without it no paper trade is ever written |
| #14 | scan-loop resilience | without it the run dies on the first DNS blip |
| #15 | power assertion + strict launcher | without it the host sleeps and kills the run |

The uncommitted lifecycle work (smoke strategy, production-path lifecycle tests,
protective-stop guard, health-check hardening) was merged on top and the two
overlapping serialization approaches reconciled into one layered defence:

- `market_features.instrument_context()` sanitises at the **producer** (PR #13);
- `store.jsonable()` recursively pre-sanitises every payload on the write path;
- `canonical_json(default=json_safe)` is the last-resort net and **logs loudly**;
- `service._market_context()` drops the redundant `live` blob and its raw
  orderbook ladder.

---

## Phase 1 — pre-run test gate

| Check | Result |
|-------|--------|
| Full suite, run 1 / 2 / 3 | **302 passed** each time (10.6 s / 16.6 s / 14.4 s) |
| Targeted regressions (serialization, scan-loop, power, lifecycle, smoke safety, crash-resume) | 46 passed |
| Failed / skipped | 0 / 0 |
| Production paper store contaminated by tests | **No** — md5 identical before and after |
| Live-state leakage | none remaining; risk-manager report paths patched per-test |
| Script syntax (`bash -n` × 5 launchers/supervisors) | all OK |
| Real-money impossible | **16/16 config combinations, 0 violations** |
| `forward_paper_only` coerces execution off | yes, and `EXECUTION_MODE` → `DRY_RUN` |
| Launcher blanks credentials | yes (`BITGET_API_KEY/SECRET/PASSPHRASE=""`) |

---

## Phase 2 — supervisor validation

Supervisor is `scripts/forward_paper_keepalive.sh` (health-check driven, restarts
**only** via the strict launcher, fail-closed after 3 restarts / 1800 s).

| Test | Result |
|------|--------|
| Exactly one bot / one supervisor | yes |
| Kill bot → health check | `PROCESS_NOT_RUNNING` |
| Restart latency, foreground invocations | 6 s, 2 s |
| Restart latency, **detached** supervisor unaided | **91 s** (120 s poll interval) |
| Duplicate bot processes after restart | 0 |
| Power assertion rebound to new pid | yes; old caffeinate died with old bot |
| Restart-loop protection tripped incorrectly | no |
| Supervisor logs created/updated | yes |

### Pre-run repair PR-1 (would have invalidated the whole run)

Killing the bot and watching the **detached** supervisor revealed it could not
recover: the strict launcher refuses a dirty tree, and the harness generated
untracked runtime files (`monitor_loop.sh`, `*.pid`), so every automatic restart
failed with `working tree is not clean` and the bot stayed down.

Undetected this reproduces the original failure mode exactly — healthy-looking
until the first crash, then silently dead. Fixed by committing the loop script and
gitignoring runtime pids (`221efa0`). Re-verified: recovery unaided in 91 s.

The clock was restarted from zero for this. No evidence had accumulated
(invalidated start: 2026-07-25T13:55:30Z).

---

## Phase 3 — retryable network failure

Covered at the real `_scan_cycle_iteration` seam by 6 tests (PR #14), none of which
mock the loop away: retryable error does not terminate the loop; no exception class
escapes; sustained outage then recovery resets the counter; failure count and
`error_type` are published to the heartbeat; DNS failure in `fetch_contracts` skips
the cycle without raising; the scan lock is released on failure.

**Limitation:** no live machine-wide network fault was injected. Blackholing
`api.bitget.com` needs `sudo` and affects the whole host, so it was not done without
owner approval. Resilience is therefore proven at the code seam, not against a real
outage — a real one during the 72 h window will provide that evidence.

---

## Phase 4 — health-check truthfulness

| Case | Expected | Result |
|------|----------|--------|
| A — alive, normal pipeline | PASS | `status=HEALTHY` ✓ |
| B — executable plans, no paper output | FAIL | logic present (`FORWARD_PAPER_OUTPUT_MISSING`, and non-zero exit on `FORWARD_PAPER_FAILED_CLOSED`); not yet exercised with live executable plans |
| C — process dead | FAIL | `status=PROCESS_NOT_RUNNING` ✓ |
| D — heartbeat stale | FAIL | threshold implemented; not exercised |
| E — event-chain corruption | FAIL | **5/5 modes detected** (tampered checksum, tampered payload, broken previous_hash, bad sequence, wrong dataset); clean control reads ✓ |
| F — inconsistent open position | FAIL | reconstruction reports `unresolved_open_trade_count` / `fragmented_transition_count`; not exercised |
| G — archiver throughput | FAIL/DEGRADED | thresholds not defined — **open gap** |
| H — no executable plans, zero trades | may stay healthy | `events=0`, `status=HEALTHY` ✓ (frequency is not forced) |

B, D, F were verified by code inspection only. G has no defined threshold yet.

---

## Phase 5 — run configuration

| Parameter | Value |
|-----------|-------|
| Mode | strict forward-paper only; execution technically disabled |
| Symbol | **LTCUSDT** — chosen on evidence: highest executable-plan frequency (18 in 7 days = 2.57/day) |
| Timeframe | 15m primary / 1h confirmation |
| Strategy | real detectors, `low_vol_reclaim` dominant (114 of 147 executable plans). **Smoke harness disabled.** |
| Max open positions | 1 |
| Scan interval | 60 s |
| Fees | 12 bps round-trip, taker |
| Slippage | entry real; exit fills exact at stop/target (modelled 0.0) |
| Compounding / leverage escalation | none |
| Baseline counters | 0 paper events; funnel 75 807 events / 147 executable (deltas measured from here) |

Running processes: bot `30953`, power assertion `31057`, supervisor `13967`,
monitor `13974`, archiver `49787`. Zero private/order calls in the log.

---

## Phase 6 — observation

`validation_72h/monitor.sh` writes a read-only JSON snapshot hourly to
`validation_72h/snapshots/` (health, pids, heartbeat, chain status, event/trade
counts, funnel deltas, restarts, failed-closed and retryable counters, disk, CPU,
memory, log growth). It never restarts or steers the bot.

## Known operational risks for the window

1. **Disk: 23 Gi free at 89 % capacity**, and `logs/` is already 1.2 G. Growth is
   not yet measured over a full day. If it accelerates this is the most likely
   cause of a mid-run failure.
2. **Archiver throughput has no defined threshold** (Phase 4 G), so a silent
   archive degradation would not be flagged.
3. **Restart budget is 3 per 1800 s**, then the supervisor fail-closes and waits
   for a human — correct behaviour, but it means a crash-loop ends the run.
4. Resilience to a genuine network outage is unproven (Phase 3 limitation).

## Verdict

**PRE-FLIGHT: PASS. Clock started 2026-07-25T14:02:10Z.**

**UNATTENDED FORWARD-PAPER RELIABILITY: NOT YET PROVEN** — 72 hours have not
elapsed. This is the only honest statement available at this point.
