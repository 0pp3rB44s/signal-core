#!/bin/bash
# LIVE launcher. Pinned to .env.live. Cannot start forward paper: the guard
# asserts FORWARD_PAPER_ONLY=false and this script never reads .env.forward.
#
# FOUR INDEPENDENT AUTHORISATION LAYERS must all pass. No single variable, and no
# single file, can start live trading. This script places no orders itself; it
# starts the engine, which then may.
set -uo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"; cd "$PROJECT_DIR"
. scripts/lib/env_guard.sh
. scripts/lib/power_assertion.sh

AUTH_FILE="state/LIVE_PILOT_AUTHORISATION"
CONFIRM_PHRASE="START LIVE PILOT"

echo "=== LIVE LAUNCH — AUTHORISATION REQUIRED ==="

# LAYER 1: every Critical risk resolved or explicitly accepted.
if grep -qE '^\- \*\*Status:\*\* OPEN' docs/RISK_REGISTER.md 2>/dev/null; then
  OPEN_CRIT=$(awk '/^## CRITICAL/,/^## HIGH/' docs/RISK_REGISTER.md | grep -c 'Status:\*\* OPEN' || echo 0)
  [ "$OPEN_CRIT" -eq 0 ] || guard_die "LAYER 1: $OPEN_CRIT Critical risk(s) still OPEN in docs/RISK_REGISTER.md"
fi
echo "layer 1: no open Critical risks"

# LAYER 2: owner-created authorisation token.
[ -f "$AUTH_FILE" ] || guard_die "LAYER 2: missing $AUTH_FILE — owner authorisation required. This file must be created by the owner, dated and signed. It is not created by any script."
echo "layer 2: authorisation token present ($AUTH_FILE)"

# LAYER 3: configuration file and invariants.
guard_assert_repo_clean
guard_assert_venv
guard_assert_no_duplicate
guard_assert_disk
guard_load_env ".env.live"
guard_assert_live_mode
guard_assert_pilot_limits
echo "layer 3: live configuration invariants OK"

# LAYER 4: interactive human confirmation.
echo
echo "About to start LIVE trading with REAL money."
echo "  commit  : $(git rev-parse --short HEAD)"
echo "  symbols : ${WATCHLIST:-?} (max ${MAX_SYMBOLS:-?})"
echo "  max pos : ${MAX_OPEN_POSITIONS:-?}   notional cap: ${EXECUTION_MAX_LIVE_NOTIONAL_PER_TRADE_USDT:-?} USDT"
printf 'Type exactly "%s" to proceed: ' "$CONFIRM_PHRASE"
read -r TYPED
[ "$TYPED" = "$CONFIRM_PHRASE" ] || guard_die "LAYER 4: confirmation phrase not matched; nothing was started"
echo "layer 4: operator confirmation accepted"

mkdir -p logs state data_store reports
BOT_PID="$(.venv/bin/python scripts/launch_detached.py --stdout logs/live.out -- .venv/bin/python -u -m app.main)"
echo "$BOT_PID" > state/bot.pid
{ echo "mode=LIVE"; echo "pid=$BOT_PID"; echo "started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)";
  echo "env_file=.env.live"; echo "commit=$(git rev-parse HEAD)"; echo "operator=${USER:-unknown}"; } > state/live_runtime.state
sleep 2
ps -p "$BOT_PID" >/dev/null 2>&1 || guard_die "engine failed to start; see logs/live.out"
hold_power_assertion "$BOT_PID" "live"
guard_log_start "LIVE" ".env.live"
echo "LIVE started (PID $BOT_PID)"
