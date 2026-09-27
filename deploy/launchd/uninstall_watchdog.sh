#!/bin/bash
set -uo pipefail
LABEL="com.cgc.watchdog"
launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "removed $LABEL"
