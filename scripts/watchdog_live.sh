#!/bin/bash
# Runtime-mode-aware watchdog for the LIVE engine.
#
# scripts/watchdog.sh is forward-paper shaped: it greps for
# forward_paper_keepalive, reads validation_72h snapshots and runs
# check_forward_paper.sh. In a LIVE deployment those three fire on every single
# run, so a 60s cadence would emit ~864 false alerts/day and train the operator
# to ignore the channel. This watchdog selects its checks by runtime mode.
#
# READ-ONLY. It never restarts, stops or steers anything, never imports the
# execution stack, and performs no exchange call of any kind.
#
#   watchdog_live.sh              evaluate and deliver
#   watchdog_live.sh --dry-run    evaluate, print, deliver nothing, persist nothing
#   watchdog_live.sh --no-deliver evaluate and persist state, but never send
#
# Env overrides (used by tests to work on isolated fixtures):
#   WD_STATE_DIR   default state
#   WD_HEARTBEAT   default $WD_STATE_DIR/runtime_heartbeat.json
#   WD_COOLDOWN_SEC default 1800
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"; cd "$PROJECT_DIR"

DRY_RUN=0; NO_DELIVER=0
for arg in "$@"; do
  case "$arg" in
    --dry-run)    DRY_RUN=1; NO_DELIVER=1 ;;
    --no-deliver) NO_DELIVER=1 ;;
  esac
done

WD_STATE_DIR="${WD_STATE_DIR:-state}"
HB="${WD_HEARTBEAT:-$WD_STATE_DIR/runtime_heartbeat.json}"
BOT_PID_FILE="$WD_STATE_DIR/bot.pid"
ALERT_STATE="$WD_STATE_DIR/watchdog_alerts.json"
WD_HB="$WD_STATE_DIR/watchdog_live_heartbeat.json"
LOCK="$WD_STATE_DIR/watchdog_live.lock"
COOLDOWN="${WD_COOLDOWN_SEC:-1800}"
MAX_HB_AGE="${MAX_HEARTBEAT_AGE_SEC:-600}"

mkdir -p "$WD_STATE_DIR" logs
NOW=$(date -u +%s)
NOW_ISO=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# --- single instance ------------------------------------------------------
# flock is non-blocking: a second invocation exits 0 without evaluating or
# sending, so an overrunning run can never double-alert.
exec 9>"$LOCK"
if command -v flock >/dev/null 2>&1; then
  flock -n 9 || { echo "watchdog already running; exiting"; exit 0; }
else
  # macOS has no flock(1). mkdir is atomic; a stale dir older than 10 min is
  # reclaimed so a killed run cannot wedge the schedule permanently.
  LOCKDIR="$LOCK.d"
  if ! mkdir "$LOCKDIR" 2>/dev/null; then
    LOCK_AGE=$(( NOW - $(stat -f %m "$LOCKDIR" 2>/dev/null || echo "$NOW") ))
    if [ "$LOCK_AGE" -lt 600 ]; then
      echo "watchdog already running (lock ${LOCK_AGE}s old); exiting"; exit 0
    fi
    echo "reclaiming stale lock (${LOCK_AGE}s)"
    rmdir "$LOCKDIR" 2>/dev/null || true
    mkdir "$LOCKDIR" 2>/dev/null || { echo "lock contended; exiting"; exit 0; }
  fi
  trap 'rmdir "$LOCKDIR" 2>/dev/null || true' EXIT
fi

# --- helpers --------------------------------------------------------------
FINDINGS=0
declare -a ACTIVE_KEYS=()

age_of() { [ -f "$1" ] || { echo -1; return; }; echo $(( NOW - $(stat -f %m "$1") )); }
jget() {  # jget <file> <key>  — value or empty; never fails the script
  [ -f "$1" ] || { echo ""; return; }
  /usr/bin/python3 -c "
import json,sys
try:
    d=json.load(open(sys.argv[1]))
    v=d.get(sys.argv[2])
    print('' if v is None else v)
except Exception:
    print('')
" "$1" "$2" 2>/dev/null
}
report() { printf '  [%s] %s\n' "$1" "$2"; [ "$1" = "OK" ] || FINDINGS=$((FINDINGS+1)); }

# --- runtime mode ---------------------------------------------------------
# Prefer a FRESH heartbeat's own mode; fall back to configured execution mode;
# otherwise UNKNOWN. Never assume LIVE, never assume forward-paper.
HB_AGE=$(age_of "$HB")
MODE="UNKNOWN"; MODE_SRC="none"
if [ "$HB_AGE" -ge 0 ] && [ "$HB_AGE" -le "$MAX_HB_AGE" ]; then
  M="$(jget "$HB" mode)"
  if [ -n "$M" ] && [ "$M" != "UNKNOWN" ]; then MODE="$M"; MODE_SRC="heartbeat"; fi
