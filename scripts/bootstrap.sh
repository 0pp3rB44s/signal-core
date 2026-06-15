#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

echo "=== Bitget AI Agent Bootstrap ==="

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not installed"
  exit 1
fi

PY_VERSION="$(python3 --version 2>&1)"
echo "python detected: $PY_VERSION"

if [ ! -d ".venv" ]; then
  echo "creating virtual environment..."
  python3 -m venv .venv
else
  echo ".venv already exists"
fi

source .venv/bin/activate

python -m pip install --upgrade pip wheel setuptools
pip install -r requirements.txt

mkdir -p logs state reports/backtests

if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "created .env from .env.example"
else
  echo ".env already exists"
fi

chmod +x scripts/*.sh

echo ""
echo "bootstrap completed"
echo ""
echo "next steps:"
echo "1. edit .env with Bitget API credentials"
echo "2. run: ./scripts/start_bot.sh"
echo "3. run: ./scripts/start_dashboard.sh"
