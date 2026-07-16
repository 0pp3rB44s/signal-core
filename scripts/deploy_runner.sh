#!/usr/bin/env bash
set -euo pipefail

usage() { echo "usage: $0 <annotated-runner-tag-or-full-main-commit> | --rollback <backup-ref>" >&2; exit 2; }
[[ $# -ge 1 ]] || usage

repo="$(git rev-parse --show-toplevel)"
cd "$repo"
[[ -z "$(git status --porcelain)" ]] || { echo "ERROR: dirty runner checkout" >&2; exit 1; }

git fetch origin --prune --tags
target="$1"
rollback="no"
if [[ "$target" == "--rollback" ]]; then
  [[ $# -eq 2 ]] || usage
  target="$2"
  rollback="yes"
  [[ "$target" == refs/runner-backups/* ]] || { echo "ERROR: rollback requires refs/runner-backups/..." >&2; exit 1; }
else
  [[ $# -eq 1 ]] || usage
fi

if [[ "$target" =~ ^runner-v[0-9]{4}\.[0-9]{2}\.[0-9]{2}\.[0-9]+$ ]]; then
  [[ "$(git cat-file -t "refs/tags/$target" 2>/dev/null || true)" == "tag" ]] || { echo "ERROR: deployment tag must be annotated" >&2; exit 1; }
  commit="$(git rev-list -n1 "$target")"
elif [[ "$target" =~ ^[0-9a-f]{40}$ ]]; then
  commit="$target"
  git cat-file -e "$commit^{commit}" 2>/dev/null || { echo "ERROR: unknown commit" >&2; exit 1; }
  git merge-base --is-ancestor "$commit" origin/main || { echo "ERROR: commit is not reachable from origin/main" >&2; exit 1; }
elif [[ "$target" == refs/runner-backups/* ]]; then
  commit="$(git rev-parse "$target^{commit}")"
else
  usage
fi

if [[ "$rollback" == "no" ]]; then
  git merge-base --is-ancestor "$commit" origin/main || { echo "ERROR: target is not reachable from origin/main" >&2; exit 1; }
fi

required="$(git show "$commit:.python-version" 2>/dev/null | tr -d '[:space:]')"
[[ -n "$required" ]] || { echo "ERROR: target has no Python version contract" >&2; exit 1; }
python_bin="${PYTHON_BIN:-python3}"
actual="$($python_bin -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
[[ "$actual" == "$required" ]] || { echo "ERROR: Python $required required; found $actual" >&2; exit 1; }
git cat-file -e "$commit:requirements.txt" 2>/dev/null || { echo "ERROR: target has no dependency lock" >&2; exit 1; }

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
previous="$(git rev-parse HEAD)"
backup="refs/runner-backups/$timestamp"
git update-ref "$backup" "$previous"

echo "preflight=PASS target=$commit backup=$backup"
git checkout --detach "$commit"
[[ -d .venv ]] || "$python_bin" -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check --requirement requirements.txt
.venv/bin/python -m compileall -q .
.venv/bin/python -m pytest -q tests/test_config_security.py tests/test_runtime_diagnostics.py

mkdir -p state
printf '%s\n' "$commit" > state/deployed_commit.txt.tmp
mv state/deployed_commit.txt.tmp state/deployed_commit.txt

echo "deployed_commit=$commit"
echo "rollback_command=$0 --rollback $backup"
echo "live_execution_started=no"
