# 07 — Recovery & Disaster Recovery

Distinguishes what is **evidenced to recover** from what is **known to fail**. The
distinction matters: three of the failure modes below were observed, not imagined.

## Proven recovery

| Scenario | Behaviour | Evidence |
|---|---|---|
| Process crash, host awake | supervisor restarts via the strict launcher | 6 s and 2 s foreground; **91 s and 121 s detached** (120 s poll) |
| Duplicate suppression | exactly one bot after restart; power assertion rebound | 0 duplicates observed |
| Transient network failure | retry with backoff (1.25 s → 2.5 s), cycle completes | **real** DNS outage 2026-07-26 10:32:47 recovered on attempt 3 |
| Unclean interruption | hash chain valid; open position reconstructed from the log | chain VALID across 49 events after a 22 h suspension **and** a SIGTERM |
| Duplicate open after restart | deterministic `candidate_id`; store rejects the second open | strategy re-emitted a plan post-restart; store deduped it |
| Clean shutdown | signal captured, shutdown record written | `RUNTIME_SIGNAL_RECEIVED signal=SIGTERM exit_code=143` |

## Known recovery failures

### F1 — No recovery after host restart (CRITICAL, R1)

Host booted 2026-07-27T10:54:07Z, 16 s after the bot's SIGTERM. **Nothing ran for the
next 7.82 h.** The supervisor is a `nohup` shell loop and does not survive a reboot; no
boot-persistent mechanism supervises it.

Manual recovery:

```bash
git status --porcelain          # MUST be empty or the launcher refuses
tmux new -s cgcbot 'bash validation_72h/supervise.sh'
# or a single check/restart pass:
bash scripts/forward_paper_keepalive.sh
```

### F2 — Host sleep suspends the process (CRITICAL, R2)

A `caffeinate -ims -w <pid>` assertion was held and the host still suspended the process
for **22.19 h**. System idle sleep is 1 minute on this hardware. The suspension produced
no log line, no alert and no health failure — the process simply froze and resumed.
There is no independent check that the assertion is alive.

### F3 — Nothing recovers or reports the monitor (HIGH, R3)

The observation loop stopped at 2026-07-26T12:09:03Z and never resumed. Nothing detected
it. **Absence of monitor output is not currently treated as a fault.**

### F4 — Heartbeat staleness has no live consumer (HIGH, R4)

The heartbeat froze for 7.82 h with no consequence. Its *contents* are evaluated by the
health check; its *freshness* is enforced by nothing that runs.

## Disaster recovery

### Total host loss

| Asset | Recoverable? |
|---|---|
| Code | Yes — git, tagged |
| Validation evidence | Yes — `validation_72h/archive/RC1_forward_paper_validation.tar.gz` (checksummed, in git) |
| Forward-paper event store | **No** — `data_store/` is gitignored, local only |
| Funnel telemetry | **No** — local only |
| Runtime state | **No** — local only |
| Open exchange positions | Visible on the exchange; **not** managed while the host is down |

**Gap:** there is no off-host backup of the event store, telemetry or state. Total host
loss means total loss of trading history not already archived.

### Corrupted event chain

The chain fails closed. `read_events()` raises `ForwardPaperCorruptionError` on a
checksum mismatch, broken `previous_hash`, non-contiguous sequence or wrong dataset —
all five modes verified detectable. **Do not repair by editing.** Preserve the file,
record the failure, and treat downstream analytics as untrusted from that point.

### Unresolved open position

Observed at the RC1 freeze (`unresolved_open_trade_count: 1`). In paper this is a
reporting state. In live it is **an unmanaged position**: reconcile against exchange
truth before restarting, and never assume the local journal is authoritative.

## Recovery checklist

- [ ] Establish what is running: `pgrep -f 'app\.main'`, keepalive, caffeinate, archiver
- [ ] Read `state/last_shutdown.json` — reason, exit code, signal
- [ ] Check heartbeat age and `snapshot_count`
- [ ] `bash scripts/check_forward_paper.sh`
- [ ] Validate the event chain
- [ ] **Reconcile open positions against the exchange**
- [ ] Confirm `git status --porcelain` is empty
- [ ] Restart via the strict launcher; confirm one bot and one assertion
