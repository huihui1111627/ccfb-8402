#!/usr/bin/env bash
# 一键启动：mock 站点 :8100 + 控制台 :8000
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  python3 -m venv .venv
  . .venv/bin/activate
  pip install -q -r requirements.txt
else
  . .venv/bin/activate
fi

export MOCK_BASE_URL="${MOCK_BASE_URL:-http://127.0.0.1:8100}"
mkdir -p data/artifacts data/receipts

cleanup() { kill 0 2>/dev/null || true; }
trap cleanup EXIT INT TERM

uvicorn mock.server:app --host 127.0.0.1 --port 8100 --log-level warning &
uvicorn app.web:app --host 127.0.0.1 --port 8000 --log-level info &
echo
echo "控制台   http://127.0.0.1:8000"
echo "Mock站点 http://127.0.0.1:8100/mock/admin"
echo "短信网关 http://127.0.0.1:8100/mock/sms/outbox"
echo "Ctrl-C 结束"
wait
