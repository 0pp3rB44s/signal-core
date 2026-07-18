#!/usr/bin/env bash
set -euo pipefail

repo="$(git rev-parse --show-toplevel)"
cd "$repo"
[[ ! -e .env ]] || { echo "ERROR: .env already exists; refusing to overwrite or inspect it" >&2; exit 1; }
target=".env.runner.template"
[[ ! -e "$target" ]] || { echo "ERROR: $target already exists" >&2; exit 1; }

awk '
  /^[[:space:]]*#/ || /^[[:space:]]*$/ { print; next }
  /^[A-Za-z_][A-Za-z0-9_]*=/ {
    key=$0; sub(/=.*/, "", key)
    if (key=="CGC_RUNTIME_MODE") print key "=runner"
    else if (key=="CGC_STATE_ROOT") print key "=state"
    else if (key=="CGC_REPORTS_ROOT") print key "=reports"
    else if (key=="CGC_DATA_ROOT") print key "=data"
    else print key "="
  }
' .env.example > "$target"

echo "template=$target"
echo "Manually create .env on the Runner and transfer required Bitget values securely. Do not use GitHub, chat, logs, patches, or reports."
