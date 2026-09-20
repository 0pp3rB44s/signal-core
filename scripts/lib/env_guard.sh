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

# Production leverage ceiling for MicroFlow. MUST equal
# MICROFLOW_MAX_ALLOWED_LEVERAGE in app/config.py -- this guard runs before
# Python loads, so the two are separate implementations of one policy and a
# mismatch means LIVE either refuses an approved config or accepts an
# unapproved one. tests/test_launch_guard_leverage.py fails CI if they diverge.
# Guarded assignment: this file is sourced by several launchers and a bare
# `readonly` re-assignment would abort the second source.
: "${GUARD_MICROFLOW_MAX_LEVERAGE:=10}"

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

# --- LAYER 2 content binding: stale-authorization fix, 2026-09-18 ------------
# Reproduced finding: the OLD Layer 2 was `[ -f "$AUTH_FILE" ]` -- existence
# only. state/LIVE_PILOT_AUTHORISATION on production is, right now, dated for
# commit aa42c3aac52ef67d6d20efac2901f22581a60877 while production HEAD is
# 4d2fb7182afc49687cfa27cde3fcc7c4783f22ae -- a DIFFERENT commit -- and the old
# check would still pass. An authorization must be bound to the exact approved
# deployment identity, not merely be present. This function REPLACES the bare
# existence check; scripts/launch_live.sh now calls it directly at LAYER 2
# (2026-09-19, reproduced against this exact production file first, then
# wired -- see tests/test_launch_live_ceremony_wiring.py, which drives the
# real ceremony script rather than only this function in isolation).
# NOTHING here creates, modifies, or invalidates any real authorization file.

# Canonical, deterministic hash of the risk-relevant configuration actually
# about to run. Reuses values this guard already validates elsewhere (LIVE
# mode invariants, pilot limits) rather than inventing a second definition of
# "risk configuration" -- if one of these values changes, the deployment's
# actual risk posture has changed, and a stale authorization must not cover it.
guard_compute_risk_config_hash() {
  printf '%s|%s|%s|%s|%s|%s|%s|%s|%s\n' \
    "${MAX_LEVERAGE:-}" "${MAX_OPEN_POSITIONS:-}" "${EXECUTION_MAX_PER_CYCLE:-}" \
    "${PRODUCTION_SYMBOL_ALLOWLIST:-}" "${ENABLED_STRATEGIES:-}" \
    "${HARD_DAILY_STOP_PCT:-}" "${WEEKLY_FREEZE_LOSS_PCT:-}" \
    "${ADAPTIVE_TREND_LIVE_ENTRY_ENABLED:-}" "${MICROFLOW_SCALPER_ENABLED:-}" \
    | shasum -a 256 | awk '{print $1}'
}

# Hash of the ACTUALLY-INSTALLED launchd supervisor definition, when present
# (it lives outside the git tree, at ~/Library/LaunchAgents/, and can drift
# from the repo's own template without the repo itself going dirty). Falls
# back to the in-repo template, then to a fixed sentinel, so the field is
# always defined and always comparable -- never silently skipped.
guard_compute_runtime_hash() {
  local installed="${HOME}/Library/LaunchAgents/com.cgc.live.plist"
  if [ -f "$installed" ]; then
    shasum -a 256 "$installed" | awk '{print $1}'
    return 0
  fi
  local template="deploy/launchd/com.cgc.live.plist.template"
  if [ -f "$template" ]; then
    shasum -a 256 "$template" | awk '{print $1}'
    return 0
  fi
  echo "N/A"
}

# Non-secret fingerprint of which account/environment this run targets. Never
# includes the raw API key/secret/passphrase -- a SHA-256 of the key plus the
# declared environment name is enough to detect "this authorization was for a
# different account or environment" without persisting or logging the
# credential itself.
guard_compute_account_identity() {
  local key_fp="N/A"
  if [ -n "${BITGET_API_KEY:-}" ]; then
    key_fp="$(printf '%s' "${BITGET_API_KEY}" | shasum -a 256 | awk '{print substr($1,1,16)}')"
  fi
  printf '%s:%s\n' "${APP_ENV:-unset}" "$key_fp"
}

