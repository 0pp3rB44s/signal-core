#!/usr/bin/env bash
set -euo pipefail

repo="$(git rev-parse --show-toplevel)"
cd "$repo"

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  echo "ERROR: this bootstrap contract requires Apple Silicon macOS" >&2
  exit 1
fi

required="$(tr -d '[:space:]' < .python-version)"
python_bin="${PYTHON_BIN:-python3}"
actual="$($python_bin -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "$actual" != "$required" ]]; then
  echo "ERROR: Python $required required; found $actual" >&2
  exit 1
fi

[[ -d .venv ]] || "$python_bin" -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check --requirement requirements.txt
mkdir -p data_store logs reports state

.venv/bin/python -m compileall -q .
.venv/bin/python -m pytest -q
scripts/verify_checkout.sh
