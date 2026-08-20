#!/bin/bash
# Dedicated Dashboard V3 launchd entry. It has no LIVE executor dependency.
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$PROJECT_DIR"
exec bash scripts/start_dashboard.sh