fi
if [ "$MODE" = "UNKNOWN" ] && [ -f .env.live ]; then
  M="$(grep -m1 '^EXECUTION_MODE=' .env.live 2>/dev/null | cut -d= -f2- | tr -d '"'"'"' ')"
  if [ -n "$M" ]; then MODE="$M"; MODE_SRC="env.live"; fi
fi

echo "=== WATCHDOG(live) $NOW_ISO | mode=$MODE (via $MODE_SRC)$([ $DRY_RUN -eq 1 ] && echo ' | DRY-RUN') ==="

# --- alert plumbing -------------------------------------------------------
# raise <key> <severity> <message>
raise() {
  local key="$1" sev="$2" msg="$3"
  ACTIVE_KEYS+=("$key")
  [ $DRY_RUN -eq 1 ] && { echo "      would alert: [$sev] $key"; return; }
  /usr/bin/python3 scripts/lib/wd_alert_state.py raise \
    --state "$ALERT_STATE" --key "$key" --severity "$sev" \
    --message "$msg" --now "$NOW" --cooldown "$COOLDOWN" \
    --no-deliver "$NO_DELIVER"
}

# ===================== CHECKS ============================================

# 1-4. engine process identity
BOT_PID="$(cat "$BOT_PID_FILE" 2>/dev/null || echo '')"
ENGINE_ALIVE=false
if [ -z "$BOT_PID" ]; then
  report FAIL "bot.pid missing"
  raise BOT_PID_MISSING CRITICAL "state/bot.pid absent — engine identity unknown"
elif ! kill -0 "$BOT_PID" 2>/dev/null; then
  report FAIL "stored PID $BOT_PID is not alive"
  raise BOT_STOPPED CRITICAL "engine PID $BOT_PID from bot.pid is not running"
else
  CMD="$(ps -o command= -p "$BOT_PID" 2>/dev/null)"
  case "$CMD" in
    *app.main*) ENGINE_ALIVE=true; report OK "engine alive (pid $BOT_PID)" ;;
    *) report FAIL "PID $BOT_PID is not the engine"
       raise BOT_PID_WRONG_COMMAND CRITICAL "PID $BOT_PID does not run app.main — recycled pid" ;;
  esac
fi

PIDS="$(pgrep -f '[Pp]ython(3)?.*(-m )?app\.main' 2>/dev/null | tr '\n' ' ')"
PIDCOUNT=$(echo "$PIDS" | wc -w | tr -d ' ')
if [ "$PIDCOUNT" -gt 1 ]; then
  report FAIL "duplicate engines: $PIDS"
  raise DUPLICATE_ENGINE CRITICAL "$PIDCOUNT engine processes running ($PIDS) — double-submit risk"
else
  report OK "single engine process"
fi

# 5-7. heartbeat presence, freshness, and advancement
if [ "$HB_AGE" -lt 0 ]; then
  report FAIL "heartbeat missing"
  raise HEARTBEAT_MISSING CRITICAL "$HB absent"
elif [ "$HB_AGE" -gt "$MAX_HB_AGE" ]; then
  report FAIL "heartbeat STALE (${HB_AGE}s > ${MAX_HB_AGE}s)"
  if $ENGINE_ALIVE; then
    raise HEARTBEAT_NOT_ADVANCING CRITICAL \
      "engine alive but heartbeat ${HB_AGE}s stale — wedged process or host sleep"
  else
    raise HEARTBEAT_STALLED CRITICAL "heartbeat ${HB_AGE}s stale"
  fi
else
  report OK "heartbeat fresh (${HB_AGE}s)"
fi

# 8. repeated scan failures, from heartbeat v2 error fields
HB_SCHEMA="$(jget "$HB" schema_version)"
if [ "${HB_SCHEMA:-1}" -ge 2 ] 2>/dev/null; then
  LAST_ERR="$(jget "$HB" last_error_type)"
  LAST_OK="$(jget "$HB" last_successful_scan_utc)"
  STAGE="$(jget "$HB" stage)"
  if [ -n "$LAST_ERR" ] && [ "$STAGE" = "scan_cycle_failed" ]; then
    report FAIL "last scan failed: $LAST_ERR"
    raise SCAN_FAILING HIGH "scan cycles failing (last_error_type=$LAST_ERR)"
  else
    report OK "scan telemetry ok (last success ${LAST_OK:-unknown})"
  fi
else
  report WARN "heartbeat schema v${HB_SCHEMA:-1} — scan-failure telemetry unavailable"
fi

