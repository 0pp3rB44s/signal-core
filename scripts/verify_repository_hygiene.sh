#!/usr/bin/env bash
set -euo pipefail

repo="$(git rev-parse --show-toplevel)"
cd "$repo"

usage() {
  echo "usage: $0 [--tracked | --base REF | --check-paths]" >&2
}

mode="tracked"
base_ref=""
if [[ "${1:-}" == "--base" ]]; then
  [[ $# -eq 2 ]] || { usage; exit 2; }
  mode="release"
  base_ref="$2"
elif [[ "${1:-}" == "--tracked" ]]; then
  [[ $# -eq 1 ]] || { usage; exit 2; }
elif [[ "${1:-}" == "--check-paths" ]]; then
  [[ $# -eq 1 ]] || { usage; exit 2; }
  mode="paths"
elif [[ $# -ne 0 ]]; then
  usage
  exit 2
elif [[ -n "${GITHUB_BASE_REF:-}" ]]; then
  mode="release"
  if git rev-parse --verify --quiet "origin/${GITHUB_BASE_REF}^{commit}" >/dev/null; then
    base_ref="origin/${GITHUB_BASE_REF}"
  else
    base_ref="${GITHUB_BASE_REF}"
  fi
fi

is_forbidden_path() {
  local path="$1"
  [[ "$path" =~ (^|/)\.env($|\.[^/]*) ]] \
    || [[ "$path" =~ (^|/)(credentials?|secrets?)(/|$) ]] \
    || [[ "$path" =~ \.(pem|key|p12|pfx|crt|cer)$ ]] \
    || [[ "$path" =~ (^|/)(state|runtime|pids)(/|$) ]] \
    || [[ "$path" =~ \.pid$ ]] \
    || [[ "$path" =~ (^|/)logs?(/|$) ]] \
    || [[ "$path" =~ (^|/)data_store(/|$) ]] \
    || [[ "$path" =~ (^|/)(datasets?|local_data)(/|$) ]] \
    || [[ "$path" =~ (^|/)data/.*\.(csv|tsv|json|jsonl|parquet|pkl|pickle|db|sqlite)$ ]] \
    || [[ "$path" =~ (^|/)(__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|\.cache|cache)(/|$) ]] \
    || [[ "$path" =~ \.pyc$ ]] \
    || [[ "$path" =~ (^|/)(\.idea|\.vscode)(/|$) ]] \
    || [[ "$path" =~ (^|/)\.DS_Store$ ]] \
    || [[ "$path" =~ (~|\.swp|\.tmp|\.temp|\.bak)$ ]] \
    || [[ "$path" =~ (^|/)[^/]*(credential|secret|token|api[-_]?key)[^/]*\.(json|yaml|yml|ini|conf|txt)$ ]]
}

paths=()
if [[ "$mode" == "paths" ]]; then
  while IFS= read -r path; do
    [[ -z "$path" ]] || paths+=("$path")
  done
elif [[ "$mode" == "release" ]]; then
  git rev-parse --verify --quiet "${base_ref}^{commit}" >/dev/null || {
    echo "ERROR: hygiene base reference cannot be resolved" >&2
    exit 2
  }
  while IFS= read -r path; do
    [[ -z "$path" ]] || paths+=("$path")
  done < <(
    {
      git diff --name-only --diff-filter=ACDMRT "${base_ref}...HEAD"
      git diff --cached --name-only --diff-filter=ACDMRT
      git diff --name-only --diff-filter=ACDMRT
      git ls-files --others --exclude-standard
    } | LC_ALL=C sort -u
  )
else
  while IFS= read -r path; do
    [[ -z "$path" ]] || paths+=("$path")
  done < <(git ls-files)
fi

forbidden_paths=()
forbidden_count=0
for path in "${paths[@]-}"; do
  [[ -z "$path" ]] && continue
  if is_forbidden_path "$path"; then
    forbidden_paths+=("$path")
    forbidden_count=$((forbidden_count + 1))
  fi
done
if (( forbidden_count > 0 )); then
  echo "ERROR: forbidden operational, secret-bearing, dataset, cache, or editor paths detected" >&2
  printf '%s\n' "${forbidden_paths[@]}" >&2
  exit 1
fi

# --check-paths is a hermetic classifier mode and deliberately never reads files.
if [[ "$mode" == "paths" ]]; then
  echo "repository_hygiene=PASS"
  exit 0
fi

secret_pattern='(AKIA[0-9A-Z]{16}|-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9_]{30,}|github_pat_[A-Za-z0-9_]{40,}|xox[baprs]-[A-Za-z0-9-]{10,})'
secret_matches=()
secret_match_count=0
large_files=()
large_file_count=0
symlink_paths=()
symlink_count=0
for path in "${paths[@]-}"; do
  [[ -z "$path" ]] && continue
  if [[ -L "$path" ]]; then
    symlink_paths+=("$path")
    symlink_count=$((symlink_count + 1))
    continue
  fi
  [[ -f "$path" ]] || continue
  if LC_ALL=C grep -Iq -- . "$path" && LC_ALL=C grep -Eq -- "$secret_pattern" "$path"; then
    secret_matches+=("$path")
    secret_match_count=$((secret_match_count + 1))
  fi
  size="$(wc -c < "$path")"
  if (( size > 50000000 )); then
    large_files+=("$path")
    large_file_count=$((large_file_count + 1))
  fi
done

if (( symlink_count > 0 )); then
  echo "ERROR: symbolic links are not permitted in the release patch" >&2
  printf '%s\n' "${symlink_paths[@]}" >&2
  exit 1
fi
if (( secret_match_count > 0 )); then
  echo "ERROR: source matches a high-confidence secret pattern" >&2
  printf '%s\n' "${secret_matches[@]}" >&2
  exit 1
fi
if (( large_file_count > 0 )); then
  echo "ERROR: files exceed 50 MB" >&2
  printf '%s\n' "${large_files[@]}" >&2
  exit 1
fi

echo "repository_hygiene=PASS"
