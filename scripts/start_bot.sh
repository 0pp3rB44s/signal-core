#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"


if [ ! -d ".venv" ]; then
  echo "ERROR: .venv not found. Run: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
  exit 1
fi

if [ ! -f ".env" ]; then
  echo "ERROR: .env not found. Copy .env.example to .env and fill in required values."
  exit 1
fi

source .venv/bin/activate

APP_ENV_VALUE="$(grep -E '^APP_ENV=' .env | tail -1 | cut -d '=' -f2- || echo 'unknown')"
APP_MODE_VALUE="$(grep -E '^APP_MODE=' .env | tail -1 | cut -d '=' -f2- || echo 'unknown')"
EXECUTION_MODE_VALUE="$(grep -E '^EXECUTION_MODE=' .env | tail -1 | cut -d '=' -f2- || echo 'unknown')"

printf '\n'
echo "=== CGC BOOT STATUS ==="
echo "environment: $APP_ENV_VALUE"
echo "mode: $APP_MODE_VALUE"
echo "execution: $EXECUTION_MODE_VALUE"
printf '\n'

# ensure folders exist
mkdir -p logs state

# cleanup stale pid
if [ -f state/bot.pid ]; then
  OLD_PID="$(cat state/bot.pid 2>/dev/null || true)"
  if [ -n "$OLD_PID" ] && ! ps -p "$OLD_PID" >/dev/null 2>&1; then
    rm -f state/bot.pid
    echo "removed stale bot.pid"
  fi
fi

# prevent duplicate bot processes
pkill -9 -f "app.main" >/dev/null 2>&1 || true
pkill -9 -f "python3.*app.main" >/dev/null 2>&1 || true
pkill -9 -f "Python.*app.main" >/dev/null 2>&1 || true

sleep 1

# start bot in background and store PID
START_REASON="${1:-manual_start}"
nohup python3 -u -m app.main > logs/bot.out 2>&1 &
BOT_START_TS="$(date '+%Y-%m-%d %H:%M:%S')"
echo "$BOT_START_TS | BOT_START | reason=$START_REASON | env=$APP_ENV_VALUE | mode=$APP_MODE_VALUE | execution=$EXECUTION_MODE_VALUE" >> logs/runtime.log
echo $! > state/bot.pid

sleep 1

if ps -p $(cat state/bot.pid) > /dev/null; then
    echo "bot started successfully (PID $(cat state/bot.pid))"
else
    echo "bot failed to start"
    exit 1
fi