#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

echo "=== Runtime Cleanup ==="

ARCHIVE_ROOT="${CGC_ARCHIVE_ROOT:-$HOME/bitget_ai_agent_runtime_backups}"
mkdir -p "$ARCHIVE_ROOT/runtime_cleanup"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="$ARCHIVE_ROOT/runtime_cleanup/$TIMESTAMP"
mkdir -p "$BACKUP_DIR"

# backup critical runtime artifacts
for target in logs state reports/backtests; do
  if [ -d "$target" ]; then
    cp -R "$target" "$BACKUP_DIR/" 2>/dev/null || true
  fi
done

# cleanup python cache
find app backtesting clients dashboard_v2 data execution market_data planning risk scripts strategies telemetry tests -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
find app backtesting clients dashboard_v2 data execution market_data planning risk scripts strategies telemetry tests -type f \( -name "*.pyc" -o -name "*.pyo" -o -name "*.cpython-*" \) -delete 2>/dev/null || true

# cleanup temp + backup files
find app backtesting clients dashboard_v2 data execution market_data planning risk scripts strategies telemetry tests -type f \( -name "*.bak" -o -name "*.tmp" -o -name "*.orig" -o -name "*.swp" \) -delete 2>/dev/null || true

# recreate runtime directories
mkdir -p logs state reports/backtests

echo "runtime cleanup completed"
echo "backup stored in: $BACKUP_DIR"
