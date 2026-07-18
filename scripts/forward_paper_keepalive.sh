#!/bin/bash
# Keepalive voor STRICT forward-paper-only. Herstart uitsluitend via
# scripts/start_forward_paper.sh (die zelf alle veiligheidscondities
# afdwingt: schone main, geen dubbele processen, credentials leeg, geen
# orders mogelijk). Start nooit een andere modus. Fail-closed bij
# herhaalde snelle crashes: een bot die blijft sterven moet een mens zien.
#
# Gebruik:
#   scripts/forward_paper_keepalive.sh          # één controle (cron-vriendelijk)
#   scripts/forward_paper_keepalive.sh --loop 120   # blijvend, elke 120 s (tmux)
# Stoppen van de loop: maak state/forward_paper_keepalive.stop aan.
set -uo pipefail

PROJECT_DIR="${CGC_PROJECT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$PROJECT_DIR"

STOP_FLAG="state/forward_paper_keepalive.stop"
HISTORY="state/forward_paper_keepalive.history"
LOG="logs/forward_paper_keepalive.log"
MAX_RESTARTS=3
WINDOW_SECONDS=1800
SCAN_INTERVAL="${SCAN_INTERVAL:-60}"
mkdir -p logs state

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$1" | tee -a "$LOG"; }

check_once() {
  [ -f "$STOP_FLAG" ] && { log "stopvlag aanwezig; keepalive doet niets"; return 0; }
  STATUS="$(bash scripts/check_forward_paper.sh 2>/dev/null | awk -F= '$1=="status" {print $2}' | tail -1)"
  case "$STATUS" in
    HEALTHY)
      return 0 ;;
    PROCESS_NOT_RUNNING|NOT_STARTED|"")
      NOW="$(date +%s)"
      RECENT=0
      if [ -f "$HISTORY" ]; then
        while read -r TS; do
          [ $(( NOW - TS )) -lt "$WINDOW_SECONDS" ] && RECENT=$((RECENT + 1))
        done < "$HISTORY"
      fi
      if [ "$RECENT" -ge "$MAX_RESTARTS" ]; then
        log "FAIL-CLOSED: $RECENT herstarts binnen ${WINDOW_SECONDS}s; menselijke controle vereist"
        return 1
      fi
      log "forward paper niet actief (status=${STATUS:-LEEG}); herstart via strict launcher"
      if ./scripts/start_forward_paper.sh "$SCAN_INTERVAL" >> "$LOG" 2>&1; then
        echo "$NOW" >> "$HISTORY"
        log "herstart gelukt"
      else
        log "herstart geweigerd door strict launcher (zie log); geen verdere actie"
        return 1
      fi ;;
    *)
      log "onverwachte status=$STATUS; geen automatische actie (fail-closed)"
      return 1 ;;
  esac
}

if [ "${1:-}" = "--loop" ]; then
  INTERVAL="${2:-120}"
  log "keepalive-loop gestart (interval ${INTERVAL}s)"
  while [ ! -f "$STOP_FLAG" ]; do
    check_once || true
    sleep "$INTERVAL"
  done
  log "keepalive-loop gestopt via stopvlag"
else
  check_once
fi
