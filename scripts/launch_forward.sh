#!/bin/bash
# FORWARD PAPER launcher. Pinned to .env.forward. Cannot start live execution:
# the guard asserts EXECUTION_ENABLED=false, EXECUTION_MODE=DRY_RUN and blank
# credentials before the engine is started, and this script never reads .env.live.
set -uo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"; cd "$PROJECT_DIR"
. scripts/lib/env_guard.sh
. scripts/lib/power_assertion.sh

SCAN_INTERVAL="${1:-60}"
[[ "$SCAN_INTERVAL" =~ ^[1-9][0-9]*$ ]] || guard_die "scan interval must be a positive integer"

echo "=== FORWARD PAPER LAUNCH ==="
guard_assert_repo_clean
guard_assert_venv
guard_assert_no_duplicate
guard_assert_disk
guard_assert_sleep_safe            # R2: surfaces battery/lid sleep exposure
guard_load_env ".env.forward"
guard_assert_forward_mode          # aborts if live execution is reachable
guard_assert_pilot_limits
export SCAN_INTERVAL_SEC="$SCAN_INTERVAL"

mkdir -p logs state data_store reports
echo "FORWARD_PAPER_ONLY ACTIVE"; echo "PRIVATE EXCHANGE CALLS DISABLED"
BOT_PID="$(.venv/bin/python scripts/launch_detached.py --stdout logs/forward_paper.out -- .venv/bin/python -u -m app.main)"
echo "$BOT_PID" > state/bot.pid
{ echo "mode=FORWARD_PAPER_ONLY"; echo "pid=$BOT_PID"; echo "started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)";
  echo "scan_interval_sec=$SCAN_INTERVAL"; echo "env_file=.env.forward";
  echo "commit=$(git rev-parse HEAD)"; } > state/forward_paper_runtime.state

sleep 2
ps -p "$BOT_PID" >/dev/null 2>&1 || guard_die "engine failed to start; see logs/forward_paper.out"
hold_power_assertion "$BOT_PID" "forward_paper"
guard_log_start "FORWARD_PAPER" ".env.forward"
echo "forward paper started (PID $BOT_PID, interval ${SCAN_INTERVAL}s)"
