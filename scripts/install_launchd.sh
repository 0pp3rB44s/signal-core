#!/bin/bash
# Installs a launchd job that keeps the trading bot running and restarts
# it automatically if the process crashes or exits unexpectedly. This is
# the recommended way to run the bot instead of scripts/start_bot.sh's
# manual nohup start, which does not survive a crash or reboot.
#
# This script installs and LOADS the job, which means launchd will start
# the bot (RunAtLoad) the moment this script runs. Only run this when you
# are ready for the bot to actually start trading.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

LABEL="com.cgc.tradingbot"
PLIST_DEST="$HOME/Library/LaunchAgents/${LABEL}.plist"

mkdir -p "$HOME/Library/LaunchAgents"

sed "s#__PROJECT_DIR__#${PROJECT_DIR}#g" "scripts/${LABEL}.plist.template" > "$PLIST_DEST"

echo "wrote $PLIST_DEST"

launchctl bootout "gui/$(id -u)/${LABEL}" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DEST"
launchctl enable "gui/$(id -u)/${LABEL}"

echo "launchd job installed and started: ${LABEL}"
echo "check status: launchctl print gui/$(id -u)/${LABEL}"
echo "logs: tail -f logs/bot.out"
echo ""
echo "to stop and remove auto-restart, run: scripts/uninstall_launchd.sh"
