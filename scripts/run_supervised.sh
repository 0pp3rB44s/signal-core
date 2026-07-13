#!/bin/bash
# Supervisor: herstart de bot automatisch na een crash, met backoff.
#
# Draai dit in een gewone terminal of tmux/screen-sessie (user-context, dus
# geen macOS TCC-problemen zoals bij launchd). Bewust GEEN launchd: zie
# scripts/com.cgc.tradingbot.plist.template voor waarom dat eerder faalde.
#
#   tmux new -s cgcbot 'bash scripts/run_supervised.sh'
#
# Gedrag:
# - start de bot via de bestaande start-route (zelfde env-checks)
# - crasht de bot (non-zero exit of proces weg), dan herstart met backoff
# - na MAX_RAPID_FAILURES snelle crashes op rij stopt de supervisor
#   (fail-closed: een bot die direct blijft crashen moet een mens zien)
# - nette stop: scripts/stop_all.sh zet state/supervisor.stop neer, of
#   verwijder handmatig state/bot.pid en maak state/supervisor.stop aan

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

STOP_FLAG="state/supervisor.stop"
MAX_RAPID_FAILURES=5
RAPID_WINDOW_SECONDS=300
BACKOFF_SECONDS=15
MAX_BACKOFF_SECONDS=300

rm -f "$STOP_FLAG"
rapid_failures=0
backoff=$BACKOFF_SECONDS

echo "$(date '+%Y-%m-%d %H:%M:%S') | SUPERVISOR_START" | tee -a logs/runtime.log

while true; do
  if [ -f "$STOP_FLAG" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') | SUPERVISOR_STOP | stop flag aanwezig" | tee -a logs/runtime.log
    exit 0
  fi

  start_ts=$(date +%s)
  bash scripts/start_bot.sh supervised_start

  BOT_PID="$(cat state/bot.pid 2>/dev/null || true)"
  if [ -z "$BOT_PID" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') | SUPERVISOR_START_FAILED" | tee -a logs/runtime.log
  else
    # wacht tot het botproces eindigt
    while ps -p "$BOT_PID" >/dev/null 2>&1; do
      if [ -f "$STOP_FLAG" ]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') | SUPERVISOR_STOP_REQUESTED | bot blijft draaien (PID $BOT_PID)" | tee -a logs/runtime.log
        exit 0
      fi
      sleep 10
    done
  fi

  end_ts=$(date +%s)
  uptime=$((end_ts - start_ts))
  echo "$(date '+%Y-%m-%d %H:%M:%S') | SUPERVISOR_BOT_EXITED | uptime_sec=$uptime" | tee -a logs/runtime.log

  if [ "$uptime" -lt "$RAPID_WINDOW_SECONDS" ]; then
    rapid_failures=$((rapid_failures + 1))
    backoff=$((backoff * 2))
    [ "$backoff" -gt "$MAX_BACKOFF_SECONDS" ] && backoff=$MAX_BACKOFF_SECONDS
  else
    rapid_failures=0
    backoff=$BACKOFF_SECONDS
  fi

  if [ "$rapid_failures" -ge "$MAX_RAPID_FAILURES" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') | SUPERVISOR_GIVING_UP | $rapid_failures snelle crashes op rij" | tee -a logs/runtime.log
    osascript -e 'display notification "Supervisor gestopt na herhaalde crashes. Handmatige actie nodig." with title "CGC bot supervisor" sound name "Basso"' 2>/dev/null || true
    exit 1
  fi

  echo "$(date '+%Y-%m-%d %H:%M:%S') | SUPERVISOR_RESTARTING | backoff=${backoff}s (failures=$rapid_failures)" | tee -a logs/runtime.log
  osascript -e 'display notification "Bot gecrasht - automatische herstart." with title "CGC bot supervisor"' 2>/dev/null || true
  sleep "$backoff"
done
