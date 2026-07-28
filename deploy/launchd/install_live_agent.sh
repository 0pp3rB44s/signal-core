#!/bin/bash
# Install boot/crash persistence for the LIVE engine (risk R1).
# Installing the agent does NOT start trading: the agent refuses to run without
# the owner-signed state/LIVE_PILOT_AUTHORISATION token.
set -uo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$PROJECT_DIR"
LABEL="com.cgc.live"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
TEMPLATE="deploy/launchd/com.cgc.live.plist.template"
[ -f "$TEMPLATE" ] || { echo "ABORT: template missing"; exit 1; }
[ -z "$(git status --porcelain)" ] || { echo "ABORT: working tree not clean"; exit 1; }
mkdir -p "$HOME/Library/LaunchAgents" logs
sed "s|__PROJECT_DIR__|$PROJECT_DIR|g" "$TEMPLATE" > "$DEST"
echo "wrote $DEST"
launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$DEST" || { echo "ABORT: bootstrap failed"; exit 1; }
launchctl enable "gui/$(id -u)/$LABEL"
sleep 2
launchctl list | grep -q "$LABEL" || { echo "ABORT: agent not in launchctl list"; exit 1; }
echo "loaded: $(launchctl list | grep "$LABEL")"
printf '%s | LAUNCHD_INSTALL | label=%s | commit=%s\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$LABEL" "$(git rev-parse --short HEAD)" >> logs/runtime.log
echo
echo "Boot/crash persistence ACTIVE for the live engine."
echo "  it starts the engine only when state/LIVE_PILOT_AUTHORISATION exists"
echo "  stop cleanly (survives reboot): touch state/live.stop"
echo "  remove:                          bash deploy/launchd/uninstall_live_agent.sh"
