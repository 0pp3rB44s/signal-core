#!/usr/bin/env bash
set -euo pipefail

repo="$(git rev-parse --show-toplevel)"
cd "$repo"

forbidden='(^|/)(\.env($|\.)|logs|state|data_store|runtime|pids|secrets|credentials)(/|$)|\.(pem|key|pid)$'
tracked="$(git ls-files | grep -E "$forbidden" || true)"
if [[ -n "$tracked" ]]; then
  echo "ERROR: forbidden operational or secret-bearing paths are tracked" >&2
  printf '%s\n' "$tracked" >&2
  exit 1
fi

if git grep -Il -E '(AKIA[0-9A-Z]{16}|-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9_]{30,}|github_pat_[A-Za-z0-9_]{40,}|xox[baprs]-[A-Za-z0-9-]{10,})' -- . ':(exclude).env.example' | grep -q .; then
  echo "ERROR: tracked source matches a high-confidence secret pattern" >&2
  exit 1
fi

large="$(git ls-files -z | xargs -0 -I{} sh -c 'test ! -f "$1" || test "$(wc -c < "$1")" -le 50000000 || printf "%s\n" "$1"' sh {} )"
if [[ -n "$large" ]]; then
  echo "ERROR: tracked files exceed 50 MB" >&2
  printf '%s\n' "$large" >&2
  exit 1
fi

echo "repository_hygiene=PASS"