# Replaces the old bare `[ -f "$AUTH_FILE" ]` check. Requires every binding
# field to be present AND to match the CURRENT deployment exactly. Any
# mismatch, or any missing field (an old-format file, like production's
# current one, has none of these fields), fails closed -- same guard_die
# fail-fast contract as every other layer in this file.
guard_assert_authorization_binding() {
  local auth_file="${1:-state/LIVE_PILOT_AUTHORISATION}"
  [ -f "$auth_file" ] || guard_die \
    "LAYER 2: missing $auth_file — owner authorisation required. This file must be created by the owner, dated and signed. It is not created by any script."

  local auth_commit auth_risk_hash auth_runtime_hash auth_account auth_at
  auth_commit="$(awk -F= '$1=="commit"{print $2; exit}' "$auth_file")"
  auth_risk_hash="$(awk -F= '$1=="risk_config_hash"{print $2; exit}' "$auth_file")"
  auth_runtime_hash="$(awk -F= '$1=="runtime_hash"{print $2; exit}' "$auth_file")"
  auth_account="$(awk -F= '$1=="account_environment"{print $2; exit}' "$auth_file")"
  auth_at="$(awk -F= '$1=="authorized_at"{print $2; exit}' "$auth_file")"

  [ -n "$auth_commit" ] || guard_die \
    "LAYER 2: $auth_file has no commit= field (old-format or malformed authorization; owner must issue a fresh one bound to the current deployment identity)"
  [ -n "$auth_risk_hash" ] || guard_die \
    "LAYER 2: $auth_file has no risk_config_hash= field (old-format or malformed authorization)"
  [ -n "$auth_account" ] || guard_die \
    "LAYER 2: $auth_file has no account_environment= field (old-format or malformed authorization)"
  [ -n "$auth_at" ] || guard_die \
    "LAYER 2: $auth_file has no authorized_at= field (old-format or malformed authorization)"
  # runtime_hash may legitimately be the literal string N/A on both sides
  # (no launchd definition installed or templated anywhere); still required
  # to be PRESENT as a field, so a truncated file cannot silently pass.
  [ -n "$auth_runtime_hash" ] || guard_die \
    "LAYER 2: $auth_file has no runtime_hash= field (old-format or malformed authorization)"

  local current_commit; current_commit="$(git rev-parse HEAD)"
  [ "$auth_commit" = "$current_commit" ] || guard_die \
"LAYER 2: authorization is bound to commit ${auth_commit}, current HEAD is ${current_commit}.
       A stale authorization must never be reused for a different commit — the owner
       must issue a fresh authorization for THIS exact commit."

  local current_risk_hash; current_risk_hash="$(guard_compute_risk_config_hash)"
  [ "$auth_risk_hash" = "$current_risk_hash" ] || guard_die \
"LAYER 2: authorization risk_config_hash (${auth_risk_hash}) does not match the current
       risk configuration (${current_risk_hash}). Risk-relevant settings changed since
       authorization was issued; a fresh authorization is required."

  local current_runtime_hash; current_runtime_hash="$(guard_compute_runtime_hash)"
  [ "$auth_runtime_hash" = "$current_runtime_hash" ] || guard_die \
"LAYER 2: authorization runtime_hash (${auth_runtime_hash}) does not match the current
       installed/templated supervisor definition (${current_runtime_hash}). The
       deployment/supervisor definition changed since authorization; a fresh
       authorization is required."

  local current_account; current_account="$(guard_compute_account_identity)"
  [ "$auth_account" = "$current_account" ] || guard_die \
"LAYER 2: authorization account_environment does not match the current environment
       and credentials on file. A stale authorization for a different account or
       environment must never be reused."

  echo "layer 2: authorisation content-bound and verified (commit=$auth_commit, authorized_at=$auth_at)"
}

