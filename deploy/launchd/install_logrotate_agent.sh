#!/bin/bash
# Install the hourly lossless log-rotation schedule (OWNER ACTION; nothing in this repository runs it).
#   usage: bash deploy/launchd/install_logrotate_agent.sh <paths-file> <archive-dir> [python]
# <paths-file>: one regenerable log path per line. <archive-dir>: where the .gz parts and MANIFEST.jsonl go.
set -uo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$PROJECT_DIR"
LABEL="com.cgc.logrotate"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
TEMPLATE="deploy/launchd/com.cgc.logrotate.plist.template"
PATHS_FILE="${1:-}"; ARCHIVE_DIR="${2:-}"; PYTHON="${3:-python3}"
[ -n "$PATHS_FILE" ] && [ -f "$PATHS_FILE" ] || { echo "ABORT: paths file missing (usage: install_logrotate_agent.sh <paths-file> <archive-dir> [python])"; exit 1; }
[ -n "$ARCHIVE_DIR" ] || { echo "ABORT: archive dir missing"; exit 1; }
case "$PROJECT_DIR" in
  *"/Desktop/"*|*"/Documents/"*|*"/Downloads/"*)
    echo "ABORT: project is in a TCC-protected directory; launchd cannot read it"; exit 1 ;;
esac
"$PYTHON" scripts/rotate_regenerable_logs.py --archive-dir "$ARCHIVE_DIR" --paths-file "$PATHS_FILE" >/dev/null \
  || { echo "ABORT: dry run refused a listed path"; exit 1; }
mkdir -p "$HOME/Library/LaunchAgents" logs "$ARCHIVE_DIR"
sed -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" -e "s|__PYTHON__|$PYTHON|g" -e "s|__PATHS_FILE__|$PATHS_FILE|g" \
    -e "s|__ARCHIVE_DIR__|$ARCHIVE_DIR|g" "$TEMPLATE" > "$DEST"
plutil -lint "$DEST" >/dev/null || { echo "ABORT: plist invalid"; exit 1; }
launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$DEST" || { echo "ABORT: bootstrap failed"; exit 1; }
echo "bootstrapped: $LABEL (hourly). stop: launchctl bootout gui/$(id -u)/$LABEL"
