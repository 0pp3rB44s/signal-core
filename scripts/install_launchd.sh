#!/bin/bash
# Installs a launchd WATCHDOG job: it runs briefly every ~5 minutes (via
# pgrep, no separate script execution), and starts the bot in the
# background only if it isn't already running. This is the recommended
# way to make sure the bot survives a crash or reboot, instead of relying
# on remembering to re-run scripts/start_bot.sh's manual nohup start.
#
# This script installs and LOADS the job, which checks immediately
# (RunAtLoad) -- if the bot isn't already running, it will be started at
# that point. Only run this when you are ready for the bot to actually
# start trading (or it's already running, in which case this is a no-op).
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

echo "launchd watchdog installed: ${LABEL} (checks every ~5 min, starts the bot if it isn't running)"
echo "check status: launchctl print gui/$(id -u)/${LABEL}"
echo "is the bot itself running: pgrep -f app.main"
echo "logs: tail -f logs/bot.out"
echo ""
echo "to stop and remove auto-restart, run: scripts/uninstall_launchd.sh"