# --- start_bot.sh direct-live bypass hardening (2026-09-20) -------------------
# scripts/start_bot.sh starts app.main with none of the four ceremony layers. It is
# the strict forward-paper / observe launcher, so it must REFUSE whenever the
# environment it would hand to the engine is live-capable -- regardless of whether that
# came from `.env` or from variables already exported into the operator's shell (e.g. a
# sourced `.env.live`). Resolution follows pydantic-settings: a real OS environment
# variable (any case) beats the .env file, and the last assignment in the file wins.
guard_resolve_setting() {
  local name="$1" envfile="${2:-.env}" out
  out="$(env | awk -v n="$name" 'BEGIN{FS="="} toupper($1)==n {print "SET:" substr($0, index($0,"=")+1); exit}')"
  if [ -n "$out" ]; then printf '%s' "${out#SET:}"; return 0; fi
  [ -f "$envfile" ] || return 0
  awk -v n="$name" '
    { line=$0; sub(/^[ \t]*export[ \t]+/, "", line) }
    line ~ /^[ \t]*#/ { next }
    { i=index(line,"="); if (i==0) next
      k=substr(line,1,i-1); gsub(/[ \t]/,"",k)
      if (toupper(k)==n) { found=1; last=substr(line,i+1) } }
    END { if (found) { v=last; sub(/^[ \t]+/,"",v)
          if (v ~ /^"/) { sub(/^"/,"",v); sub(/".*$/,"",v) }
          else if (v ~ /^\047/) { sub(/^\047/,"",v); sub(/\047.*$/,"",v) }
          else { sub(/[ \t]+#.*$/,"",v); sub(/[ \t\r]+$/,"",v) }
          printf "%s", v } }' "$envfile"
}

guard_assert_not_live_capable_env() {
  local envfile="${1:-.env}" mode enabled reasons="" name
  mode="$(guard_resolve_setting EXECUTION_MODE "$envfile" | tr '[:lower:]' '[:upper:]')"
  enabled="$(guard_resolve_setting EXECUTION_ENABLED "$envfile" | tr '[:upper:]' '[:lower:]')"
  [ "$mode" = "LIVE" ] && reasons="${reasons} EXECUTION_MODE=LIVE"
  case "$enabled" in true|1|yes|y|on) reasons="${reasons} EXECUTION_ENABLED=true" ;; esac
  for name in BITGET_API_KEY BITGET_API_SECRET BITGET_API_PASSPHRASE; do
    [ -n "$(guard_resolve_setting "$name" "$envfile")" ] && reasons="${reasons} ${name}(non-blank)"
  done
  [ -z "$reasons" ] || guard_die \
"start_bot.sh is the forward-paper/observe launcher and refuses a live-capable environment:${reasons}
       (values are never printed). This can come from ${envfile} OR from variables already
       exported into this shell. Live trading starts ONLY through scripts/launch_live.sh
       (four-layer ceremony); nothing was started, nothing was stopped."
}

# start_bot.sh kills any "app.main" process to prevent duplicates. That must never be a running
# LIVE engine: it would be SIGKILLed with no graceful shutdown while positions may be open.
guard_assert_no_live_engine_running() {
  local state="${1:-state/live_runtime.state}" mode pid
  [ -f "$state" ] || return 0
  mode="$(awk -F= '$1=="mode"{print $2; exit}' "$state")"
  pid="$(awk -F= '$1=="pid"{print $2; exit}' "$state")"
  [ "$mode" = "LIVE" ] || return 0
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || return 0
  guard_die "a LIVE engine (pid ${pid}) is running per ${state}; start_bot.sh will not kill it. Use scripts/stop_all.sh deliberately."
}

# Load an env file into the environment, ignoring comments and blanks.
guard_load_env() {
  local f="$1"
  [ -f "$f" ] || guard_die "environment file not found: $f"
  # shellcheck disable=SC2046
  set -a; . "$f"; set +a
  echo "config: loaded $f"
}

# Canonical LIVE market scope.  PRODUCTION_SYMBOL_ALLOWLIST is the only owner-
# controlled source; legacy WATCHLIST / EXECUTION_CONFIRM_SYMBOLS / MAX_SYMBOLS
# values are derived in-memory and are never independent allowlists.
guard_apply_canonical_symbol_allowlist() {
  local raw="${PRODUCTION_SYMBOL_ALLOWLIST:-}" symbol normalized="" count=0
  [ -n "$raw" ] || guard_die \
    "LIVE requires owner-confirmed PRODUCTION_SYMBOL_ALLOWLIST; broad defaults are forbidden"

  local IFS=',' seen=','
  for symbol in $raw; do
    symbol="$(printf '%s' "$symbol" | tr '[:lower:]' '[:upper:]' | tr -d '[:space:]')"
    [ -n "$symbol" ] || continue
    [[ "$symbol" =~ ^[A-Z0-9]{2,24}USDT$ ]] || guard_die \
      "invalid symbol in PRODUCTION_SYMBOL_ALLOWLIST: '$symbol'"
    case "$seen" in
      *",${symbol},"*) guard_die "duplicate symbol in PRODUCTION_SYMBOL_ALLOWLIST: '$symbol'" ;;
    esac
    seen="${seen}${symbol},"
    normalized="${normalized}${normalized:+,}${symbol}"
    count=$((count + 1))
  done

  [ "$count" -gt 0 ] || guard_die "PRODUCTION_SYMBOL_ALLOWLIST normalised to an empty list"
  export PRODUCTION_SYMBOL_ALLOWLIST="$normalized"
  export WATCHLIST="$normalized"
  export EXECUTION_CONFIRM_SYMBOLS="$normalized"
  export MAX_SYMBOLS="$count"
  export ALLOW_AUTO_WATCHLIST_REFRESH=false
  echo "guard: canonical production allowlist OK (count=$count; symbols=$normalized)"
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
  if [ "${APP_ENV:-}" = "production" ]; then
    [ "${STRATEGY_ISOLATION_ENABLED:-}" = "true" ] || guard_die "production LIVE requires strategy isolation"
    [ "${OLD_STRATEGIES_NEW_ENTRIES_ENABLED:-}" = "false" ] || guard_die "legacy new-entry paths must remain disabled"
    [ "${DYNAMIC_GRID_ENABLED:-}" = "false" ] || guard_die "dynamic grid must remain disabled"
    [ "${DYNAMIC_GRID_MODE:-}" = "OFF" ] || guard_die "dynamic grid mode must remain OFF"
    awk -v v="${MAX_LEVERAGE:-0}" -v m="$GUARD_MICROFLOW_MAX_LEVERAGE" 'BEGIN { exit !(v > 0 && v <= m) }' \
      || guard_die "MAX_LEVERAGE must be >0 and <=$GUARD_MICROFLOW_MAX_LEVERAGE"
    case "${ENABLED_STRATEGIES:-}" in
      microflow_scalper_v1)
        [ "${MICROFLOW_SCALPER_ENABLED:-}" = "true" ] || guard_die "production LIVE requires MICROFLOW_SCALPER_ENABLED=true"
        [ "${MICROFLOW_SYMBOLS:-}" = "${PRODUCTION_SYMBOL_ALLOWLIST:-}" ] || guard_die "MicroFlow universe must equal the canonical production allowlist"
        awk -v v="${MICROFLOW_LEVERAGE:-0}" -v m="$GUARD_MICROFLOW_MAX_LEVERAGE" 'BEGIN { exit !(v > 0 && v <= m) }' \
          || guard_die "MicroFlow leverage must be >0 and <=$GUARD_MICROFLOW_MAX_LEVERAGE"
        awk -v v="${MICROFLOW_MAX_SLIPPAGE_BPS:-0}" 'BEGIN { exit !(v > 0 && v <= 1) }' || guard_die "MicroFlow slippage cap must be >0 and <=1 bps"
        ;;
      adaptive_trend_tsmom_v1)
        # Owner-gated: enabling this strategy in production LIVE also
        # requires its dedicated flag -- ENABLED_STRATEGIES alone is not
        # sufficient, matching the same coupling enforced one layer down
        # in ExecutionService.execute()'s HYBRID SAFE MODE gate.
        [ "${ADAPTIVE_TREND_LIVE_ENTRY_ENABLED:-}" = "true" ] \
          || guard_die "production LIVE requires ADAPTIVE_TREND_LIVE_ENTRY_ENABLED=true for adaptive_trend_tsmom_v1"
        ;;
      *)
        guard_die "production LIVE enables only microflow_scalper_v1 or adaptive_trend_tsmom_v1"
        ;;
    esac
  fi
  echo "guard: LIVE invariants OK"
}

