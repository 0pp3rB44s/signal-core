#!/bin/bash
# Stops the dashboard PID only. It never signals an engine/supervisor/collector.
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"; cd "$PROJECT_DIR"
PID_FILE="state/dashboard.pid"
if [ ! -f "$PID_FILE" ]; then echo "dashboard already stopped"; exit 0; fi
PID="$(head -n 1 "$PID_FILE" 2>/dev/null || true)"
if [ -z "$PID" ] || ! kill -0 "$PID" 2>/dev/null; then
  mv "$PID_FILE" "${PID_FILE}.stale.$(date +%s)"
  echo "dashboard stopped (stale pid file archived)"; exit 0
fi
COMMAND="$(ps -o command= -p "$PID" 2>/dev/null || true)"
case "$COMMAND" in
  *dashboard_v3.app*) ;;
  *) echo "ERROR: PID $PID is not dashboard_v3.app; refusing to signal it"; exit 1 ;;
esac
kill -TERM "$PID"
for _ in $(seq 1 20); do kill -0 "$PID" 2>/dev/null || break; sleep 0.25; done
if kill -0 "$PID" 2>/dev/null; then echo "ERROR: dashboard PID $PID did not stop"; exit 1; fi
mv "$PID_FILE" "${PID_FILE}.stopped.$(date +%s)"
printf '%s | DASHBOARD_STOP | pid=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$PID" >> logs/runtime.log
echo "dashboard stopped (PID $PID); LIVE engine untouched"
