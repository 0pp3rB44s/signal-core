#!/bin/bash
# Remove the hourly log-rotation schedule. Archives and MANIFEST.jsonl are left exactly where they are.
set -uo pipefail
LABEL="com.cgc.logrotate"
launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "removed $LABEL (archives untouched)"
