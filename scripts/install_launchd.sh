#!/bin/bash
# Installs a launchd NOTIFICATION-ONLY watchdog: it runs briefly every
# ~5 minutes (via pgrep, no separate script execution) and shows a macOS
# notification if the bot isn't running. It never (re)starts the bot
# itself -- a background child launched this way gets killed by macOS
# process-coalition cleanup the instant the launchd-spawned checker
# shell exits (confirmed directly; neither `nohup` nor `setsid` avoid
# it), and keeping the bot directly under launchd (KeepAlive) hit an
# unrelated, unexplained EX_CONFIG deep inside the live scan cycle. So
# restarting stays manual: run scripts/start_bot.sh when notified.
#
# This script installs and LOADS the job. It only ever runs `pgrep` and
# (conditionally) a macOS notification -- it cannot start, stop, or
# otherwise touch the bot process, so it's safe to run regardless of
# whether the bot is currently running.
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

echo "launchd notifier installed: ${LABEL} (checks every ~5 min, notifies if the bot isn't running)"
echo "check status: launchctl print gui/$(id -u)/${LABEL}"
echo "is the bot itself running: pgrep -f app.main"
echo "if notified the bot is down, restart it with: scripts/start_bot.sh"
echo ""
echo "to remove this notifier, run: scripts/uninstall_launchd.sh"
