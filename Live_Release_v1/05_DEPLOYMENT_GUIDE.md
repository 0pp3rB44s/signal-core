# 05 — Deployment Guide

> **This guide does not authorise live trading.** Stage 4 is blocked by unmet
> preconditions listed in [PRODUCTION_READINESS_ASSESSMENT.md](PRODUCTION_READINESS_ASSESSMENT.md).
> Stages 0–2 are performable today.

## Promotion path

```
Stage 0  Preconditions      → Stage 1  Deploy artefact
Stage 2  Strict forward paper (current)
Stage 3  Dry run with live market + private read      [BLOCKED]
Stage 4  Live, minimum size                            [BLOCKED — not approved]
```

## Stage 0 — preconditions

- [ ] `git status --porcelain` empty. **A dirty tree silently breaks every automatic
      restart** — the strict launcher refuses to start on one.
- [ ] On `main`, at a tagged commit; record the SHA.
- [ ] `python -m pytest tests/ -q` → 339 passed, run twice.
- [ ] `.venv` present; `python --version` matches the recorded baseline (3.11.15).
- [ ] Disk headroom checked (`logs/` and `reports/` were ~1.1 G each at RC1).
- [ ] Public connectivity verified.
- [ ] No bot, supervisor or archiver already running.
- [ ] Stale `state/bot.pid` and `state/supervisor.stop` removed.

## Stage 1 — deploy the artefact

```bash
git fetch --all --tags
git checkout <tag>            # e.g. rc1-forward-paper-validated
git status --porcelain        # must be empty
.venv/bin/python -m pytest tests/ -q
```

Record: tag, SHA, config hash, python version, host, timestamp.

## Stage 2 — strict forward paper (safe, current mode)

```bash
bash scripts/start_forward_paper.sh 60
```

The launcher enforces: `main` branch, clean tree, no duplicate bot or dashboard,
`FORWARD_PAPER_ONLY=true`, `EXECUTION_ENABLED=false`, `EXECUTION_MODE=DRY_RUN`,
position manager off, **credentials blanked**, and a power assertion bound to the PID.

Supervised:

```bash
tmux new -s cgcbot 'bash validation_72h/supervise.sh'
```

Verify: `bash scripts/check_forward_paper.sh` → `status=HEALTHY`.

## Stage 3 — dry run with private read *(BLOCKED)*

Blocked until R1 (boot-persistent supervision) and R2 (sleep prevention) are closed and
a full-duration reliability run has passed.

## Stage 4 — live *(BLOCKED — NOT APPROVED)*

All of the following must be true and evidenced. **None is satisfied today.**

- [ ] Full-duration unattended reliability run passed
- [ ] Boot-persistent supervision verified by a real reboot (R1)
- [ ] Sleep prevention verified with independent assertion liveness (R2)
- [ ] Monitor self-liveness and off-host alerting (R3, R4)
- [ ] Live order path exercised at minimum size in a controlled window (R5)
- [ ] Risk-gate blocking exercised at least once (R7)
- [ ] Exchange preparation complete ([04](04_CONFIGURATION_GUIDE.md))
- [ ] Rollback rehearsed ([06](06_ROLLBACK_GUIDE.md))
- [ ] **Explicit written owner authorisation**

Only then: `EXECUTION_ENABLED=true`, `EXECUTION_MODE=LIVE`, smallest permitted size,
one symbol, one position, with an operator present.

## Post-deployment verification

- `status=HEALTHY`, heartbeat advancing, `snapshot_count > 0`
- exactly one bot, one supervisor, one power assertion
- 0 `SCAN_CYCLE_FAILED`, 0 `FORWARD_PAPER_FAILED_CLOSED`, 0 HTTP errors
- event chain valid; tree still clean
