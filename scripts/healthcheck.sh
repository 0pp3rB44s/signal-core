#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

mkdir -p logs state

BOT_STATUS="stopped"
DASH_STATUS="stopped"
PORT_STATUS="closed"
HTTP_STATUS="down"
PY_STATUS="missing"
VENV_STATUS="missing"
ENV_STATUS="missing"
APP_ENV_VALUE="unknown"
APP_MODE_VALUE="unknown"
EXECUTION_MODE_VALUE="unknown"

if command -v python3 >/dev/null 2>&1; then
  PY_STATUS="$(python3 --version 2>&1)"
fi

if [ -d ".venv" ]; then
  VENV_STATUS="present"
fi

if [ -f ".env" ]; then
  ENV_STATUS="present"
  APP_ENV_VALUE="$(grep -E '^APP_ENV=' .env | tail -1 | cut -d '=' -f2- || echo 'unknown')"
  APP_MODE_VALUE="$(grep -E '^APP_MODE=' .env | tail -1 | cut -d '=' -f2- || echo 'unknown')"
  EXECUTION_MODE_VALUE="$(grep -E '^EXECUTION_MODE=' .env | tail -1 | cut -d '=' -f2- || echo 'unknown')"
fi

if [ -f state/bot.pid ] && ps -p "$(cat state/bot.pid)" > /dev/null 2>&1; then
  BOT_STATUS="running"
fi

if [ -f state/dashboard.pid ] && ps -p "$(cat state/dashboard.pid)" > /dev/null 2>&1; then
  DASH_STATUS="running"
fi

DASHBOARD_PORT="${DASHBOARD_PORT:-8501}"
if [ -f ".env" ]; then
  ENV_PORT="$(grep -E '^DASHBOARD_PORT=' .env | tail -1 | cut -d '=' -f2- || true)"
  if [ -n "$ENV_PORT" ]; then
    DASHBOARD_PORT="$ENV_PORT"
  fi
fi

if lsof -i :"$DASHBOARD_PORT" > /dev/null 2>&1; then
  PORT_STATUS="open"
fi

if curl -s "http://127.0.0.1:$DASHBOARD_PORT" > /dev/null 2>&1; then
  HTTP_STATUS="up"
fi

HEALTH_STATUS="OK"
if [ "$BOT_STATUS" != "running" ] || [ "$HTTP_STATUS" != "up" ]; then
  HEALTH_STATUS="ATTENTION_REQUIRED"
fi

PROTECTION_STATUS="OK"
DESYNC_STATUS="OK"

if tail -500 logs/agent.log 2>/dev/null | grep -Ei "UNPROTECTED|TP_PROTECTION_VERIFY_FAILED|VERIFY_STOP_LOSS_FAILED|ENTRY_PROTECTION_VERIFY_FAILED|FAIL_SAFE_CLOSE_FAILED" >/dev/null 2>&1; then
  PROTECTION_STATUS="ATTENTION_REQUIRED"
  HEALTH_STATUS="ATTENTION_REQUIRED"
fi

if tail -500 logs/agent.log 2>/dev/null | grep -Ei "STATE_MISMATCH|POSITION_SYNC_UNCERTAIN|LOCAL_OPEN_NOT_ON_EXCHANGE_SYNCED|RESIDUAL_POSITION_DETECTED|EXCHANGE_CLOSED_TPSL_CLEANUP_FAILED" >/dev/null 2>&1; then
  DESYNC_STATUS="ATTENTION_REQUIRED"
  HEALTH_STATUS="ATTENTION_REQUIRED"
fi

echo "project: $PROJECT_DIR"
echo "python: $PY_STATUS"
echo "venv: $VENV_STATUS"
echo "env: $ENV_STATUS"
echo "app env: $APP_ENV_VALUE"
echo "app mode: $APP_MODE_VALUE"
echo "execution mode: $EXECUTION_MODE_VALUE"
echo "bot: $BOT_STATUS"
echo "dashboard pid: $DASH_STATUS"
echo "dashboard port $DASHBOARD_PORT: $PORT_STATUS"
echo "dashboard http: $HTTP_STATUS"
echo "health status: $HEALTH_STATUS"
echo "protection status: $PROTECTION_STATUS"
echo "desync status: $DESYNC_STATUS"

echo ""
echo "--- runtime lifecycle ---"
tail -20 logs/runtime.log 2>/dev/null || echo "no runtime lifecycle log yet"

echo ""
echo "--- recent critical bot log ---"
tail -300 logs/agent.log 2>/dev/null | grep -Ei "ERROR|CRITICAL|Traceback|FAIL_SAFE|UNPROTECTED|VERIFY_FAILED|429" || echo "no recent critical agent events"

echo ""
echo "--- recent protection/desync markers ---"
tail -500 logs/agent.log 2>/dev/null | grep -Ei "UNPROTECTED|TP_PROTECTION|VERIFY_STOP_LOSS|ENTRY_PROTECTION|STATE_MISMATCH|POSITION_SYNC_UNCERTAIN|LOCAL_OPEN_NOT_ON_EXCHANGE_SYNCED|RESIDUAL_POSITION|EXCHANGE_CLOSED_TPSL" || echo "no recent protection/desync markers"

echo ""
echo "--- bot out ---"
tail -10 logs/bot.out 2>/dev/null || echo "no bot log yet"

echo ""
echo "--- dashboard out ---"
tail -10 logs/dashboard.out 2>/dev/null || echo "no dashboard log yet"
