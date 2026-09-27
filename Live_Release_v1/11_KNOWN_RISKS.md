# 11 — Known Risks carried into Live Release v1

Full register with evidence: `docs/RISK_REGISTER.md`. Limitations:
`docs/KNOWN_LIMITATIONS.md`. **None of these is fixed in RC1.**

Totals: **2 Critical · 3 High · 5 Medium · 3 Low.**

## Critical — block live deployment

| ID | Risk | Evidence |
|---|---|---|
| **R1** | No recovery after host restart | boot 2026-07-27T10:54:07Z, 16 s after SIGTERM; nothing ran for 7.82 h; supervisor is a `nohup` loop |
| **R2** | Host sleep suspends the process undetected | 22.19 h log gap, same PID, no error, despite an active `caffeinate` assertion; idle sleep is 1 min |

**Live impact:** either risk leaves an open position unmanaged for hours — stops not
evaluated, targets not enforced.

## High

| ID | Risk | Evidence |
|---|---|---|
| **R3** | Nothing monitors the monitor | observation loop stopped 2026-07-26T12:09:03Z; 21 of ~50 snapshots; no error |
| **R4** | Heartbeat freshness has no live consumer | heartbeat frozen 7.82 h with no consequence |
| **R5** | Live order path never executed | 0 order calls, 0 private endpoints across the campaign |

## Medium

| ID | Risk |
|---|---|
| **R6** | Trade sample statistically meaningless — 2 closed trades, net −0.00875619 |
| **R7** | Risk-engine blocking never exercised — `RISK_DECISION` PASS 5 / FAIL 0 |
| **R8** | Config identity not traceable — manifest `17524444…` vs trades `d6f53802…` |
| **R9** | Funnel accounting discrepancy — 3 `FORWARD_PAPER_LINK` vs 2 `EXECUTABLE_DECISION` |
| **R10** | Position left unresolved at freeze — `unresolved_open_trade_count: 1` |

## Low

| ID | Risk |
|---|---|
| **R11** | Stale PID files reference dead processes |
| **R12** | Archiver stopped at reboot; no throughput threshold defined |
| **R13** | Storage growth unbounded in principle (`logs/` and `reports/` ~1.1 G each) |

## Additional gaps recorded in this release

| Gap | Detail |
|---|---|
| No off-host backup | `data_store/`, `state/` and telemetry are local only; total host loss = total history loss |
| No off-host alerting | every check is pull-based and local |
| Rollback never rehearsed | procedure documented, never executed |
| Risk engine reads absolute paths | `BASE_PATH`/`REPORTS_PATH` outside the commit — risk behaviour not reproducible from the commit alone |
| No exchange-side kill switch | process exit does not cancel orders or close positions |

## Risk acceptance

No risk in this register has been accepted or waived. R1 and R2 are hard blockers for
Stage 4 in [05_DEPLOYMENT_GUIDE.md](05_DEPLOYMENT_GUIDE.md).
