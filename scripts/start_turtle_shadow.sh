#!/bin/bash
# TURTLE_SHADOW launcher -- turtle_long_strong_breakout_v1 prospective epoch.
#
# NOT a live deployment. No order-transport code path exists anywhere in
# turtle_shadow/ -- this process only reads public Binance candle data and
# read-only Bitget contract metadata, and writes to two local JSONL ledgers.
# Mirrors scripts/start_sunday_shadow.sh's safety posture (blanked
# credentials, DRY_RUN, dedicated-worktree check) even though this process
# has no order-transport capability to disable in the first place --
# defense in depth, not a load-bearing guarantee here.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

SCAN_INTERVAL="${1:-1800}"
if ! [[ "$SCAN_INTERVAL" =~ ^[1-9][0-9]*$ ]]; then
  echo "ERROR: scan interval must be a positive integer in seconds"
  exit 2
fi

EXPECTED_DIR="/Users/bryonkolkman/cgc/turtle-long-strong-breakout-v1"
if [ "$PROJECT_DIR" != "$EXPECTED_DIR" ]; then
  echo "ERROR: refusing to run outside the dedicated Turtle shadow worktree ($EXPECTED_DIR)"
  exit 3
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "ERROR: working tree is not clean"
  exit 4
fi

if pgrep -f "[Tt]urtle_shadow.runner_loop" >/dev/null 2>&1; then
  echo "ERROR: a turtle shadow process is already running; nothing was stopped"
  exit 5
fi

PYTHON_BIN="/Users/bryonkolkman/cgc/sunday_shadow_runner/.venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
  echo "ERROR: shared venv python is unavailable at $PYTHON_BIN"
  exit 6
fi

mkdir -p logs state/turtle_shadow

export EXECUTION_ENABLED=false
export EXECUTION_MODE=DRY_RUN
export POSITION_MANAGER_ENABLED=false
export BITGET_API_KEY=""
export BITGET_API_SECRET=""
export BITGET_API_PASSPHRASE=""

nohup "$PYTHON_BIN" -m turtle_shadow.runner_loop "$SCAN_INTERVAL" \
  >> logs/turtle_shadow.log 2>&1 &
PID=$!
echo "$PID" > state/turtle_shadow/turtle_shadow.pid
disown

echo "TURTLE_SHADOW_STARTED pid=$PID interval=${SCAN_INTERVAL}s log=logs/turtle_shadow.log"
