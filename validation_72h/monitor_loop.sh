#!/bin/bash
# Hourly snapshot loop. Stop with: touch validation_72h/monitor.stop
set -uo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"
while [ ! -f validation_72h/monitor.stop ]; do
  bash validation_72h/monitor.sh >> logs/validation_monitor.log 2>&1
  sleep 3600
done
