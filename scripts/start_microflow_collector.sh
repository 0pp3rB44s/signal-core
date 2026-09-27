#!/bin/bash
# Public Bitget trade/books5 collector. No credentials are loaded; no orders can be placed.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"
PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
MICROFLOW_DATA_DIR="${MICROFLOW_DATA_DIR:-$PROJECT_DIR/data_store/microflow-v1}"
MICROFLOW_SYMBOLS="${MICROFLOW_SYMBOLS:-BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,BNBUSDT,LINKUSDT,AVAXUSDT,SUIUSDT,HYPEUSDT,ZECUSDT,NEARUSDT}"

mkdir -p "$MICROFLOW_DATA_DIR"
PID_FILE="$MICROFLOW_DATA_DIR/collector.pid"
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "microflow collector already running (pid $(cat "$PID_FILE"))"
  exit 1
fi
"$PYTHON_BIN" -c "import websocket" >/dev/null
COLLECTOR_PID="$("$PYTHON_BIN" scripts/launch_detached.py \
  --stdout "$MICROFLOW_DATA_DIR/collector.out" -- \
  "$PYTHON_BIN" -m microflow.collector \
  --data-dir "$MICROFLOW_DATA_DIR" --symbols "$MICROFLOW_SYMBOLS")"
sleep 2
kill -0 "$COLLECTOR_PID" 2>/dev/null || {
  echo "ERROR: collector exited; inspect $MICROFLOW_DATA_DIR/collector.out"
  exit 1
}
echo "microflow collector started | pid=$COLLECTOR_PID | read_only=true | orders_allowed=false"
