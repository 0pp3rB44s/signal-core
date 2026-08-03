# 06 — Rollback Guide

Two independent axes: **mode rollback** (stop trading behaviour) and **release
rollback** (revert code). Mode rollback is always safe and always permitted. It is the
faster of the two and should be done first.

## Priority order

1. **Stop new entries** (mode rollback) — seconds.
2. **Deal with open exposure** — see [09_EMERGENCY_PROCEDURES.md](09_EMERGENCY_PROCEDURES.md).
3. **Revert code** only once exposure is understood.

## Mode rollback — live → dry run → strict paper

Downward transitions require no authorisation and no rebuild.

```bash
# 1. stop the supervisor so it cannot restart the bot underneath you
touch state/forward_paper_keepalive.stop
touch validation_72h/monitor.stop

# 2. stop the bot
kill "$(cat state/bot.pid)"

# 3. confirm nothing is running
pgrep -f 'app\.main'; pgrep -f forward_paper_keepalive; pgrep -f caffeinate
```

Then restart in the safe mode:

```bash
bash scripts/start_forward_paper.sh 60     # strict paper: orders impossible
```

**Stopping the process does not cancel exchange orders or close positions.** Resting
protective orders remain on the exchange. Verify position and order state manually in
the Bitget UI.

## Release rollback — revert to a previous tag

```bash
touch state/forward_paper_keepalive.stop && kill "$(cat state/bot.pid)"
git fetch --all --tags
git checkout rc1-forward-paper-validated
git status --porcelain            # MUST be empty
.venv/bin/python -m pytest tests/ -q
bash scripts/start_forward_paper.sh 60
```

**The clean-tree requirement is the most common rollback failure.** The strict launcher
refuses to start on a dirty working tree, and the supervisor will then fail every
restart silently. This exact failure kept the bot down during pre-flight (repair PR-1).
Always confirm `git status --porcelain` is empty before and after.

## State compatibility

| Store | On rollback |
|---|---|
| `data_store/forward_paper_events.jsonl` | append-only and hash-chained; older code reads it if the schema version is supported. **Never edit or truncate** — any edit breaks the chain and is detected. |
| `data_store/forward_paper_outcomes.csv` | derived; safe to regenerate via `scripts/rebuild_forward_paper_outcomes.py` |
| `state/*.json` | runtime state; stale PID files should be removed |
| Exchange positions | **not** rolled back by anything in this repo |

Schema version is `2`; the store also accepts legacy `1`.

## Verification after rollback

- [ ] `git describe --tags` shows the intended tag; tree clean
- [ ] 339 tests pass
- [ ] `bash scripts/check_forward_paper.sh` → `status=HEALTHY`
- [ ] exactly one bot process; heartbeat advancing with `snapshot_count > 0`
- [ ] event chain valid (`read_events()` succeeds)
- [ ] exchange shows no unexpected open position or resting order

## Rollback rehearsal — not yet performed

A release rollback has **never been rehearsed** on this system. It is a Stage 4
precondition in [05_DEPLOYMENT_GUIDE.md](05_DEPLOYMENT_GUIDE.md).
