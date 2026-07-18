#!/bin/bash
# Start de microstructuur-archiver (observe-only; plaatst nooit orders).
# Gebruikt uitsluitend publieke endpoints; vereist geen .env of geheimen.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
ARCHIVE_DIR="${ARCHIVE_DIR:-$PROJECT_DIR/data/archive}"
export ARCHIVE_DIR
# Eigen rate-limit-state zodat de archiver de bot-limiter niet beïnvloedt.
export BITGET_RATE_LIMIT_STATE_PATH="${BITGET_RATE_LIMIT_STATE_PATH:-$ARCHIVE_DIR/bitget_rate_limit.json}"

mkdir -p "$ARCHIVE_DIR"
PID_FILE="$ARCHIVE_DIR/archiver.pid"

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "archiver draait al (pid $(cat "$PID_FILE")); eerst scripts/stop_archiver.sh"
  exit 1
fi

"$PYTHON_BIN" -c "import websocket" 2>/dev/null || {
  echo "ERROR: websocket-client ontbreekt. Installeer: $PYTHON_BIN -m pip install -r requirements.txt"
  exit 1
}

nohup "$PYTHON_BIN" -m archiving.run_archiver >> "$ARCHIVE_DIR/archiver.out" 2>&1 &
ARCH_PID=$!
echo "$ARCH_PID" > "$PID_FILE"
sleep 2
if kill -0 "$ARCH_PID" 2>/dev/null; then
  echo "archiver gestart | pid=$ARCH_PID | dir=$ARCHIVE_DIR"
  echo "status:   cat $ARCHIVE_DIR/status.json"
  echo "stoppen:  scripts/stop_archiver.sh"
else
  echo "ERROR: archiver direct gestopt; zie $ARCHIVE_DIR/archiver.out"
  exit 1
fi
