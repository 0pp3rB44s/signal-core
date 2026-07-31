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
# Verify with `launchctl print`, not `launchctl list`. RunAtLoad fires
# immediately and this agent exits 0 when it declines to start, so its presence
# in `list` races with its own execution; `print` answers the real question
# ("is the service bootstrapped?") and is stable across that window.
launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 \
  || { echo "ABORT: $LABEL is not bootstrapped"; exit 1; }
echo "bootstrapped: $LABEL"
launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null \
  | grep -E "^\s*(state|runs|last exit code) " | sed 's/^/  /' 
printf '%s | LAUNCHD_INSTALL | label=%s | commit=%s\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$LABEL" "$(git rev-parse --short HEAD)" >> logs/runtime.log
echo
echo "Boot/crash persistence ACTIVE for the live engine."
echo "  it starts the engine only when state/LIVE_PILOT_AUTHORISATION exists"
echo "  stop cleanly (survives reboot): touch state/live.stop"
echo "  remove:                          bash deploy/launchd/uninstall_live_agent.sh"
