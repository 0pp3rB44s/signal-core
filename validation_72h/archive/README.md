# Forward-Paper Validation — Permanent Evidence Archive (RC1)

Immutable forensic archive of the Forward-Paper validation campaign of 2026-07-25 →
2026-07-27. This directory is the authoritative record. Nothing in it may be
overwritten; corrections are added as new files.

## Bundle

| Item | Value |
|---|---|
| Bundle | `RC1_forward_paper_validation.tar.gz` |
| SHA-256 | `3b5bf71527184038eb58dc4772c582aa179e7d8747f7089f703eb4a7d5590739` |
| Compressed size | 912 KB (932 611 bytes) |
| Uncompressed size | 12 MB |
| Files | 63 (78 tar entries incl. directories) |
| Integrity | 63/63 verified; round-trip extraction re-verified 63/63 |

Verify at any time:

```bash
shasum -a 256 -c validation_72h/archive/RC1_forward_paper_validation.tar.gz.sha256
tar -xzf validation_72h/archive/RC1_forward_paper_validation.tar.gz -C /tmp
cd /tmp/RC1_forward_paper_validation && shasum -a 256 -c CHECKSUMS.sha256
```

## Contents

```
RC1_forward_paper_validation/
├── CHECKSUMS.sha256          SHA-256 of every file in the archive
├── manifest.json             run-2 validation manifest (identity, config, pids, counters)
├── reports/
│   ├── CLOSEOUT_REPORT.md    forensic close-out audit (14 phases, final verdict)
│   ├── PREFLIGHT_REPORT.md   pre-flight gates, baseline assembly, run-1 addendum
│   └── RUN1_INVALID.md       invalidation verdict for run 1
├── run2_evidence/            the audited run
│   ├── data_store/           forward_paper_events.jsonl, forward_paper_outcomes.csv
│   ├── state/                heartbeat, last_shutdown, runtime state, bot.pid,
│   │                         executed_trades, execution_events, position_events,
│   │                         live_trade_journal, symbol_cooldowns
│   ├── logs/                 forward_paper.out (27 965 lines), keepalive, monitor,
│   │                         supervisor, runtime
│   ├── reports/              forward_paper_data_quality.json
│   ├── snapshots/            21 hourly monitor snapshots
│   ├── live_state/           freeze verification, process state at audit
│   ├── git/                  commit, branch, status, log at audit
│   └── env/                  OS, arch, python, boot time, disk, directory sizes
├── run1_invalidated/         the invalidated run (granularity defect)
│   └── ...                   manifest, snapshots, logs, heartbeat, empty event store
└── harness/                  run_env.sh, supervise.sh, monitor.sh, monitor_loop.sh
```

## Run identity

| | Run 1 — INVALID | Run 2 — audited |
|---|---|---|
| Clock start | 2026-07-25T14:02:10Z | 2026-07-25T16:09:41Z |
| Ended | 2026-07-25T15:53Z (operator) | 2026-07-27T10:53:51Z (SIGTERM, host reboot) |
| Baseline commit | `221efa0` | `cda8187` |
| Verdict | INVALID — no usable evidence | FAIL vs 72 h criterion |
| Reason | `granularity=1h` rejected (HTTP 400 / 400171, 742×); all 106 scans failed while health reported HEALTHY | terminated at T+42.75 h by host reboot; 22.19 h lost to host sleep |

Run 1's `forward_paper_events.jsonl` has SHA-256 `e3b0c442…b7852b855` — the digest of
an empty file. That is cryptographic corroboration that run 1 wrote nothing.

## Not bundled

| Artefact | Reason |
|---|---|
| `data_store/funnel_events.jsonl` (74 MB) | repo-wide rolling telemetry, not run-scoped; strategy-accounting figures derived from it are recorded in the close-out report |
| CPU / memory time series | never sampled; process table lost at host reboot — **evidence not available** |
| OS sleep/wake records for the 22.19 h gap | unified log rotated at the 2026-07-27T10:54:07Z boot — **evidence not available** |

## Related

- `docs/FORWARD_PAPER_VALIDATION.md` — narrative history of the campaign
- `docs/RECOVERY_PROCEDURES.md`, `docs/MONITORING_GUIDE.md` — operational gaps found
- `docs/KNOWN_LIMITATIONS.md`, `docs/RISK_REGISTER.md` — carried risks
- `RELEASE_NOTES.md` — RC1 baseline definition
