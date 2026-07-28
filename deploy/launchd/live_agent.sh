#!/bin/bash
# Boot- and crash-persistent supervision for the LIVE engine (closes risk R1).
#
# WHY: com.cgc.tradingbot is notify-only (pgrep + osascript); it has no KeepAlive
# and never starts anything. Before this agent existed, a host reboot or an engine
# crash left the live bot offline indefinitely, with open positions no longer
# managed by the TP/SL lifecycle.
#
# AUTHORISATION IS NEVER SELF-GRANTED. This agent restores a live session that the
# OWNER already authorised; it cannot create one. It refuses to start unless
# state/LIVE_PILOT_AUTHORISATION exists (the owner-signed token that
# scripts/launch_live.sh LAYER 2 requires) and a live session was actually started
# (state/live_runtime.state). Absent either, it exits 0 and launchd does not
# respawn it, because KeepAlive is SuccessfulExit=false.
#
# Stop cleanly (survives reboot):  touch state/live.stop
# Resume:                          rm state/live.stop
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$PROJECT_DIR"
mkdir -p logs state
# stdout only: launchd already captures it to logs/launchd_live.out via
# StandardOutPath. Tee-ing to that same file duplicated every line.
log() { printf '%s | LIVE_AGENT | %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1"; }

log "starting (commit=$(git rev-parse --short HEAD 2>/dev/null || echo unknown))"

# --- refusals that must NOT crash-loop: exit 0 so launchd leaves us alone ---

if [ -f state/live.stop ]; then
  log "stop flag present (state/live.stop); not starting"
  exit 0
fi

if [ ! -f state/LIVE_PILOT_AUTHORISATION ]; then
  log "REFUSING: no owner authorisation token (state/LIVE_PILOT_AUTHORISATION)."
  log "          this agent restores an authorised session; it cannot create one."
  exit 0
fi

if [ ! -f state/live_runtime.state ]; then
  log "REFUSING: no live session on record (state/live_runtime.state)."
  log "          the owner must start the first session via scripts/launch_live.sh."
  exit 0
fi

if [ ! -f .env.live ]; then
  log "REFUSING: .env.live missing"
  exit 0
fi

if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  # A dirty tree would otherwise fail every single restart, forever.
  log "REFUSING: working tree not clean"
  exit 0
fi

if pgrep -f "[Pp]ython(3)?.*(-m )?app\.main" >/dev/null 2>&1; then
  log "engine already running; nothing to do"
  exit 0
fi

# --- R2 precondition: never resurrect the engine onto a host that will sleep ---
# A bot that is restarted onto a sleeping host reproduces the 22 h silent
# suspension instead of fixing it.
. scripts/lib/power_assertion.sh 2>/dev/null || true
SLEEP_MIN="$(pmset -g 2>/dev/null | awk '$1=="sleep" {print $2; exit}')"
if [ -n "${SLEEP_MIN:-}" ] && [ "$SLEEP_MIN" != "0" ]; then
  log "REFUSING: host idle sleep is ${SLEEP_MIN} min; continuous operation impossible."
  log "          owner fix: sudo pmset -a sleep 0 disablesleep 1"
  exit 0
fi

# --- start the engine ---
log "restoring authorised live session"
set -a; . ./.env.live; set +a

BOT_PID="$(.venv/bin/python scripts/launch_detached.py --stdout logs/live.out -- .venv/bin/python -u -m app.main)"
echo "$BOT_PID" > state/bot.pid
{
  echo "mode=LIVE"; echo "pid=$BOT_PID"
  echo "started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "env_file=.env.live"; echo "commit=$(git rev-parse HEAD)"
  echo "operator=launchd:com.cgc.live"
} > state/live_runtime.state

sleep 2
if ! ps -p "$BOT_PID" >/dev/null 2>&1; then
  log "engine failed to stay up (pid $BOT_PID); see logs/live.out"
  exit 1          # non-zero -> launchd retries after ThrottleInterval
fi

hold_power_assertion "$BOT_PID" "live" 2>/dev/null || true
log "engine running (pid $BOT_PID)"

# Stay in the foreground for the lifetime of the engine so launchd's KeepAlive
# tracks the engine, not a script that already exited.
while ps -p "$BOT_PID" >/dev/null 2>&1; do sleep 10; done

log "engine exited; agent returning non-zero so launchd restarts it"
exit 1
