#!/bin/bash
# Shared configuration guard. Sourced by every launcher.
#
# Purpose: make mode isolation ENFORCED rather than advisory. The audit found a
# single .env in a live-ish posture where one variable (EXECUTION_ENABLED) was
# the only barrier to placing real orders. This guard replaces that with
# per-mode invariants that are asserted independently of the env file contents.
#
# Every function aborts the caller on the first inconsistency. Nothing silently
# continues. No function here changes trading behaviour.

guard_die() { echo "ABORT: $*" >&2; exit 90; }

# --- R2: continuous operation is a host property, not a document claim -------
# The 2026-07-26 validation lost 22.19 h because the host slept while the engine
# believed it was running, with a caffeinate assertion held. Enforcement
# therefore reads the live pmset configuration at launch instead of trusting the
# risk register: a host that can idle-sleep must not run a live engine.
guard_assert_power_continuous() {
  command -v pmset >/dev/null 2>&1 || { echo "guard: pmset unavailable; power state unverifiable"; return 0; }

  # disablesleep=1 pins the machine awake and overrides the idle timer.
  if pmset -g 2>/dev/null | awk '$1=="disablesleep" {f=$2} END {exit !(f=="1")}'; then
    echo "guard: power OK (disablesleep=1)"
    return 0
  fi

  local sleep_minutes
  sleep_minutes="$(pmset -g 2>/dev/null | awk '$1=="sleep" {print $2; exit}')"
  if [ -z "$sleep_minutes" ]; then
    echo "guard: idle-sleep setting not reported; treating as unverified"
    return 0
  fi
  [ "$sleep_minutes" = "0" ] || guard_die \
"R2: host idle sleep is ${sleep_minutes} min, so continuous operation is impossible.
       An open position stops being managed the moment the host sleeps.
       Owner fix (requires sudo):  sudo pmset -a sleep 0 disablesleep 1"
  echo "guard: power OK (idle sleep disabled)"
}

# --- LAYER 1: every CRITICAL risk must be RESOLVED or explicitly ACCEPTED -----
# Replaces a grep that (a) aborted even when zero Critical risks were open,
# because `grep -c ... || echo 0` yields "0\n0" and breaks the -eq test, and
# (b) skipped the CRITICAL section entirely when no line-start
# "- **Status:** OPEN" existed anywhere, letting an open Critical risk through.
# This version reads only the CRITICAL section and requires an explicit terminal
# status per risk. Unknown or missing statuses fail closed.
guard_assert_critical_risks_cleared() {
  local register="${1:-docs/RISK_REGISTER.md}"
  [ -f "$register" ] || guard_die "LAYER 1: risk register not found: $register"

  local report
  report="$(awk '
    /^## CRITICAL/ { in_crit=1; next }
    /^## / && in_crit { in_crit=0 }
    !in_crit { next }
    /^### / { risk=$2; order[++n]=risk; next }
    /\*\*Status:\*\*/ {
      if (!risk) next
      line=$0
      sub(/.*\*\*Status:\*\*[ ]*/, "", line)
      gsub(/\*/, "", line)
      status = toupper(line)
      if (risk in state) next            # first status line per risk wins
      if (status ~ /^RESOLVED/)       state[risk]="RESOLVED"
      else if (status ~ /^ACCEPTED/)  state[risk]="ACCEPTED"
      else if (status ~ /^PARTIALLY/) state[risk]="PARTIALLY_RESOLVED"
      else if (status ~ /^OPEN/)      state[risk]="OPEN"
      else                            state[risk]="UNRECOGNISED"
    }
    END {
      for (i=1; i<=n; i++) {
        r=order[i]
        printf "%s=%s\n", r, (r in state) ? state[r] : "NO_STATUS"
      }
    }
  ' "$register")"

  [ -n "$report" ] || guard_die "LAYER 1: no CRITICAL risks found in $register (expected '## CRITICAL' with '### R<n>' entries)"

  local blocking="" line risk status
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    risk="${line%%=*}"; status="${line#*=}"
    case "$status" in
      RESOLVED|ACCEPTED) ;;
      *) blocking="${blocking}${blocking:+, }${risk}:${status}" ;;
    esac
  done <<< "$report"

  [ -z "$blocking" ] || guard_die \
