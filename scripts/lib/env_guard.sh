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
