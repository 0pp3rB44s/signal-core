# Recovery procedures — observed behaviour and known gaps

What the system is *evidenced* to recover from, and what it demonstrably does not.
Derived from the Forward-Paper campaign of 2026-07-25 → 2026-07-27; evidence in
`validation_72h/archive/`.

This document records observed behaviour. It does not propose code changes.

---

## Recovery that is proven

### Process crash while the host is awake

The supervisor (`scripts/forward_paper_keepalive.sh`) is health-check driven: it reads
`scripts/check_forward_paper.sh` and restarts **only** through
`scripts/start_forward_paper.sh`, which re-asserts every safety condition.

| Test | Result |
|---|---|
| Kill bot, foreground supervisor invocation | recovered in 6 s, then 2 s |
| Kill bot, **detached** supervisor unaided | recovered in **91 s** and **121 s** (120 s poll interval) |
| Duplicate processes after restart | 0 |
| Power assertion rebound to new PID | yes; old `caffeinate` died with the old bot |
| Working tree after restart | still clean |

### Transient network failure

Proven against a **real** outage, not only in tests. On 2026-07-26 10:32:47 a genuine
DNS resolution failure occurred:

```
ERROR  BITGET_DNS_RESOLUTION_FAILURE  /market/contracts  attempt=1/3
WARN   BITGET_NETWORK_RETRY  sleep=1.25s  attempt=1
ERROR  BITGET_DNS_RESOLUTION_FAILURE  /market/contracts  attempt=2/3
WARN   BITGET_NETWORK_RETRY  sleep=2.5s   attempt=2
INFO   BITGET_API_LATENCY  /market/contracts  status=200  latency_ms=1117.78
```

A second failure hit `/market/candles` at 10:33:07 and also recovered.
`SCAN_CYCLE_COMPLETED` followed at 10:33:13. No duplicate paper order, no state damage,
`SCAN_CYCLE_FAILED` count 0.

### State survival across unclean interruption

The forward-paper hash chain was **VALID across all 49 events** after a 22.19 h process
suspension *and* a `SIGTERM`. 0 duplicate event ids, 0 duplicate semantic transitions,
0 terminal-close conflicts, 0 orphan events. An open position is reconstructed from the
event log alone; a persisted terminal intent is completed before newer candles are
evaluated, so a crash cannot strand a trade.

### Clean shutdown

`SIGTERM` is captured and recorded:

```
RUNTIME_SIGNAL_RECEIVED | signal=SIGTERM | exit_code=143
```

followed by a written `state/last_shutdown.json` carrying reason, exit code, signal and
cycle counters.

---

## Recovery gaps — evidenced failures

### G1 — No recovery after host restart (CRITICAL, risk R1)

Host booted `2026-07-27T10:54:07Z`, 16 s after the bot's `SIGTERM`. Nothing ran for the
next **7.82 h**. The supervisor was started with `nohup` and does not survive a reboot;
no boot-persistent mechanism supervises this harness.

**Manual recovery today:** from a clean `main` working tree,

```bash
tmux new -s cgc72h 'bash validation_72h/supervise.sh'
```

or a single check/restart pass:

```bash
bash scripts/forward_paper_keepalive.sh
```

**Precondition that is easy to miss:** `scripts/start_forward_paper.sh` refuses to
start on a dirty working tree. Any untracked runtime file therefore breaks *every*
automatic restart silently. This was pre-run repair PR-1 and remains a live operational
constraint.

### G2 — Host sleep is not reliably prevented (CRITICAL, risk R2)

A `caffeinate -ims -w <bot pid>` assertion was held and the host still suspended the
process for 22.19 h. System idle sleep is configured at **1 minute** on this hardware.
The suspension produced no log line, no alert and no health failure.

There is no independent check that the assertion process is alive.

### G3 — Nothing recovers or reports the monitor (HIGH, risk R3)

The validation monitor stopped at `2026-07-26T12:09:03Z` and never resumed. Nothing
detected it. Absence of monitor output is not currently treated as a fault.

### G4 — Heartbeat staleness has no live consumer (HIGH, risk R4)

`state/runtime_heartbeat.json` froze for 7.82 h with no consequence. The health check
evaluates heartbeat *contents* (stage, snapshot count, consecutive failures) but its
*freshness* is not enforced by any running consumer.

---

## Health-check verdicts

`scripts/check_forward_paper.sh` emits `status=` on stdout and a non-zero exit code:

| Condition | Status | Exit |
|---|---|---|
| Normal | `HEALTHY` | 0 |
| Process absent | `PROCESS_NOT_RUNNING` | non-zero |
| Paper writer failing | `FORWARD_PAPER_WRITE_FAILING` | 7 |
| Executable plans but no paper output | `FORWARD_PAPER_OUTPUT_MISSING` | 9 |
| Completed cycle with `snapshot_count=0` | `SCAN_PRODUCED_NO_MARKET_DATA` | 10 |
| ≥3 consecutive scan failures | `SCAN_LOOP_FAILING` | 11 |
| ≥1 consecutive scan failure | `DEGRADED` | 12 |

The supervisor deliberately **does not restart** on `DEGRADED`,
`SCAN_PRODUCED_NO_MARKET_DATA` or `SCAN_LOOP_FAILING`: it fail-closes for a human,
because restarting cannot fix a code-level defect and would only mask it. It restarts
only on `PROCESS_NOT_RUNNING` / `NOT_STARTED`, and gives up after 3 restarts in 1800 s.

---

## Stopping cleanly

```bash
touch state/forward_paper_keepalive.stop   # stop the supervisor loop
touch validation_72h/monitor.stop          # stop the observation loop
```

Then terminate the bot PID in `state/bot.pid`. Verify with:

```bash
pgrep -f 'app\.main'; pgrep -f forward_paper_keepalive; pgrep -f caffeinate
```
