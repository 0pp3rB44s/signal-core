#!/bin/bash
# SUNDAY_SHADOW_DEPLOY_AND_START launcher for the dedicated shadow worktree.
#
# Adapted from scripts/start_forward_paper.sh: identical safety-critical
# behavior (forces FORWARD_PAPER_ONLY, blanks exchange credentials in the
# process environment, disables position management), but relaxes that
# script's `git branch --show-current == main` check, which assumes the
# single-worktree production deployment model -- this run is deliberately on
# a dedicated worktree/branch (sunday-shadow/deploy-2026-09-13) created for
# this validation, not production's own checkout.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

source "$PROJECT_DIR/scripts/lib/power_assertion.sh"

SCAN_INTERVAL="${1:-60}"
if ! [[ "$SCAN_INTERVAL" =~ ^[1-9][0-9]*$ ]]; then
  echo "ERROR: scan interval must be a positive integer in seconds"
  exit 2
fi

EXPECTED_DIR="/Users/bryonkolkman/cgc/sunday_shadow_runner"
if [ "$PROJECT_DIR" != "$EXPECTED_DIR" ]; then
  echo "ERROR: refusing to run outside the dedicated shadow worktree ($EXPECTED_DIR)"
  exit 3
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "ERROR: working tree is not clean"
  exit 4
fi

if pgrep -f "[Pp]ython(3)?.*(-m )?app\.main" >/dev/null 2>&1; then
  echo "ERROR: a bot process is already running; nothing was stopped"
  exit 5
fi

if pgrep -f "[Pp]ython(3)?.*(-m )?dashboard_v2\.app" >/dev/null 2>&1; then
  echo "ERROR: a dashboard process is already running; nothing was stopped"
  exit 6
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "ERROR: .venv/bin/python is unavailable"
  exit 7
fi

mkdir -p logs state data_store reports

export FORWARD_PAPER_ONLY=true
export FORWARD_PAPER_ENABLED=true
export FORWARD_PAPER_METRICS_CONTEXT_ENABLED=true
export EXECUTION_ENABLED=false
export EXECUTION_MODE=DRY_RUN
export POSITION_MANAGER_ENABLED=false
export POSITION_LOOP_ENABLED=false
export POSITION_SYNC_ON_START=false
export BITGET_API_KEY=""
export BITGET_API_SECRET=""
export BITGET_API_PASSPHRASE=""
export SCAN_ON_START=true
export SCAN_LOOP_ENABLED=true
export SCAN_INTERVAL_SEC="$SCAN_INTERVAL"

START_REASON="sunday_shadow_deploy_and_start"
STARTED_AT="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

echo "FORWARD_PAPER_ONLY ACTIVE"
echo "PRIVATE EXCHANGE CALLS DISABLED"
echo "FORWARD_PAPER_METRICS_CONTEXT_ENABLED=true"
BOT_PID="$(.venv/bin/python scripts/launch_detached.py \
  --stdout logs/forward_paper.out \
  -- .venv/bin/python -u -m app.main)"
echo "$BOT_PID" > state/bot.pid

{
  echo "mode=FORWARD_PAPER_ONLY"
  echo "pid=$BOT_PID"
  echo "started_at=$STARTED_AT"
  echo "scan_interval_sec=$SCAN_INTERVAL"
  echo "reason=$START_REASON"
  echo "worktree=$PROJECT_DIR"
  echo "branch=$(git branch --show-current)"
  echo "sha=$(git rev-parse HEAD)"
} > state/forward_paper_runtime.state

sleep 3
if ! ps -p "$BOT_PID" >/dev/null 2>&1; then
  echo "ERROR: shadow process failed to start"
  tail -50 logs/forward_paper.out || true
  exit 8
fi

hold_power_assertion "$BOT_PID" "sunday_shadow"

echo "$STARTED_AT | SUNDAY_SHADOW_START | reason=$START_REASON | interval=${SCAN_INTERVAL}s" >> logs/runtime.log
echo "Sunday shadow started (PID $BOT_PID, interval ${SCAN_INTERVAL}s)"
