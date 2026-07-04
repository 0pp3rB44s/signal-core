#!/bin/bash
# Removes the launchd watchdog installed by scripts/install_launchd.sh.
# Use this (not just scripts/stop_all.sh) to actually stop the bot for
# good, since the watchdog will otherwise notice it's gone and restart
# it again within ~5 minutes.
set -euo pipefail

LABEL="com.cgc.tradingbot"
PLIST_DEST="$HOME/Library/LaunchAgents/${LABEL}.plist"

launchctl bootout "gui/$(id -u)/${LABEL}" >/dev/null 2>&1 || true
rm -f "$PLIST_DEST"

echo "launchd job removed: ${LABEL}"
echo "the bot will no longer auto-restart. Run scripts/stop_all.sh to stop the running process."
