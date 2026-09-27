#!/bin/bash
# Install boot-persistent FORWARD PAPER supervision (RC2 Phase 3, closes R1).
# Reversible: deploy/launchd/uninstall_forward_agent.sh removes it completely.
set -uo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$PROJECT_DIR"
LABEL="com.cgc.forward"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
TEMPLATE="deploy/launchd/com.cgc.forward.plist.template"

[ -f "$TEMPLATE" ] || { echo "ABORT: template missing"; exit 1; }
[ -z "$(git status --porcelain)" ] || { echo "ABORT: working tree not clean"; exit 1; }

mkdir -p "$HOME/Library/LaunchAgents" logs
sed "s|__PROJECT_DIR__|$PROJECT_DIR|g" "$TEMPLATE" > "$DEST"
echo "wrote $DEST"

launchctl unload "$DEST" 2>/dev/null || true
launchctl load  "$DEST" || { echo "ABORT: launchctl load failed"; exit 1; }

sleep 2
if launchctl list | grep -q "$LABEL"; then
  echo "loaded: $(launchctl list | grep "$LABEL")"
  printf '%s | LAUNCHD_INSTALL | label=%s | commit=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$LABEL" "$(git rev-parse --short HEAD)" >> logs/runtime.log
else
  echo "ABORT: agent did not appear in launchctl list"; exit 1
fi
echo
echo "Boot persistence ACTIVE. Verify with a real reboot before relying on it."
echo "Stop cleanly:  touch state/forward_paper_keepalive.stop"
echo "Remove:        bash deploy/launchd/uninstall_forward_agent.sh"