"LAYER 1: CRITICAL risk(s) not resolved or accepted -> ${blocking}
       Each CRITICAL risk must read '**Status:** RESOLVED ...' or
       '**Status:** ACCEPTED ...' in ${register}. PARTIALLY RESOLVED and OPEN
       both block; an owner may accept a risk, but only explicitly and in writing."

  echo "guard: LAYER 1 OK ($(printf '%s\n' "$report" | grep -c . ) critical risk(s), all resolved or accepted)"
}

# Load an env file into the environment, ignoring comments and blanks.
guard_load_env() {
  local f="$1"
  [ -f "$f" ] || guard_die "environment file not found: $f"
  # shellcheck disable=SC2046
  set -a; . "$f"; set +a
  echo "config: loaded $f"
}

# Assert the FORWARD PAPER invariants. Aborts if live execution is reachable.
guard_assert_forward_mode() {
  [ "${FORWARD_PAPER_ONLY:-}" = "true" ]  || guard_die "forward mode requires FORWARD_PAPER_ONLY=true (got '${FORWARD_PAPER_ONLY:-unset}')"
  [ "${EXECUTION_ENABLED:-}" = "false" ]  || guard_die "forward mode requires EXECUTION_ENABLED=false (got '${EXECUTION_ENABLED:-unset}')"
  [ "${EXECUTION_MODE:-}" = "DRY_RUN" ]   || guard_die "forward mode requires EXECUTION_MODE=DRY_RUN (got '${EXECUTION_MODE:-unset}')"
  [ -z "${BITGET_API_KEY:-}" ]            || guard_die "forward mode requires a blank BITGET_API_KEY"
  [ -z "${BITGET_API_SECRET:-}" ]         || guard_die "forward mode requires a blank BITGET_API_SECRET"
  [ -z "${BITGET_API_PASSPHRASE:-}" ]     || guard_die "forward mode requires a blank BITGET_API_PASSPHRASE"
  echo "guard: FORWARD PAPER invariants OK (execution impossible, credentials blank)"
}

# Assert the LIVE invariants. Never called unless every authorisation layer passed.
guard_assert_live_mode() {
  [ "${FORWARD_PAPER_ONLY:-}" = "false" ] || guard_die "live mode requires FORWARD_PAPER_ONLY=false"
  [ "${EXECUTION_ENABLED:-}" = "true" ]   || guard_die "live mode requires EXECUTION_ENABLED=true"
  [ "${EXECUTION_MODE:-}" = "LIVE" ]      || guard_die "live mode requires EXECUTION_MODE=LIVE"
  [ -n "${BITGET_API_KEY:-}" ]            || guard_die "live mode requires BITGET_API_KEY"
  [ -n "${BITGET_API_SECRET:-}" ]         || guard_die "live mode requires BITGET_API_SECRET"
  [ -n "${BITGET_API_PASSPHRASE:-}" ]     || guard_die "live mode requires BITGET_API_PASSPHRASE"
  case "${BITGET_API_KEY:-}" in __SET_AT_DEPLOY__*) guard_die "BITGET_API_KEY is still the template placeholder";; esac
  [ "${EXECUTION_REQUIRE_CONFIRMATION:-}" = "true" ] || guard_die "live mode requires EXECUTION_REQUIRE_CONFIRMATION=true"
  [ -n "${EXECUTION_CONFIRM_SYMBOLS:-}" ] || guard_die "live mode requires a non-empty EXECUTION_CONFIRM_SYMBOLS allow-list"
  echo "guard: LIVE invariants OK"
}

# Pilot exposure ceilings. Independent of mode; applies to both.
guard_assert_pilot_limits() {
  local max_sym="${MAX_SYMBOLS:-0}" max_pos="${MAX_OPEN_POSITIONS:-0}"
  [ "$max_sym" -le 1 ] 2>/dev/null || guard_die "pilot ceiling: MAX_SYMBOLS must be <=1 (got '$max_sym')"
  [ "$max_pos" -le 1 ] 2>/dev/null || guard_die "pilot ceiling: MAX_OPEN_POSITIONS must be <=1 (got '$max_pos')"
  [ "${ALLOW_AUTO_WATCHLIST_REFRESH:-true}" = "false" ] || guard_die "pilot requires ALLOW_AUTO_WATCHLIST_REFRESH=false"
  [ "${FORWARD_PAPER_SMOKE_STRATEGY_ENABLED:-false}" = "false" ] || guard_die "smoke harness must be disabled"
  echo "guard: pilot limits OK (symbols<=$max_sym, positions<=$max_pos)"
}

