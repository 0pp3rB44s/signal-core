# 09 — Emergency Procedures

> **Critical fact:** stopping the process does **not** cancel exchange orders and does
> **not** close positions. Process control and exposure control are separate actions.
> There is no automated exchange-side kill switch in this system.

## E1 — Stop everything now

```bash
touch state/forward_paper_keepalive.stop     # stop the supervisor FIRST
touch validation_72h/monitor.stop
kill "$(cat state/bot.pid)"                  # SIGTERM: handled, writes shutdown record
pgrep -f 'app\.main'; pgrep -f forward_paper_keepalive; pgrep -f caffeinate
```

Stop the supervisor first, or it will restart the bot underneath you within 120 s.

Escalate to `kill -9` only if SIGTERM does not terminate the process; that skips the
shutdown record.

## E2 — Open position with the bot down

The bot is not managing it. Protective orders may or may not be resting on the exchange.

1. Open the Bitget UI and establish ground truth: position, size, and any resting
   stop/take-profit.
2. Decide manually: leave protective orders in place, adjust, or close.
3. **Closing a position is a financial action and is the owner's to perform** — do not
   delegate it to an automated agent.
4. Record what was done and when.

## E3 — Suspected live-execution safety breach

Treat as SEVERITY 0. Any of: an order call in paper mode, a private endpoint reached
while `FORWARD_PAPER_ONLY=true`, more positions than configured, a trade without a stop.

1. Stop immediately (E1).
2. **Preserve evidence — do not clean up.** Copy `logs/`, `state/`, `data_store/`.
3. Verify credentials: rotate the API key if a breach is plausible.
4. Do not restart without written owner approval.

Detection: `grep -icE 'place_order|place_futures|GET_ACCOUNTS|Private API OK' logs/forward_paper.out`
— expected `0` in paper mode (it was 0 across the entire campaign).

## E4 — Event chain corrupt

The chain fails closed; `read_events()` raises. **Do not edit or truncate the file** —
any edit is detected and destroys the audit trail.

1. Stop the bot.
2. Copy `data_store/forward_paper_events.jsonl` aside, unmodified.
3. Record the failing line from the exception.
4. Treat all downstream analytics from that point as untrusted.

## E5 — Scan loop wedged (`SCAN_LOOP_FAILING` / `SCAN_PRODUCED_NO_MARKET_DATA`)

The supervisor deliberately will **not** restart on these. That is correct: it is a code
or configuration fault, and restarting would mask it.

1. `bash scripts/check_forward_paper.sh` — record the status.
2. Inspect for `BITGET_HTTP_ERROR`, `400171`, `SCAN_CYCLE_FAILED`.
3. `400171` means an invalid granularity reached the API — see
   [04_CONFIGURATION_GUIDE.md](04_CONFIGURATION_GUIDE.md).
4. Fix the cause, then restart. Do not loop-restart.

## E6 — Host rebooted / process vanished

Known failure F1: nothing restarts automatically.

```bash
cat state/last_shutdown.json     # reason, exit_code, signal
sysctl -n kern.boottime          # was it a reboot?
git status --porcelain           # MUST be empty or the launcher refuses
tmux new -s cgcbot 'bash validation_72h/supervise.sh'
```

Before restarting in live mode, **reconcile open positions against the exchange**.

## E7 — Disk exhaustion

`logs/` and `reports/` were ~1.1 G each at RC1. Rotation exists
(`com.cgc.cleanlogs`). Stop the bot, archive or prune old logs, verify free space, then
restart. Never delete `data_store/forward_paper_events.jsonl`.

## Severity model

| Severity | Examples | Action |
|---|---|---|
| **0 — safety breach** | real order capability enabled, private endpoint called, position exceeds max, missing stop | stop, preserve evidence, owner approval to restart |
| **1 — evidence-invalidating** | impossible fill, wrong PnL, chain corruption, duplicate order/fill, silent failure after executable plans | stop, preserve, minimal tested fix, restart validation from zero |
| **2 — recoverable** | exchange outage, DNS failure, crash with correct supervisor restart | allow recovery, record, continue |
| **3 — warning** | transient warning, elevated latency, quiet market with no trades | record, continue |

## Contacts and authority

Financial actions — closing positions, cancelling orders, moving funds, enabling live
execution — are **owner-only**. No automated process and no agent may perform them.
