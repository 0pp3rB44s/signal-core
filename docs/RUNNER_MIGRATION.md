# GitHub and Runner migration contract

## Identity and roles

The canonical repository is `0pp3rB44s/signal-core`; `main` is the approved
branch. The Work MacBook M4 owns development, research, tests, PRs and releases.
The Runner executes only an explicit approved commit reachable from
`origin/main`, preferably an annotated `runner-vYYYY.MM.DD.N` tag. A newer Work
Mac research checkout is not runner drift.

Runner audit baseline: macOS 14.8.7, Intel `x86_64`, system Python 3.9.6,
existing virtualenv Python 3.11.15, clean `main` at
`6f1b93edc8d3df443bef9a4bc7e633bf43a3ec17`, no source drift, completed local
backups, no active bot/monitor/launch service, and no local `.env`. No Runner
checkout or process was changed during this infrastructure fix.

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

Both Apple Silicon (`arm64`, Homebrew in `/opt/homebrew`) and Intel (`x86_64`,
Homebrew in `/usr/local`) macOS are supported. Other architectures fail
preflight. Rosetta is never installed or assumed. `.python-version` selects one
shared Python version and `requirements.txt` is fully pinned.

```bash
scripts/bootstrap_mac.sh
scripts/bootstrap_mac.sh --recreate-venv  # preserves an incompatible .venv first
scripts/verify_checkout.sh
```

Bootstrap refuses Apple system Python 3.9 and selects the architecture-correct
Homebrew Python. An incompatible `.venv` is rejected unless
`--recreate-venv` is explicit; then it is moved to a timestamped backup before
replacement. It creates ignored folders, verifies hygiene, compiles and tests.
It does not source `.env`.

Python 3.12 is the shared contract. Locked-wheel resolution succeeds for both
macOS arm64 and x86_64. Python 3.13 is currently blocked by the locked
Streamlit/pyarrow dependency chain, which has no matching CPython 3.13 macOS
wheel in the resolver. Python 3.11 is compatible but is not selected because
3.12 is the highest common validated version.

| Python | Locked dependencies | Tests | arm64 | x86_64 | Verdict |
|---|---|---|---|---|---|
| 3.11 | wheels resolve | not executed in isolated environment | metadata/wheels | metadata/wheels | compatible, not selected |
| 3.12 | wheels resolve | 237 pass on M4 | executed | wheels + architecture tests; final Runner run pending | selected |
| 3.13 | pyarrow wheel unresolved | prior suite ran only in an already-provisioned environment | resolver fails | resolver fails | rejected |

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

Before deployment, run the non-mutating preflight:

```bash
scripts/deploy_runner.sh --preflight runner-vYYYY.MM.DD.N
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

Generate a names-only Runner template only while `.env` is absent:

```bash
scripts/create_runner_env_template.sh
```

The script blanks all values except the documented safe local-root suggestions.
Enter required Bitget values manually on the Runner or use macOS Keychain, an
encrypted password manager, or a user-initiated encrypted local transfer.
Secrets must never travel through GitHub, commits, patches, chat, logs or shared
reports. No script extracts secrets from the Work Mac.

After both local files exist, compare names/presence without values:

```bash
scripts/compare_env_presence.sh .env.example /path/to/work/.env /path/to/runner/.env
```

GitHub CLI is optional and is not used by Runner deployment. The Runner needs
only Git, Homebrew and the selected Python. PR administration remains on the
Work Mac.

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

## Intel Runner preparation after PR approval

These commands prepare and preflight; they do not deploy or start processes:

```bash
cd /path/to/signal-core
git fetch origin --prune --tags
git switch --detach origin/infra/github-runner-synchronization
/usr/local/bin/brew install python@3.12
scripts/bootstrap_mac.sh --recreate-venv
scripts/create_runner_env_template.sh
cp .env.runner.template .env
# Manually complete .env using a secure local method; never paste values into chat.
scripts/verify_checkout.sh
scripts/deploy_runner.sh --preflight <approved-full-main-sha-or-runner-tag>
```

If `/usr/local/bin/brew` is absent, stop and install Homebrew manually from its
official instructions first. The scripts do not install Homebrew or Rosetta.