# Repository must be clean: a dirty tree silently breaks every automatic restart.
guard_assert_repo_clean() {
  [ -z "$(git status --porcelain)" ] || guard_die "working tree is not clean; automatic restart would fail closed"
  echo "guard: repository clean at $(git rev-parse --short HEAD)"
}

# Refuse to start beside an existing engine.
guard_assert_no_duplicate() {
  ! pgrep -f "[Pp]ython(3)?.*(-m )?app\.main" >/dev/null 2>&1 || guard_die "a bot process is already running; nothing was stopped"
  echo "guard: no duplicate engine process"
}

guard_assert_venv() {
  [ -x ".venv/bin/python" ] || guard_die ".venv/bin/python is unavailable"
  echo "guard: venv OK ($(.venv/bin/python --version 2>&1))"
}

# Disk headroom: the audit noted logs/ and reports/ at ~1.1 G each.
guard_assert_disk() {
  local avail_kb; avail_kb=$(df -k . | tail -1 | awk '{print $4}')
  local min_kb=$(( 2 * 1024 * 1024 ))   # 2 GiB floor
  [ "$avail_kb" -ge "$min_kb" ] || guard_die "insufficient disk: $(( avail_kb / 1024 )) MiB free, need >= 2048 MiB"
  echo "guard: disk OK ($(( avail_kb / 1024 / 1024 )) GiB free)"
}

# Emit an auditable record of what is about to start.
guard_log_start() {
  local mode="$1" envfile="$2"
  mkdir -p logs
  printf '%s | LAUNCH | mode=%s | env=%s | commit=%s | operator=%s | host=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$mode" "$envfile" \
    "$(git rev-parse --short HEAD)" "${USER:-unknown}" "$(hostname)" >> logs/runtime.log
}

# Sleep-safety pre-flight (R2). Root cause established 2026-07-28:
#   * `caffeinate -s` is "valid only when system is running on AC power" (man page)
#   * `caffeinate -i` blocks idle sleep only; clamshell (lid-close) sleep is NOT
#     blocked -- already documented in scripts/lib/power_assertion.sh:14-17
#   * this host has `sleep 1` (1-minute idle) on BOTH AC and Battery
# So on battery, or with the lid closed, the power assertion cannot keep the host
# awake. That is what suspended the engine for 22.19 h on 2026-07-26.
#
# This guard does not fix it -- the fix needs sudo (owner action). It makes the
# condition VISIBLE at launch instead of silent.
#
# Default is WARN, not abort: defaulting to abort on a battery-powered laptop
# would silently break every automatic restart, which is the same class of
# failure this whole effort exists to remove. Production should set
# CGC_REQUIRE_AC=1 to turn the warning into a hard gate.
guard_assert_sleep_safe() {
  local on_battery=0 prevent_system=0
  pmset -g batt 2>/dev/null | grep -q "Battery Power" && on_battery=1
  prevent_system=$(pmset -g assertions 2>/dev/null | awk '/PreventSystemSleep/{print $2; exit}')
  prevent_system=${prevent_system:-0}
  local idle_sleep; idle_sleep=$(pmset -g custom 2>/dev/null | awk '/^ *sleep /{print $2; exit}')

  echo "guard: power state | on_battery=$on_battery | PreventSystemSleep=$prevent_system | idle_sleep=${idle_sleep:-?}min"
  if [ "$on_battery" -eq 1 ]; then
    echo "guard: WARNING R2 -- host is on BATTERY. 'caffeinate -s' is inert on battery," >&2
    echo "       so the engine can be suspended without warning (22.19 h on 2026-07-26)." >&2
    echo "       Owner fix: connect AC power, keep the lid open, and/or run:" >&2
    echo "         sudo pmset -a sleep 0 disksleep 0" >&2
    if [ "${CGC_REQUIRE_AC:-0}" = "1" ]; then
      guard_die "CGC_REQUIRE_AC=1 and host is on battery; refusing to start unattended"
    fi
    echo "guard: proceeding on battery (set CGC_REQUIRE_AC=1 to make this a hard gate)"
  fi
  if [ "${idle_sleep:-1}" != "0" ]; then
    echo "guard: NOTE idle sleep is ${idle_sleep}min; lid-close sleep is never blocked by caffeinate" >&2
  fi
}
