#!/bin/bash
# Removes the launchd notification-only watchdog installed by
# scripts/install_launchd.sh. It never restarts the bot itself, so this
# is just about turning off the "bot is down" notifications -- run
# scripts/stop_all.sh separately to actually stop the running bot.
set -euo pipefail

LABEL="com.cgc.tradingbot"
PLIST_DEST="$HOME/Library/LaunchAgents/${LABEL}.plist"

launchctl bootout "gui/$(id -u)/${LABEL}" >/dev/null 2>&1 || true
rm -f "$PLIST_DEST"

echo "launchd notifier removed: ${LABEL}"
echo "you will no longer be notified if the bot stops. Run scripts/stop_all.sh to stop the running process."
