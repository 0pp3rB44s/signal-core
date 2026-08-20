#!/bin/bash
set -euo pipefail
LABEL=com.cgc.dashboard; DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
if [ -f "$DEST" ]; then mv "$DEST" "$DEST.removed.$(date +%s)"; fi
echo "removed $LABEL; LIVE service untouched"
