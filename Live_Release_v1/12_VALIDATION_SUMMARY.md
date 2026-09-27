# 12 — Validation Summary

Full narrative: `docs/FORWARD_PAPER_VALIDATION.md`. Evidence:
`validation_72h/archive/RC1_forward_paper_validation.tar.gz`
(sha256 `3b5bf715…0739`, 912 KB, 63 files, integrity 63/63 verified twice).

## Outcome in one line

**The 72-hour reliability criterion was not met: 20.56 h effective, 28.5 %.**

## Two runs

| | Run 1 — INVALID | Run 2 — audited |
|---|---|---|
| Start | 2026-07-25T14:02:10Z | 2026-07-25T16:09:41Z |
| End | 15:53Z (operator) | 2026-07-27T10:53:51Z (SIGTERM, host reboot) |
| Baseline | `221efa0` | `cda8187` |
| Result | no usable evidence | FAIL vs criterion |
| Cause | `granularity=1h` rejected — HTTP 400171 × 742; all 106 scans failed while health said HEALTHY | terminated by host reboot at T+42.75 h; 22.19 h lost to host sleep |

Run 1's event store carries the digest of an **empty file** (`e3b0c442…`) —
cryptographic proof it wrote nothing.

## Time accounting (run 2)

```
process lifetime    42.75 h
suspended         − 22.19 h   host sleep, single gap, same PID, no error
effective scanning  20.56 h   = 28.5 % of 72 h
dead before audit    7.82 h   unnoticed
```

## Proven

| Area | Evidence |
|---|---|
| Safety | 13/13 controls; **0 private endpoints, 0 order calls**; 3 public paths; **9 892 requests, 100 % HTTP 200** |
| Event integrity | hash chain **VALID across 49 events** after a 22 h suspension *and* a SIGTERM; 0 duplicate ids/semantics, 0 terminal conflicts, 0 orphans, 0 events after close |
| Accounting | both closed trades reconcile independently to **< 1e-6**; exits inside the traded range — no impossible fills |
| Stability | 0 tracebacks, 0 scan-cycle failures, 0 fail-closed writes, 0 HTTP errors, 0 × 400171 |
| Network resilience | a **real** DNS outage absorbed by retry/backoff; cycle completed |
| Restart recovery (process) | detached supervisor recovered the bot unaided in 91 s and 121 s, no duplicates |
| Config safety | 16/16 combinations, 0 violations; `forward_paper_only` forces execution off |

## Not proven

| Area | Reason |
|---|---|
| Continuous uptime | 20.56 h of 72 h |
| Sleep prevention | 22.19 h suspension despite an active assertion |
| Restart after host reboot | 7.82 h dead, no recovery |
| Monitoring continuity | monitor stopped silently 22.7 h before the end |
| Live order path | never executed — 0 private calls by design |
| TP / partial-exit paths | no take-profit was reached |
| Risk-gate blocking | 0 rejects in the window |

## Runtime and performance

| Metric | Value |
|---|---|
| Scan cycles | 1 177 (heartbeat agrees with log) |
| Cadence | median 63 s, p95 63 s (60 s configured) |
| Scan duration | ≈ 3 s |
| API latency | median 317.2 ms, p95 344.1 ms, max 1 313.0 ms, σ 33.8 |
| Restarts / supervisor interventions | 0 / 0 |
| CPU / memory | **evidence not available** |

## Trades

3 opened, 2 closed, 1 unresolved. All `low_vol_reclaim` / LTCUSDT / LONG / 15m.

| Trade | Fill | Exit | Reason | Gross | Fees | Net |
|---|---|---|---|---|---|---|
| `paper_50e14853…` | 46.750000 | 46.796750 | STOP_LOSS | +0.02832000 | 0.03400099 | −0.00568099 |
| `paper_dec390d7…` | 47.100000 | 47.147100 | STOP_LOSS | +0.01533000 | 0.01840520 | −0.00307520 |
| `paper_2b760b2f…` | 47.160000 | — | OPEN at freeze | — | — | — |

Aggregate closed: gross +0.04365000, fees 0.05240619, **net −0.00875619**.

Both were profit-lock stops: positive gross turned negative by fees. **Sample size 2 —
no inference of any kind is possible.** This is not a performance result and must never
be cited as one. The platform position is unchanged: **no proven edge**.

## Defects found and fixed during the campaign

| # | Defect | Detection |
|---|---|---|
| D1 | `ContractSpec` not serialisable — aborted every paper write | 0-byte event store for 11 days against 147 executable plans |
| D2 | Scan loop had no exception handling | 20 h undetected outage |
| D3 | Break-even stop booked profit at an untraded price | stop 74.004931 vs candle high 73.947 |
| D4 | `granularity=1h` rejected by Bitget | 742 × HTTP 400171 |
| D5 | Failed scan published `scan_cycle_complete` | 106 cycles "complete" with `snapshot_count: 0` |

All five carry regression tests. Suite: **339 passed, twice.** D4 and D5 were each
verified to reproduce with their fix reverted.
