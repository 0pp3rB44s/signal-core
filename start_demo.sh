#!/bin/bash
# One-command owner launcher for the SIGNAL_CORE_FUNNEL_V1 DEMO/PAPER ceremony.
# This can NEVER place a real order - see funnel/tools/demo_ceremony.py and START_DEMO.md.
set -uo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"
exec python3 -m funnel.tools.demo_ceremony
