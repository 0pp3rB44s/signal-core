#!/bin/bash
# Cross-component watchdog (RC2 Phase 4, closes R3 and R4).
#
# The audit found three silent failures: the monitor died unnoticed, a 22.19 h
# process suspension raised nothing, and 7.82 h of process death went unreported.
# Root cause: every observer was pull-based, local, and unobserved itself.
#
# This watchdog is INDEPENDENT of the supervisor and the monitor. It validates
# all three components against each other and writes its OWN heartbeat, so the
# watchdog is itself observable. Run it from launchd on an interval.
#
# Read-only: it never restarts, stops or steers anything. It observes and alerts.
set -uo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"; cd "$PROJECT_DIR"
. scripts/lib/alert.sh

MAX_HEARTBEAT_AGE_SEC="${MAX_HEARTBEAT_AGE_SEC:-600}"   # engine heartbeat staleness
MAX_MONITOR_AGE_SEC="${MAX_MONITOR_AGE_SEC:-7200}"      # monitor snapshot staleness
WD_HEARTBEAT="state/watchdog_heartbeat.json"
FINDINGS=0

now=$(date -u +%s)
age_of() { [ -f "$1" ] || { echo -1; return; }; echo $(( now - $(stat -f %m "$1") )); }
report() { echo "  [$1] $2"; [ "$1" = "OK" ] || FINDINGS=$((FINDINGS+1)); }

echo "=== WATCHDOG $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

# --- 1. trading engine ---
BOT_PID="$(cat state/bot.pid 2>/dev/null || echo '')"
ENGINE_ALIVE=false
if [ -n "$BOT_PID" ] && kill -0 "$BOT_PID" 2>/dev/null; then ENGINE_ALIVE=true; fi
if $ENGINE_ALIVE; then report OK "engine alive (pid $BOT_PID)"; else
  report FAIL "engine NOT running (bot.pid=${BOT_PID:-none})"
  send_alert CRITICAL BOT_STOPPED "trading engine not running (pid=${BOT_PID:-none})" || true
fi

# --- 2. engine heartbeat freshness (closes R4) ---
HB_AGE=$(age_of state/runtime_heartbeat.json)
if [ "$HB_AGE" -lt 0 ]; then
  report FAIL "engine heartbeat missing"
  send_alert CRITICAL HEARTBEAT_MISSING "state/runtime_heartbeat.json absent" || true
elif [ "$HB_AGE" -gt "$MAX_HEARTBEAT_AGE_SEC" ]; then
  report FAIL "engine heartbeat STALE (${HB_AGE}s > ${MAX_HEARTBEAT_AGE_SEC}s)"
  send_alert CRITICAL HEARTBEAT_STALLED "engine heartbeat stale ${HB_AGE}s - possible host sleep or hang" || true
else
  report OK "engine heartbeat fresh (${HB_AGE}s)"
fi

# --- 3. supervisor ---
if pgrep -f forward_paper_keepalive >/dev/null 2>&1; then report OK "supervisor alive"; else
  report FAIL "supervisor NOT running"
  send_alert HIGH SUPERVISOR_DOWN "forward_paper_keepalive not running" || true
fi

# --- 4. monitor liveness (closes R3 - nothing used to watch the monitor) ---
LATEST_SNAP="$(ls -t validation_72h/snapshots/*.json 2>/dev/null | head -1)"
SNAP_AGE=$(age_of "${LATEST_SNAP:-/nonexistent}")
if [ "$SNAP_AGE" -lt 0 ]; then
  report WARN "no monitor snapshot found (monitor may not be running)"
elif [ "$SNAP_AGE" -gt "$MAX_MONITOR_AGE_SEC" ]; then
  report FAIL "monitor STALE (${SNAP_AGE}s) - this is the R3 failure mode"
  send_alert HIGH MONITOR_DEAD "monitor produced no snapshot for ${SNAP_AGE}s" || true
else
  report OK "monitor fresh (${SNAP_AGE}s)"
fi

# --- 5. boot persistence loaded ---
if launchctl list 2>/dev/null | grep -q com.cgc.forward; then report OK "launchd agent loaded"; else
  report WARN "launchd agent com.cgc.forward NOT loaded (no boot persistence)"
fi

# --- 6. restart-loop detection ---
RESTARTS=$(wc -l < state/forward_paper_keepalive.history 2>/dev/null | tr -d ' '); RESTARTS="${RESTARTS:-0}"
if [ "${RESTARTS:-0}" -ge 3 ]; then
  report FAIL "restart loop: ${RESTARTS} restarts in window"
  send_alert HIGH RESTART_LOOP "${RESTARTS} restarts recorded - supervisor fail-closed" || true
else report OK "restart budget ${RESTARTS:-0}/3"; fi

# --- 7. health check verdict ---
HEALTH="$(bash scripts/check_forward_paper.sh 2>/dev/null | awk -F= '$1=="status"{print $2}' | tail -1)"
case "${HEALTH:-}" in
  HEALTHY) report OK "health=HEALTHY" ;;
  "")      report WARN "health check returned no status" ;;
  *)       report FAIL "health=${HEALTH}"
           send_alert HIGH HEALTH_DEGRADED "health check reports ${HEALTH}" || true ;;
esac

# --- 8. disk ---
AVAIL_GB=$(( $(df -k . | tail -1 | awk '{print $4}') / 1024 / 1024 ))
if [ "$AVAIL_GB" -lt 2 ]; then
  report FAIL "disk low: ${AVAIL_GB}GiB"
  send_alert HIGH DISK_FULL "only ${AVAIL_GB}GiB free" || true
else report OK "disk ${AVAIL_GB}GiB free"; fi

# --- 9. event-chain integrity ---
if .venv/bin/python -c "
from forward_paper.store import ForwardPaperEventStore
ForwardPaperEventStore('data_store/forward_paper_events.jsonl').read_events()" 2>/dev/null; then
  report OK "event chain valid"
else
  report FAIL "event chain INVALID"
  send_alert CRITICAL LOG_CORRUPTION "forward-paper event chain failed validation" || true
fi

# --- watchdog's own heartbeat: makes the watchdog observable ---
mkdir -p state
printf '{"watchdog_utc":"%s","findings":%d,"engine_alive":%s,"health":"%s"}\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$FINDINGS" "$ENGINE_ALIVE" "${HEALTH:-unknown}" > "$WD_HEARTBEAT"

echo "=== findings: $FINDINGS ==="
[ "$FINDINGS" -eq 0 ] || exit 20
