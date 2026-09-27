#!/bin/bash
# Installs only com.cgc.dashboard. Does not load/restart com.cgc.live.
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$PROJECT_DIR"
LABEL=com.cgc.dashboard; DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
case "$PROJECT_DIR" in *"/Desktop/"*|*"/Documents/"*|*"/Downloads/"*) echo "ABORT: launchd cannot reliably read a TCC-protected project path"; exit 1;; esac
[ -z "$(git status --porcelain)" ] || { echo "ABORT: worktree must be clean"; exit 1; }
mkdir -p "$HOME/Library/LaunchAgents" logs
sed "s|__PROJECT_DIR__|$PROJECT_DIR|g" deploy/launchd/com.cgc.dashboard.plist.template > "$DEST"
plutil -lint "$DEST" >/dev/null
launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$DEST"
launchctl enable "gui/$(id -u)/$LABEL"
launchctl print "gui/$(id -u)/$LABEL" >/dev/null
echo "bootstrapped $LABEL; LIVE service untouched"
