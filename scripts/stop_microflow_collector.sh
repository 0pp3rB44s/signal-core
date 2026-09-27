#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MICROFLOW_DATA_DIR="${MICROFLOW_DATA_DIR:-$PROJECT_DIR/data_store/microflow-v1}"
PID_FILE="$MICROFLOW_DATA_DIR/collector.pid"
if [ ! -f "$PID_FILE" ]; then
  echo "microflow collector not running"
  exit 0
fi
COLLECTOR_PID="$(cat "$PID_FILE")"
if ! kill -0 "$COLLECTOR_PID" 2>/dev/null; then
  rm -f "$PID_FILE"
  echo "stale collector pid removed"
  exit 0
fi
kill -TERM "$COLLECTOR_PID"
for _ in $(seq 1 20); do
  kill -0 "$COLLECTOR_PID" 2>/dev/null || { echo "microflow collector stopped"; exit 0; }
  sleep 1
done
echo "ERROR: collector did not stop cleanly" >&2
exit 1
