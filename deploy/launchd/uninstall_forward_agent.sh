#!/bin/bash
# Fully reverse install_forward_agent.sh. Leaves no residue.
set -uo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$PROJECT_DIR"
LABEL="com.cgc.forward"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl unload "$DEST" 2>/dev/null && echo "unloaded $LABEL" || echo "$LABEL was not loaded"
rm -f "$DEST" && echo "removed $DEST"
printf '%s | LAUNCHD_UNINSTALL | label=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$LABEL" >> logs/runtime.log
launchctl list | grep -q "$LABEL" && echo "WARNING: still listed" || echo "verified: not loaded"
