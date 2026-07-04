#!/bin/bash
# Foreground entrypoint for launchd. Do not background/nohup here -
# launchd tracks this process's PID directly to detect crashes and
# apply KeepAlive restarts. See scripts/install_launchd.sh.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

if [ ! -d ".venv" ]; then
  echo "ERROR: .venv not found. Run scripts/bootstrap.sh first." >&2
  exit 1
fi

if [ ! -f ".env" ]; then
  echo "ERROR: .env not found. Copy .env.example to .env and fill in required values." >&2
  exit 1
fi

source .venv/bin/activate

mkdir -p logs state

echo "$(date '+%Y-%m-%d %H:%M:%S') | BOT_START | reason=launchd_supervised | env=$(grep -E '^APP_ENV=' .env | tail -1 | cut -d '=' -f2- || echo unknown) | mode=$(grep -E '^APP_MODE=' .env | tail -1 | cut -d '=' -f2- || echo unknown) | execution=$(grep -E '^EXECUTION_MODE=' .env | tail -1 | cut -d '=' -f2- || echo unknown)" >> logs/runtime.log

# $$ still refers to this shell's PID after exec below, so bot.pid stays
# accurate for scripts/healthcheck.sh and scripts/stop_all.sh.
echo $$ > state/bot.pid

exec python3 -u -m app.main
