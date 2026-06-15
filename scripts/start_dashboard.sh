#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

if [ ! -d ".venv" ]; then
  echo "ERROR: .venv not found. Run: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
  exit 1
fi

source .venv/bin/activate

mkdir -p logs state

# cleanup stale pid
if [ -f state/dashboard.pid ]; then
  OLD_PID="$(cat state/dashboard.pid 2>/dev/null || true)"
  if [ -n "$OLD_PID" ] && ! ps -p "$OLD_PID" >/dev/null 2>&1; then
    rm -f state/dashboard.pid
    echo "removed stale dashboard.pid"
  fi
fi

# Stop only project dashboard processes.
pkill -f "python3 -m dashboard_v2.app" >/dev/null 2>&1 || true
pkill -f "dashboard_v2.app" >/dev/null 2>&1 || true
pkill -f "dashboard_v2/app.py" >/dev/null 2>&1 || true
pkill -f "app.dashboard" >/dev/null 2>&1 || true

sleep 1

nohup python3 -u -m dashboard_v2.app > logs/dashboard.out 2>&1 &

DASH_START_TS="$(date '+%Y-%m-%d %H:%M:%S')"
echo "$DASH_START_TS | DASHBOARD_START" >> logs/runtime.log

echo $! > state/dashboard.pid

sleep 1

if ps -p "$(cat state/dashboard.pid)" > /dev/null 2>&1; then
  echo "dashboard started successfully (PID $(cat state/dashboard.pid))"
else
  echo "dashboard failed to start"
  tail -50 logs/dashboard.out 2>/dev/null || true
  exit 1
fi
