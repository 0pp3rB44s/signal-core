#!/bin/bash
# Deploy the dashboard runtime. This script cannot modify the trading checkout.
#
# The dashboard and the trading engine used to share one working tree, which made
# every dashboard update a trading-code change on disk under a running engine.
# The dashboard now lives in its own checkout and reads production state from the
# trading runtime via CGC_PROD_ROOT.
#
# Hard guarantees, enforced below rather than documented:
#   * the deploy path may never equal or sit inside the trading runtime;
#   * nothing here starts, stops or signals the LIVE executor;
#   * only com.cgc.dashboard is reloaded;
#   * a dashboard that fails its healthcheck is rolled back automatically.
set -euo pipefail

TRADING_ROOT="${CGC_TRADING_ROOT:-$HOME/cgc/bitget_ai_agent_phase7}"
DASHBOARD_ROOT="${CGC_DASHBOARD_ROOT:-$HOME/cgc/dashboard_runtime}"
LABEL="com.cgc.dashboard"
PORT="${DASHBOARD_PORT:-8501}"
SHA="${1:-}"

die() { echo "ABORT: $*" >&2; exit 1; }
[ -n "$SHA" ] || die "usage: deploy_dashboard.sh <sha>"

# --- guard: never the trading checkout -------------------------------------
canon() { cd "$1" 2>/dev/null && pwd -P; }
T="$(canon "$TRADING_ROOT" || echo "$TRADING_ROOT")"
D="$(canon "$DASHBOARD_ROOT" || echo "$DASHBOARD_ROOT")"
[ "$D" != "$T" ] || die "dashboard path equals the trading checkout ($T)"
case "$D/" in "$T"/*) die "dashboard path is inside the trading checkout ($T)";; esac

[ -d "$T/state" ] || die "trading runtime has no state/ at $T"
[ -d "$D/.git" ] || die "dashboard runtime is not a git checkout at $D"

ENGINE_BEFORE="$(pgrep -f 'app\.main' | tr '\n' ' ')"
echo "engine before: [${ENGINE_BEFORE:-none}]"
PREV="$(git -C "$D" rev-parse HEAD)"
echo "dashboard $PREV -> $SHA"

git -C "$D" cat-file -e "${SHA}^{commit}" 2>/dev/null || git -C "$D" fetch -q origin
git -C "$D" cat-file -e "${SHA}^{commit}" 2>/dev/null || die "unknown commit $SHA"
git -C "$D" checkout -q --detach "$SHA"
[ "$(git -C "$D" rev-parse HEAD)" = "$(git -C "$D" rev-parse "$SHA")" ] || die "checkout verification failed"

UID_N="$(id -u)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$UID_N/$LABEL" 2>/dev/null || true
sleep 1
launchctl bootstrap "gui/$UID_N" "$PLIST"

ok=""
for _ in $(seq 1 30); do
  sleep 2
  code="$(curl -s -o /dev/null -w '%{http_code}' -m 8 "http://127.0.0.1:$PORT/login" || true)"
  [ "$code" = "200" ] && { ok="yes"; break; }
done

if [ -z "$ok" ]; then
  echo "healthcheck failed; rolling dashboard back to $PREV" >&2
  git -C "$D" checkout -q --detach "$PREV"
  launchctl bootout "gui/$UID_N/$LABEL" 2>/dev/null || true
  sleep 1; launchctl bootstrap "gui/$UID_N" "$PLIST"
  die "dashboard did not become healthy; rolled back"
fi

ENGINE_AFTER="$(pgrep -f 'app\.main' | tr '\n' ' ')"
echo "engine after:  [${ENGINE_AFTER:-none}]"
[ "$ENGINE_BEFORE" = "$ENGINE_AFTER" ] || die "engine PID changed — investigate immediately"
echo "OK: dashboard on $(git -C "$D" rev-parse --short HEAD), HTTP 200, engine untouched"
