#!/usr/bin/env bash
set -euo pipefail

repo="$(git rev-parse --show-toplevel)"
cd "$repo"
source "$repo/scripts/lib/platform_preflight.sh"

recreate="no"
[[ $# -le 1 ]] || { echo "usage: $0 [--recreate-venv]" >&2; exit 2; }
if [[ $# -eq 1 ]]; then
  [[ "$1" == "--recreate-venv" ]] || { echo "usage: $0 [--recreate-venv]" >&2; exit 2; }
  recreate="yes"
fi

require_macos_platform
arch="$(detect_architecture)"
brew="$(require_homebrew)"
required="$(tr -d '[:space:]' < .python-version)"
python_bin="$(find_compatible_python "$required")"

if [[ -x .venv/bin/python ]]; then
  venv_version="$(.venv/bin/python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  if [[ "$venv_version" != "$required" ]]; then
    [[ "$recreate" == "yes" ]] || {
      echo "ERROR: existing .venv uses Python $venv_version; rerun with --recreate-venv to preserve and replace it" >&2
      exit 1
    }
    backup=".venv.backup.$(date -u +%Y%m%dT%H%M%SZ)"
    mv .venv "$backup"
    echo "venv_backup=$backup"
  fi
fi

[[ -d .venv ]] || "$python_bin" -m venv .venv
.venv/bin/python -c "import sys; assert f'{sys.version_info.major}.{sys.version_info.minor}' == '$required'"
.venv/bin/python -m pip install --disable-pip-version-check --requirement requirements.txt
mkdir -p data_store logs reports state

echo "architecture=$arch"
echo "macos_version=$(sw_vers -productVersion)"
echo "homebrew=$brew"
echo "python_path=$(cd "$(dirname "$python_bin")" && pwd)/$(basename "$python_bin")"
.venv/bin/python -m compileall -q .
scripts/verify_repository_hygiene.sh
.venv/bin/python -m pytest -q
scripts/verify_checkout.sh
