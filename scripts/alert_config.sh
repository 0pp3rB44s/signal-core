#!/bin/bash
# Alerting configuration validator and test harness (Blocker 3).
#
#   scripts/alert_config.sh validate   -- report exactly what is missing
#   scripts/alert_config.sh test       -- attempt a REAL external delivery
#
# It never reports success it cannot prove: `test` only prints DELIVERED after a
# provider call returned success. With no credentials it exits non-zero and says
# precisely which variables are absent.
set -uo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"; cd "$PROJECT_DIR"
CONF="state/alerting.env"
. scripts/lib/alert.sh

need() { # need <var> ; prints MISSING/present without revealing the value
  local v="${!1:-}"
  if [ -z "$v" ]; then echo "    MISSING  $1"; return 1; fi
  echo "    present  $1 (${#v} chars)"; return 0
}

validate() {
  echo "=== ALERTING CONFIGURATION VALIDATION ==="
  echo "  config file: $CONF $( [ -f "$CONF" ] && echo '(present)' || echo '(ABSENT)')"
  [ -f "$CONF" ] || { echo "  template:    deploy/alerting.env.example"; }
  local provider="${ALERT_PROVIDER:-none}" missing=0
  echo "  ALERT_PROVIDER: $provider"
  case "$provider" in
    telegram) need TELEGRAM_BOT_TOKEN || missing=1; need TELEGRAM_CHAT_ID || missing=1 ;;
    discord)  need DISCORD_WEBHOOK_URL || missing=1 ;;
    email)    need ALERT_EMAIL_TO || missing=1
              [ -x /usr/bin/mail ] || { echo "    MISSING  /usr/bin/mail"; missing=1; } ;;
    none|"")  echo "    no provider selected -- alerting is LOCAL-ONLY (DEGRADED)"; missing=1 ;;
    *)        echo "    UNKNOWN provider '$provider' (expected telegram|discord|email)"; missing=1 ;;
  esac
  echo
  if [ "$missing" -eq 0 ]; then
    echo "  RESULT: configuration COMPLETE for '$provider'. Run: $0 test"
    return 0
  fi
  echo "  RESULT: configuration INCOMPLETE -- external alerting is NOT operational."
  echo "  OWNER ACTION:"
  echo "    1. cp deploy/alerting.env.example $CONF"
  echo "    2. set ALERT_PROVIDER and its credentials in $CONF"
  echo "    3. $0 validate   (expect COMPLETE)"
  echo "    4. $0 test       (expect DELIVERED)"
  echo "  No agent can perform step 2: it requires your own provider credentials."
  return 1
}

run_test() {
  echo "=== ALERTING DELIVERY TEST (real external send) ==="
  validate >/dev/null 2>&1 || { validate; echo; echo "  ABORT: refusing to attempt delivery with incomplete configuration."; return 1; }
  local out rc
  out="$(send_alert INFO ALERT_DELIVERY_TEST "end-to-end delivery test from $(hostname)" 2>&1)"; rc=$?
  echo "$out" | sed 's/^/  /'
  if [ "$rc" -eq 0 ]; then
    echo "  RESULT: DELIVERED via ${ALERT_PROVIDER}. External alerting is OPERATIONAL."
    printf '%s | ALERT_TEST | provider=%s | result=DELIVERED\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${ALERT_PROVIDER}" >> logs/runtime.log
    return 0
  fi
  echo "  RESULT: NOT DELIVERED (rc=$rc). External alerting is NOT operational."
  return "$rc"
}

case "${1:-validate}" in
  validate) validate ;;
  test)     run_test ;;
  *) echo "usage: $0 [validate|test]"; exit 2 ;;
esac
