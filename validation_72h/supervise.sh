#!/bin/bash
# Entry point for the 72-hour validation. Holds the run configuration and hands
# supervision to scripts/forward_paper_keepalive.sh, which restarts only through
# the strict forward-paper launcher. Run under tmux:
#   tmux new -s cgc72h 'bash validation_72h/supervise.sh'
set -uo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"
source validation_72h/run_env.sh
exec bash scripts/forward_paper_keepalive.sh --loop 120
