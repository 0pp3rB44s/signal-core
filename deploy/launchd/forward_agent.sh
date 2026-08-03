#!/bin/bash
# launchd entry point for boot-persistent FORWARD PAPER supervision.
#
# launchd keeps THIS process alive; this process keeps the engine alive via the
# existing health-check-driven keepalive. Two levels, so a reboot, a logout or a
# supervisor crash all recover without a human.
#
# Honours the same stop flag as the manual path: creating
# state/forward_paper_keepalive.stop makes this exit cleanly and launchd will not
# restart it (SuccessfulExit=false means only NON-zero exits are restarted).
set -uo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$PROJECT_DIR"
mkdir -p logs state
LOG="logs/launchd_forward.out"

log() { printf '%s | AGENT | %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" | tee -a "$LOG"; }

log "starting (commit=$(git rev-parse --short HEAD 2>/dev/null || echo unknown))"

if [ -f state/forward_paper_keepalive.stop ]; then
  log "stop flag present; exiting cleanly, launchd will not restart"
  exit 0
fi

# Guard the same preconditions the launcher does, so launchd does not spin.
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  log "FATAL: working tree not clean; refusing to start (would fail every restart)"
  exit 0   # clean exit: do NOT crash-loop on an operator problem
fi

export SCAN_INTERVAL="${SCAN_INTERVAL:-60}"
log "handing off to forward_paper_keepalive --loop 120"
exec bash scripts/forward_paper_keepalive.sh --loop 120