# 8b. runtime incidents visible only in the engine log.
# The heartbeat reports whether the *last* cycle failed; it says nothing about
# authentication, reconciliation, order rejections, exceptions or a resolver
# outage. Those were undetectable until now: the 3h23m DNS episode on
# 2026-07-30 produced 3714 failures and 181 dead scan cycles without raising a
# single alert. Bounded tail so this stays cheap at a 60s interval.
LIVE_LOG="logs/live.out"
if [ -r "$LIVE_LOG" ]; then
  WINDOW="$(tail -n 3000 "$LIVE_LOG" 2>/dev/null)"
  count_in_window() { printf '%s\n' "$WINDOW" | grep -cE "$1" 2>/dev/null || true; }

  DNS_FAILS="$(count_in_window 'BITGET_DNS_RESOLUTION_FAILURE')"
  if [ "${DNS_FAILS:-0}" -ge 20 ]; then
    report FAIL "DNS resolution failing ($DNS_FAILS in last 3000 log lines)"
    raise DNS_INSTABILITY HIGH "resolver failing: $DNS_FAILS DNS errors in the recent log window"
  else
    report OK "DNS healthy ($DNS_FAILS recent resolution errors)"
  fi

  AUTH_FAILS="$(count_in_window 'status=401|status=403|invalid signature|code=40037|code=40009')"
  if [ "${AUTH_FAILS:-0}" -ge 1 ]; then
    report FAIL "authentication errors ($AUTH_FAILS)"
    raise AUTH_FAILURE CRITICAL "Bitget authentication failing: $AUTH_FAILS recent errors"
  else
    report OK "authentication clean"
  fi

  RECON_FAILS="$(count_in_window 'RECONCIL[A-Z_]*(FAIL|MISMATCH|ERROR)|ORPHAN_POSITION')"
  if [ "${RECON_FAILS:-0}" -ge 1 ]; then
    report FAIL "reconciliation problems ($RECON_FAILS)"
    raise RECONCILIATION_FAILURE CRITICAL "position reconciliation failing: $RECON_FAILS recent events"
  else
    report OK "reconciliation clean"
  fi

  # Only genuine exchange rejections. NOT_SENT_PRE_TRANSPORT is a local refusal
  # that never reached the venue, and AMBIGUOUS has its own handling, so neither
  # belongs here.
  ORDER_REJECTS="$(count_in_window 'classification=REJECTED|BitgetOrderRejected')"
  if [ "${ORDER_REJECTS:-0}" -ge 1 ]; then
    report FAIL "exchange rejected orders ($ORDER_REJECTS)"
    raise ORDER_REJECTED HIGH "exchange rejected $ORDER_REJECTS order(s) — check size/notional/margin"
  else
    report OK "no exchange order rejections"
  fi

  TRACEBACKS="$(count_in_window '^Traceback \(most recent call last\)')"
  if [ "${TRACEBACKS:-0}" -ge 1 ]; then
    report FAIL "unhandled exceptions ($TRACEBACKS tracebacks)"
    raise UNEXPECTED_EXCEPTION HIGH "$TRACEBACKS traceback(s) in the recent engine log"
  else
    report OK "no unhandled exceptions"
  fi
else
  report WARN "engine log $LIVE_LOG unreadable — runtime incidents undetectable"
fi

# 9. supervisor, selected by mode
case "$MODE" in
  LIVE)
    # `launchctl list` is racy for a job that exits 0 and is not currently
    # running: it intermittently omits the label, which produced a false
    # SUPERVISOR_MISSING at 12:56:23Z during scheduler validation. `launchctl
    # print` answers the real question — is the service bootstrapped — and is
    # stable across that window.
    if launchctl print "gui/$(id -u)/com.cgc.live" >/dev/null 2>&1; then
      report OK "live supervisor bootstrapped (com.cgc.live)"
    else
      report FAIL "live supervisor com.cgc.live NOT bootstrapped"
      raise SUPERVISOR_MISSING HIGH "com.cgc.live not bootstrapped — no crash/boot recovery"
    fi ;;
  DRY_RUN|FORWARD_PAPER)
    if pgrep -f forward_paper_keepalive >/dev/null 2>&1; then
      report OK "forward-paper supervisor alive"
    else
      report WARN "forward-paper supervisor not running"
    fi ;;
  *)
    report WARN "supervisor check skipped (mode UNKNOWN)" ;;
esac

