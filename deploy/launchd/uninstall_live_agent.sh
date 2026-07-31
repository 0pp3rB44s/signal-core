#!/bin/bash
set -uo pipefail
LABEL="com.cgc.live"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
rm -f "$DEST"
echo "removed $LABEL"
