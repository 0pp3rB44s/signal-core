#!/bin/bash
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"; cd "$PROJECT_DIR"
PID_FILE="state/dashboard.pid"; DASH_PORT="${DASHBOARD_PORT:-8501}"
if [ ! -f "$PID_FILE" ]; then echo "dashboard status: STOPPED (no pid file)"; exit 1; fi
PID="$(head -n 1 "$PID_FILE" 2>/dev/null || true)"
if [ -z "$PID" ] || ! kill -0 "$PID" 2>/dev/null; then echo "dashboard status: STOPPED (stale pid=${PID:-UNKNOWN})"; exit 1; fi
CODE="$(curl -s -L -o /dev/null -w '%{http_code}' -m 4 "http://127.0.0.1:${DASH_PORT}/" 2>/dev/null || true)"
METRICS="$(ps -o pcpu=,rss=,etime= -p "$PID" | awk '{$1=$1};1')"
echo "dashboard status: RUNNING pid=$PID port=$DASH_PORT http=$CODE cpu_rss_uptime='$METRICS'"
[ "$CODE" = "200" ]