# 10. alert provider configuration
if [ -f "$WD_STATE_DIR/alerting.env" ]; then
  PROV="$(grep -m1 '^ALERT_PROVIDER=' "$WD_STATE_DIR/alerting.env" | cut -d= -f2- | tr -d '"'"'"' ')"
  PERMS="$(stat -f %Lp "$WD_STATE_DIR/alerting.env" 2>/dev/null)"
  if [ -z "$PROV" ] || [ "$PROV" = "none" ]; then
    report FAIL "alert provider not selected"
    raise ALERTING_UNCONFIGURED HIGH "alerting.env present but ALERT_PROVIDER is unset"
  elif [ "$PERMS" != "600" ]; then
    report FAIL "alerting.env permissions $PERMS (expected 600)"
    raise ALERTING_PERMISSIONS HIGH "state/alerting.env is $PERMS; secrets must be 600"
  else
    report OK "alert provider configured ($PROV, 600)"
  fi
else
  report FAIL "no alert provider — incidents stay local"
  raise ALERTING_UNCONFIGURED HIGH "state/alerting.env absent; no external delivery"
fi

# 11. dashboard health — loopback GET, read-only, strict timeout
DASH_PORT="${DASHBOARD_PORT:-8501}"
DASH_CODE="$(curl -s -m 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$DASH_PORT/login" 2>/dev/null || echo 000)"
case "$DASH_CODE" in
  200|302) report OK "dashboard responding ($DASH_CODE)" ;;
  000)     report WARN "dashboard not reachable on 127.0.0.1:$DASH_PORT" ;;
  *)       report FAIL "dashboard HTTP $DASH_CODE"
           raise DASHBOARD_UNHEALTHY HIGH "dashboard returned HTTP $DASH_CODE" ;;
esac

# 12. the watchdog's own previous heartbeat
PREV_AGE=$(age_of "$WD_HB")
if [ "$PREV_AGE" -ge 0 ] && [ "$PREV_AGE" -gt 900 ]; then
  report WARN "previous watchdog run was ${PREV_AGE}s ago (scheduler may have stalled)"
fi

# --- resolve cleared incidents -------------------------------------------
if [ $DRY_RUN -eq 0 ]; then
  /usr/bin/python3 scripts/lib/wd_alert_state.py resolve \
    --state "$ALERT_STATE" --now "$NOW" --no-deliver "$NO_DELIVER" \
    --active "$(IFS=,; echo "${ACTIVE_KEYS[*]:-}")"
fi

# 13. extended checks: AdaptiveTrend scan state, exchange truth, position
# protection, risk, deployment SHA classification, logging lag, first-trade
# watch. LIVE-mode only — these all assume a live-shaped state layout and
# would just be noise in DRY_RUN/FORWARD_PAPER. Best-effort: a failure here
# must never fail this script or block its own heartbeat write below.
if [ "$MODE" = "LIVE" ]; then
  # wd_extended.py imports dashboard_v3, which needs the project's own
  # dependencies (pydantic, flask, ...) — the system python3 does not have
  # them. Use the same interpreter the engine itself runs under.
  EXT_PY=".venv/bin/python3"
  [ -x "$EXT_PY" ] || EXT_PY="/usr/bin/python3"
  # dashboard_v3.panels.exchange needs Bitget credentials for its read-only
  # (GET-only, guard-tested) account/position calls. live_agent.sh gives the
  # engine these via env_guard.sh's guard_load_env; this watchdog is a
  # separate process tree and inherits none of that. Export the same
  # .env.live into THIS subshell only — set -a/+a scopes it to the
  # environment, never the trading engine's own process, and a missing file
  # degrades to "exchange unreachable" rather than aborting the watchdog.
  if [ -f .env.live ]; then
    set -a; . .env.live 2>/dev/null; set +a
  fi
  EXT_ARGS=(--now "$NOW")
  [ $DRY_RUN -eq 1 ] && EXT_ARGS+=(--dry-run)
  [ $NO_DELIVER -eq 1 ] && EXT_ARGS+=(--no-deliver)
  if command -v timeout >/dev/null 2>&1; then
    timeout 45 "$EXT_PY" scripts/wd_extended.py "${EXT_ARGS[@]}" 2>&1 | sed 's/^/  [ext] /' || true
  else
    "$EXT_PY" scripts/wd_extended.py "${EXT_ARGS[@]}" 2>&1 | sed 's/^/  [ext] /' || true
  fi
fi

# --- own heartbeat --------------------------------------------------------
if [ $DRY_RUN -eq 0 ]; then
  printf '{"watchdog_utc":"%s","mode":"%s","mode_source":"%s","findings":%d,"engine_alive":%s,"engine_pid":"%s","heartbeat_age":%s}\n' \
    "$NOW_ISO" "$MODE" "$MODE_SRC" "$FINDINGS" "$ENGINE_ALIVE" "${BOT_PID:-}" "$HB_AGE" > "$WD_HB.tmp"
  mv -f "$WD_HB.tmp" "$WD_HB"
fi

echo "=== findings: $FINDINGS ==="
exit 0
