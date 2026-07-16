#!/usr/bin/env bash
set -euo pipefail

repo="$(git rev-parse --show-toplevel)"
cd "$repo"
source "$repo/scripts/lib/platform_preflight.sh"
python_bin="${PYTHON_BIN:-${repo}/.venv/bin/python}"
[[ -x "$python_bin" ]] || python_bin=python3

branch="$(git branch --show-current)"
[[ -n "$branch" ]] || branch="DETACHED"
dirty="no"
[[ -z "$(git status --porcelain)" ]] || dirty="yes"

echo "repository=$repo"
echo "architecture=$(detect_architecture)"
echo "macos_version=$(sw_vers -productVersion 2>/dev/null || echo UNKNOWN)"
echo "branch=$branch"
echo "commit=$(git rev-parse HEAD)"
echo "dirty=$dirty"
echo "python=$($python_bin --version 2>&1)"
echo "python_path=$python_bin"
echo "dependency_lock_sha256=$(shasum -a 256 requirements.txt | awk '{print $1}')"
