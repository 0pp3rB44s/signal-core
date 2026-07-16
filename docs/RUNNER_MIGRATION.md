# GitHub and Runner migration contract

## Identity and roles

The canonical repository is `0pp3rB44s/signal-core`; `main` is the approved
branch. The Work MacBook M4 owns development, research, tests, PRs and releases.
The Runner executes only an explicit approved commit reachable from
`origin/main`, preferably an annotated `runner-vYYYY.MM.DD.N` tag. A newer Work
Mac research checkout is not runner drift.

GitHub stores source, tests, pinned requirements, docs, small manifests and
hashes. It never transports `.env`, credentials, logs, operational reports,
state, positions, order history, freeze/kill-switch state, PID/socket files,
virtualenvs, caches, or downloaded candle payloads.

## Branch and PR model

- `main`: reviewed and tested production history; no experiments.
- `research/*`: research and rejected hypotheses; never deployed.
- `fix/*`: isolated correctness changes through review.
- `infra/*`: CI, deployment and repository-operability changes.

Develop in an isolated worktree, run all tests and
`scripts/verify_repository_hygiene.sh`, push without force, then open a draft
PR. Keep multi-phase research on archive branches; never merge it wholesale.

## Work Mac setup

Apple Silicon macOS and `.python-version` are required. `requirements.txt` is
fully pinned.

```bash
scripts/bootstrap_mac.sh
scripts/verify_checkout.sh
```

Bootstrap creates/reuses `.venv`, installs dependencies, creates ignored local
folders, compiles and tests. It does not source `.env`.

## Release, Runner audit and deployment

After review and green CI on `main`:

```bash
git tag -a runner-vYYYY.MM.DD.N <approved-main-sha> -m "Runner deployment YYYY-MM-DD N"
git push origin runner-vYYYY.MM.DD.N
```

Audit the Runner without stopping anything:

```bash
pwd
git rev-parse --show-toplevel
git remote -v
git status --short --branch
git branch --show-current
git rev-parse HEAD
git log -5 --oneline --decorate
git worktree list
pgrep -af 'python|bot|monitor' || true
launchctl list | grep -Ei 'cgc|bitget|bot|monitor' || true
```

If unique source drift exists, preserve it locally before proceeding. Review
backups locally for secrets and never upload sensitive bundles:

```bash
stamp=$(date -u +%Y%m%dT%H%M%SZ)
git branch "backup/runner-pre-migration-$stamp"
git diff > "../runner-pre-migration-$stamp.patch"
git bundle create "../runner-pre-migration-$stamp.bundle" --all
```

Only in a separately approved maintenance step:

```bash
scripts/deploy_runner.sh runner-vYYYY.MM.DD.N
```

The script requires a clean tree, fetches, verifies an annotated tag or full
main-reachable SHA, creates `refs/runner-backups/<UTC timestamp>`, validates
Python/dependencies before checkout, deploys detached, compiles, runs no-order
smoke tests and atomically records `state/deployed_commit.txt`. It never starts
trading. Roll back with the exact command it prints:

```bash
scripts/deploy_runner.sh --rollback refs/runner-backups/<UTC timestamp>
```

## Configuration and local state

Compare variable names/presence only, never values. `.env.example` is the safe
schema; local `.env` stays ignored. Explicit operational roots are:

- `CGC_STATE_ROOT` — state, deployment marker and locks;
- `CGC_REPORTS_ROOT` — local operational reports;
- `CGC_DATA_ROOT` — local data;
- `CGC_RUNTIME_MODE` — explicit machine/process role.

Tests use temporary roots, research uses explicit isolated roots, and Runner
values point to Runner-local folders. Git never synchronizes their contents.
Current production defaults are not rewired by this infrastructure change.

| Variable | Work present | Runner present | Required | Secret | Action |
|---|---|---|---|---|---|
| BITGET_API_KEY | presence only | presence only | yes | yes | keep local |
| BITGET_API_SECRET | presence only | presence only | yes | yes | keep local |
| BITGET_API_PASSPHRASE | presence only | presence only | yes | yes | keep local |
| CGC_STATE_ROOT | presence only | presence only | yes | no | set per machine |
| CGC_REPORTS_ROOT | presence only | presence only | yes | no | set per machine |
| CGC_DATA_ROOT | presence only | presence only | yes | no | set per machine |
| CGC_RUNTIME_MODE | presence only | presence only | yes | no | set explicit role |

## Data, recovery and verification

Large raw/canonical market data remains local; committed acquisition scripts,
manifests and hashes make it reproducible. Back up Runner `.env` and state with
encrypted local machine backups, never GitHub. Disaster recovery clones the
repository, restores local secrets/state, deploys an explicit tag, compares SHA
and lock hash, runs smoke tests, and waits for approval before starting trading.

Checklist:

- Remote is `git@github.com:0pp3rB44s/signal-core.git`.
- Tag is annotated and its commit is reachable from `origin/main`.
- Both Macs can fetch that exact commit.
- Runner deployment marker equals the tag SHA.
- Python and `requirements.txt` hash match.
- Checkout is clean; `.env` and operational state are untracked.
- CI and no-order smoke tests pass.
- Rollback resolves without starting trading.
