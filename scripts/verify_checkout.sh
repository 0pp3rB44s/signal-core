#!/usr/bin/env bash
set -euo pipefail

repo="$(git rev-parse --show-toplevel)"
cd "$repo"
python_bin="${PYTHON_BIN:-${repo}/.venv/bin/python}"
[[ -x "$python_bin" ]] || python_bin=python3

branch="$(git branch --show-current)"
[[ -n "$branch" ]] || branch="DETACHED"
dirty="no"
[[ -z "$(git status --porcelain)" ]] || dirty="yes"

echo "repository=$repo"
echo "branch=$branch"
echo "commit=$(git rev-parse HEAD)"
echo "dirty=$dirty"
echo "python=$($python_bin --version 2>&1)"
echo "dependency_lock_sha256=$(shasum -a 256 requirements.txt | awk '{print $1}')"
