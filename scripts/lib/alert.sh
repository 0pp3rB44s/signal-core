#!/bin/bash
# Off-host alerting (RC2 Phase 5). The audit showed local-only monitoring is
# insufficient: the monitor died silently and nobody learned of it for 22.7 h.
#
# Provider is selected by ALERT_PROVIDER. Credentials come from the environment
# or state/alerting.env (gitignored) and are NEVER logged. If no provider is
# configured, alerts still land in logs/alerts.log so nothing is lost silently -
# but that is a DEGRADED state and send_alert says so.
#
# Supported: telegram | discord | email(mailto via /usr/bin/mail) | none
set -uo pipefail
ALERT_LOG="logs/alerts.log"
[ -f state/alerting.env ] && { set -a; . state/alerting.env; set +a; }

# send_alert <severity> <event> <message>
send_alert() {
  local sev="$1" event="$2" msg="$3"
  local host ts line
  host="$(hostname)"; ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  line="[$sev] $event | $msg | host=$host | commit=$(git rev-parse --short HEAD 2>/dev/null || echo '?') | $ts"
  mkdir -p logs; echo "$line" >> "$ALERT_LOG"

  case "${ALERT_PROVIDER:-none}" in
    telegram)
      [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ] || { echo "alert: telegram not configured" >&2; return 2; }
      curl -sS -m 15 -o /dev/null -X POST \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
        --data-urlencode "text=$line" && echo "alert: sent via telegram" ;;
    discord)
      [ -n "${DISCORD_WEBHOOK_URL:-}" ] || { echo "alert: discord not configured" >&2; return 2; }
      curl -sS -m 15 -o /dev/null -H 'Content-Type: application/json' \
        -X POST -d "$(printf '{"content":"%s"}' "${line//\"/\\\"}")" \
        "$DISCORD_WEBHOOK_URL" && echo "alert: sent via discord" ;;
    email)
      [ -n "${ALERT_EMAIL_TO:-}" ] || { echo "alert: email not configured" >&2; return 2; }
      printf '%s\n' "$line" | /usr/bin/mail -s "[cgc][$sev] $event" "$ALERT_EMAIL_TO" && echo "alert: sent via email" ;;
    none|"")
      echo "alert: NO PROVIDER CONFIGURED - logged locally only (DEGRADED)" >&2; return 3 ;;
    *)
      echo "alert: unknown ALERT_PROVIDER='${ALERT_PROVIDER}'" >&2; return 4 ;;
  esac
}