# Pilot exposure ceilings. Independent of mode; applies to both.
guard_assert_pilot_limits() {
  local max_sym="${MAX_SYMBOLS:-0}" max_pos="${MAX_OPEN_POSITIONS:-0}"
  local allow_count=0 symbol IFS=','
  for symbol in ${PRODUCTION_SYMBOL_ALLOWLIST:-}; do [ -n "$symbol" ] && allow_count=$((allow_count + 1)); done
  [ "$allow_count" -gt 0 ] || guard_die "canonical production allowlist is unavailable"
  [ "$max_sym" -eq "$allow_count" ] 2>/dev/null || guard_die \
    "MAX_SYMBOLS must equal canonical allowlist size $allow_count (got '$max_sym')"
  # Owner-approved two-position portfolio. Pinned to exactly 2 rather than
  # "at most 2": 0, 1 and 3 all fail closed, so a mistyped or unset value can
  # never widen exposure. Mirrors LIVE_MAX_OPEN_POSITIONS in app/config.py.
  [ "$max_pos" -eq 2 ] 2>/dev/null || guard_die "MAX_OPEN_POSITIONS must equal 2 (got '$max_pos')"
  [ "${EXECUTION_MAX_PER_CYCLE:-0}" -eq 2 ] 2>/dev/null || guard_die \
    "EXECUTION_MAX_PER_CYCLE must equal 2 (got '${EXECUTION_MAX_PER_CYCLE:-unset}')"
  [ "${ALLOW_AUTO_WATCHLIST_REFRESH:-true}" = "false" ] || guard_die "pilot requires ALLOW_AUTO_WATCHLIST_REFRESH=false"
  [ "${FORWARD_PAPER_SMOKE_STRATEGY_ENABLED:-false}" = "false" ] || guard_die "smoke harness must be disabled"
  echo "guard: portfolio limits OK (symbols=$max_sym, positions=$max_pos, executions_per_cycle=${EXECUTION_MAX_PER_CYCLE:-0})"
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
