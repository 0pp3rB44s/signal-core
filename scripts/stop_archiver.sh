#!/bin/bash
# Stopt de microstructuur-archiver netjes (SIGTERM -> graceful shutdown).
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ARCHIVE_DIR="${ARCHIVE_DIR:-$PROJECT_DIR/data/archive}"
PID_FILE="$ARCHIVE_DIR/archiver.pid"

if [ ! -f "$PID_FILE" ]; then
  echo "geen pid-bestand ($PID_FILE); archiver draait niet?"
  exit 0
fi

ARCH_PID="$(cat "$PID_FILE")"
if ! kill -0 "$ARCH_PID" 2>/dev/null; then
  echo "proces $ARCH_PID draait niet meer; pid-bestand opruimen"
  rm -f "$PID_FILE"
  exit 0
fi

kill -TERM "$ARCH_PID"
for _ in $(seq 1 20); do
  if ! kill -0 "$ARCH_PID" 2>/dev/null; then
    echo "archiver gestopt (pid $ARCH_PID)"
    rm -f "$PID_FILE"
    exit 0
  fi
  sleep 1
done
echo "WAARSCHUWING: archiver reageert niet op SIGTERM (pid $ARCH_PID); niet ge-kill -9"
exit 1
