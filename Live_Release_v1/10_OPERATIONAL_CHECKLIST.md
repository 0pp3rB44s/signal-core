# 10 — Operational Handbook & Operator Checklist

## Daily golden rules

1. **A live process is not a working process.** Check `snapshot_count`, not liveness.
2. **Absence of alerts is not evidence of health.** The monitor can die silently — it did.
3. **A dirty working tree breaks every automatic restart**, silently.
4. **Stopping the process does not close positions.**
5. Financial actions are owner-only.

## Start of day (5 min)

```bash
cd /Users/bryonprivee/Desktop/bitget_ai_agent/bitget_ai_agent_phase7
git status --porcelain && git describe --tags
pgrep -f 'app\.main'; pgrep -f forward_paper_keepalive; pgrep -f caffeinate
bash scripts/check_forward_paper.sh
```

- [ ] Tree clean, expected tag
- [ ] Exactly one bot, one supervisor, one power assertion
- [ ] `status=HEALTHY`
- [ ] Heartbeat `timestamp` recent **and** `snapshot_count > 0`
- [ ] Cycle counters advancing since yesterday
- [ ] Disk headroom acceptable
- [ ] Restart history: `wc -l state/forward_paper_keepalive.history` (budget 3/1800 s)
- [ ] Archiver alive: `pgrep -f archiving.run_archiver`

## Health interpretation

| Status | Meaning | Action |
|---|---|---|
| `HEALTHY` | normal | none |
| `DEGRADED` | ≥1 consecutive scan failure | investigate; do not restart-loop |
| `SCAN_PRODUCED_NO_MARKET_DATA` | cycle completed with zero snapshots | **code/config fault** — E5 |
| `SCAN_LOOP_FAILING` | ≥3 consecutive failures | E5 |
| `FORWARD_PAPER_OUTPUT_MISSING` | executable plans, no paper output | investigate the writer |
| `PROCESS_NOT_RUNNING` | dead | E6 |

## Weekly (15 min)

- [ ] Validate the event chain (`read_events()` succeeds)
- [ ] Review `reports/forward_paper_data_quality.json`: `unresolved_open_trade_count`,
      `duplicate_*`, `terminal_close_conflict_count` — all should be 0 except known opens
- [ ] Independently recompute one closed trade's accounting
- [ ] Review error/warning counts against the RC1 baseline (2 ERROR / 44 WARNING per 42.75 h)
- [ ] Confirm log rotation is keeping `logs/` bounded
- [ ] Re-run the suite: 339 passed

## Routine restart

```bash
touch state/forward_paper_keepalive.stop
kill "$(cat state/bot.pid)"
pgrep -f 'app\.main'                 # expect nothing
rm -f state/forward_paper_keepalive.stop state/forward_paper_keepalive.history
git status --porcelain               # MUST be empty
bash scripts/start_forward_paper.sh 60
bash scripts/check_forward_paper.sh
```

## What an operator must never do

- Edit `data_store/forward_paper_events.jsonl` — hash-chained; any edit is detected and
  destroys the audit trail.
- Delete the event store or outcomes CSV.
- Set `EXECUTION_ENABLED=true` without written owner authorisation.
- Restart repeatedly through `SCAN_LOOP_FAILING` — it masks a real defect.
- Leave the working tree dirty.
- Assume the local journal reflects exchange truth.

## Escalation

| Symptom | Procedure |
|---|---|
| Any safety-breach indicator | [09](09_EMERGENCY_PROCEDURES.md) E3 — SEVERITY 0 |
| Chain corruption | E4 |
| Wedged scan loop | E5 |
| Host rebooted | E6 |
| Disk exhaustion | E7 |
| Open position, bot down | E2 — **owner decision** |

## Known operational burdens (unresolved in RC1)

- No boot-persistent supervision — after a reboot an operator must start it manually (R1).
- Host sleep can suspend the process silently (R2).
- Nothing monitors the monitor (R3); heartbeat freshness has no consumer (R4).
- No off-host alerting — all checks are pull-based and local.
