#!/bin/bash
# Start only the read-only dashboard. This script never starts/stops the LIVE engine.
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

PYTHON_BIN="${PYTHON_BIN:-$PROJECT_DIR/.venv/bin/python}"
PID_FILE="$PROJECT_DIR/state/dashboard.pid"
LOG_FILE="$PROJECT_DIR/logs/dashboard.out"
[ -x "$PYTHON_BIN" ] || { echo "ERROR: Python unavailable at $PYTHON_BIN"; exit 1; }
mkdir -p logs state

ENV_FILE="${ENV_FILE:-}"
if [ -z "$ENV_FILE" ]; then
  if [ -f .env.live ]; then ENV_FILE=.env.live; else ENV_FILE=.env; fi
fi
case "$ENV_FILE" in /*) ENV_PATH="$ENV_FILE" ;; *) ENV_PATH="$PROJECT_DIR/$ENV_FILE" ;; esac
[ -f "$ENV_PATH" ] || { echo "ERROR: environment file '$ENV_FILE' not found"; exit 1; }
set -a
# shellcheck disable=SC1090
. "$ENV_PATH"
set +a

# Owner-requested home-LAN operations view. Authentication remains mandatory;
# the app still refuses this bind unless this dedicated launcher opts in.
export DASHBOARD_HOST="${DASHBOARD_HOST:-0.0.0.0}"
export DASHBOARD_ALLOW_PUBLIC_BIND="${DASHBOARD_ALLOW_PUBLIC_BIND:-true}"

DASH_PORT="${DASHBOARD_PORT:-8501}"
if [ -f "$PID_FILE" ]; then
  EXISTING_PID="$(head -n 1 "$PID_FILE" 2>/dev/null || true)"
  if [ -n "$EXISTING_PID" ] && kill -0 "$EXISTING_PID" 2>/dev/null; then
    echo "dashboard already running (PID $EXISTING_PID, port $DASH_PORT)"
    exit 0
  fi
fi
if lsof -nP -iTCP:"$DASH_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "ERROR: port $DASH_PORT already has a listener; refusing to kill it"
  exit 1
fi

DASH_PID="$("$PYTHON_BIN" scripts/launch_detached.py --stdout "$LOG_FILE" -- \
  "$PYTHON_BIN" -u -m dashboard_v3.app)"
printf '%s\n' "$DASH_PID" > "$PID_FILE"

READY=0
for _ in $(seq 1 30); do
  kill -0 "$DASH_PID" 2>/dev/null || break
  CODE="$(curl -s -L -o /dev/null -w '%{http_code}' -m 4 "http://127.0.0.1:${DASH_PORT}/" 2>/dev/null || true)"
  if [ "$CODE" = "200" ]; then READY=1; break; fi
  sleep 1
done
[ "$READY" = "1" ] || { echo "ERROR: dashboard failed; inspect $LOG_FILE"; exit 1; }
printf '%s | DASHBOARD_START | pid=%s | port=%s | read_only=true\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$DASH_PID" "$DASH_PORT" >> logs/runtime.log
echo "dashboard started (PID $DASH_PID, HTTP 200 local; configured bind $DASHBOARD_HOST:$DASH_PORT)"
