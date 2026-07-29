#!/bin/bash
# Install the 60s LIVE watchdog schedule. Read-only monitoring only.
set -uo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$PROJECT_DIR"
LABEL="com.cgc.watchdog"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
TEMPLATE="deploy/launchd/com.cgc.watchdog.plist.template"

case "$PROJECT_DIR" in
  *"/Desktop/"*|*"/Documents/"*|*"/Downloads/"*)
    echo "ABORT: project is in a TCC-protected directory; launchd cannot read it"; exit 1 ;;
esac
[ -f "$TEMPLATE" ] || { echo "ABORT: template missing"; exit 1; }
[ -z "$(git status --porcelain)" ] || { echo "ABORT: working tree not clean"; exit 1; }

mkdir -p "$HOME/Library/LaunchAgents" logs
sed "s|__PROJECT_DIR__|$PROJECT_DIR|g" "$TEMPLATE" > "$DEST"
plutil -lint "$DEST" >/dev/null || { echo "ABORT: plist invalid"; exit 1; }
echo "wrote $DEST"
launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$DEST" || { echo "ABORT: bootstrap failed"; exit 1; }
launchctl enable "gui/$(id -u)/$LABEL"
launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 \
  || { echo "ABORT: $LABEL not bootstrapped"; exit 1; }
echo "bootstrapped: $LABEL (60s interval)"
printf '%s | LAUNCHD_INSTALL | label=%s | commit=%s\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$LABEL" "$(git rev-parse --short HEAD)" >> logs/runtime.log
echo "  logs: logs/watchdog_live.out"
echo "  stop: launchctl bootout gui/$(id -u)/$LABEL"
