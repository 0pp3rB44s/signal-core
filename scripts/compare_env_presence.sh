#!/usr/bin/env bash
set -euo pipefail

[[ $# -eq 3 ]] || { echo "usage: $0 <env-example> <work-env> <runner-env>" >&2; exit 2; }
example="$1"; work="$2"; runner="$3"
for file in "$example" "$work" "$runner"; do [[ -f "$file" ]] || { echo "ERROR: missing input file" >&2; exit 1; }; done

names() { awk -F= '/^[A-Za-z_][A-Za-z0-9_]*=/{print $1}' "$1" | sort -u; }
printf 'variable\trequired\twork\trunner\n'
while IFS= read -r key; do
  work_status=missing; runner_status=missing
  names "$work" | grep -Fxq "$key" && work_status=present
  names "$runner" | grep -Fxq "$key" && runner_status=present
  printf '%s\tyes\t%s\t%s\n' "$key" "$work_status" "$runner_status"
done < <(names "$example")
