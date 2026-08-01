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

# The dashboard builds the same Settings object as the engine, so it must read
# the same environment file. Without this it fell back to .env, which carries
# APP_ENV=production and EXECUTION_ENABLED=true but no PRODUCTION_SYMBOL_ALLOWLIST
# — so the LIVE invariant check raised and the process died a second after start,
# leaving the watchdog reporting "dashboard returned HTTP 000000".
# ENV_FILE can be overridden for forward-paper or development dashboards.
ENV_FILE="${ENV_FILE:-}"
if [ -z "$ENV_FILE" ]; then
  if [ -f ".env.live" ]; then ENV_FILE=".env.live"; else ENV_FILE=".env"; fi
fi
if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: environment file '$ENV_FILE' not found"
  exit 1
fi
set -a
# shellcheck disable=SC1090
. "./$ENV_FILE"
set +a
echo "dashboard environment: $ENV_FILE"

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
pkill -f "dashboard_v3.app" >/dev/null 2>&1 || true
pkill -f "app.dashboard" >/dev/null 2>&1 || true

sleep 1

nohup python3 -u -m dashboard_v3.app > logs/dashboard.out 2>&1 &

DASH_START_TS="$(date '+%Y-%m-%d %H:%M:%S')"
echo "$DASH_START_TS | DASHBOARD_START" >> logs/runtime.log

echo $! > state/dashboard.pid

DASH_PID="$(cat state/dashboard.pid)"
DASH_PORT="${DASHBOARD_PORT:-8501}"

# A one-second liveness check reported success for a process that then died on a
# config error. Wait for the port to actually answer before claiming it is up.
READY=0
for _ in $(seq 1 30); do
  if ! ps -p "$DASH_PID" > /dev/null 2>&1; then
    break
  fi
  # -L because / redirects to the login gate; the gate itself answers 200.
  CODE="$(curl -s -L -o /dev/null -w '%{http_code}' -m 4 "http://127.0.0.1:${DASH_PORT}/" 2>/dev/null || true)"
  if [ "$CODE" = "200" ]; then
    READY=1
    break
  fi
  sleep 1
done

if [ "$READY" = "1" ]; then
  echo "dashboard started successfully (PID $DASH_PID, HTTP 200 on 127.0.0.1:${DASH_PORT})"
else
  echo "dashboard failed to start (PID $DASH_PID)"
  tail -50 logs/dashboard.out 2>/dev/null || true
  rm -f state/dashboard.pid
  exit 1
fi
