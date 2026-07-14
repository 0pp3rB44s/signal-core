#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

SCAN_INTERVAL="${1:-60}"
if ! [[ "$SCAN_INTERVAL" =~ ^[1-9][0-9]*$ ]]; then
  echo "ERROR: scan interval must be a positive integer in seconds"
  exit 2
fi

if [ "$(git branch --show-current)" != "main" ]; then
  echo "ERROR: strict forward-paper must be started from main"
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

START_REASON="strict_forward_paper_public_only"
STARTED_AT="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

echo "FORWARD_PAPER_ONLY ACTIVE"
echo "PRIVATE EXCHANGE CALLS DISABLED"
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
} > state/forward_paper_runtime.state

sleep 2
if ! ps -p "$BOT_PID" >/dev/null 2>&1; then
  echo "ERROR: strict forward-paper process failed to start"
  if [ -f state/last_shutdown.json ]; then
    .venv/bin/python - <<'PY'
import json
from pathlib import Path
payload = json.loads(Path("state/last_shutdown.json").read_text(encoding="utf-8"))
expected_pid = int(Path("state/bot.pid").read_text(encoding="utf-8").strip())
if payload.get("pid") == expected_pid:
    print("shutdown_reason=" + str(payload.get("reason") or "UNKNOWN"))
    print("exit_code=" + str(payload.get("exit_code")))
    print("signal=" + str(payload.get("signal")))
PY
  fi
  exit 8
fi

echo "$STARTED_AT | FORWARD_PAPER_START | reason=$START_REASON | interval=${SCAN_INTERVAL}s" >> logs/runtime.log
echo "strict forward-paper started (PID $BOT_PID, interval ${SCAN_INTERVAL}s)"
