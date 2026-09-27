# RC2 — Infrastructure Test Report

**Date:** 2026-07-27 · **Result: 25 passed, 0 failed.** No trading tests were run.

## Configuration isolation

| Test | Expected | Result |
|---|---|---|
| Live launcher with Critical risks open | abort | **PASS** — aborted: "2 Critical risk(s) still OPEN" |
| Forward guard fed a live config | abort | **PASS** — "requires FORWARD_PAPER_ONLY=true" |
| Live guard fed a forward config | abort | **PASS** |
| Pilot ceiling fed the ambient `.env` | abort | **PASS** — "MAX_SYMBOLS must be <=1 (got '40')" |
| Forward config through all guards | pass | **PASS** |
| `.env.live` present | absent | **PASS** |
| Authorisation token present | absent | **PASS** |
| `.env.live` gitignored | yes | **PASS** |

The fourth row is the important one: the guard rejects the exact configuration
posture the audit flagged as one variable away from live.

## Launchers and boot persistence

All 8 shell components pass `bash -n`; the launchd plist template parses as a
valid plist. 9/9 PASS.

## Observability

| Test | Result |
|---|---|
| Watchdog detects engine down | **PASS** |
| Watchdog detects stale heartbeat | **PASS** (9 h stale detected) |
| Watchdog detects dead monitor (R3) | **PASS** (31.7 h stale detected) |
| Watchdog writes its own heartbeat | **PASS** |
| Alerting degrades visibly (rc=3, "DEGRADED") | **PASS** |
| Alerts persisted to `logs/alerts.log` | **PASS** |

## Engine frozen

| Test | Result |
|---|---|
| 0 runtime files changed since `cda8187` | **PASS** |
| Suite still 339 passed | **PASS** |

## Scenarios NOT tested — stated plainly

The mission listed reboot, power loss, network loss, DNS failure, exchange
timeout and disk-full testing. Honest status:

| Scenario | Status |
|---|---|
| **Reboot** | **NOT TESTED.** Requires an actual host restart. This is the single most important outstanding test — it is what R1 is about, and the launchd agent is unproven without it. |
| **Power loss** | NOT TESTED — requires physical access. |
| **Network / DNS loss** | **Evidence from RC1**, not re-tested: a real DNS failure on 2026-07-26 was absorbed by retry/backoff and the cycle completed. Machine-wide injection needs sudo. |
| **Exchange / API timeout** | Covered at the code seam by 6 tests in `tests/test_scan_loop_resilience.py`. |
| **Heartbeat loss** | **TESTED** — watchdog detects a 9 h stale heartbeat. |
| **Disk full** | Threshold implemented and asserted (2 GiB floor); not tested by filling the disk. |
| **Configuration mismatch** | **TESTED** — 4 guard-abort tests above. |
| **Recovery / shutdown / startup** | Process-level recovery proven in RC1 (91 s / 121 s detached). Host-level recovery unproven. |

## Conclusion

Everything built in RC2 was tested and works. The most consequential scenario —
**a real reboot proving boot persistence** — has not been performed, so R1 is
mitigated in design but not yet verified in fact.
