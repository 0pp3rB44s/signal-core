#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

mkdir -p logs state

stop_pid_file() {
  local name="$1"
  local pid_file="$2"

  if [ -f "$pid_file" ]; then
    local pid
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    if [ -n "$pid" ] && ps -p "$pid" > /dev/null 2>&1; then
      kill "$pid" 2>/dev/null || true
      sleep 1
      if ps -p "$pid" > /dev/null 2>&1; then
        kill -9 "$pid" 2>/dev/null || true
      fi
      echo "$name stopped (PID $pid)"
    else
      echo "$name pid file found, but process not running"
    fi
    rm -f "$pid_file"
  else
    echo "$name not running"
  fi
}

STOP_REASON="${1:-manual_stop}"

stop_pid_file "bot" "state/bot.pid"
stop_pid_file "dashboard" "state/dashboard.pid"

# Safety cleanup for orphan project processes.
pkill -f "python3 -u -m app.main" >/dev/null 2>&1 || true
pkill -f "python3 -m app.main" >/dev/null 2>&1 || true
pkill -f "python3 -u -m dashboard_v2.app" >/dev/null 2>&1 || true
pkill -f "python3 -m dashboard_v2.app" >/dev/null 2>&1 || true

STOP_TS="$(date '+%Y-%m-%d %H:%M:%S')"
echo "$STOP_TS | ALL_PROCESSES_STOPPED | reason=$STOP_REASON" >> logs/runtime.log

echo "all project processes stopped"