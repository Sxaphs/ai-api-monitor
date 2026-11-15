#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
export PYTHONIOENCODING=utf-8

if ! command -v python3 >/dev/null 2>&1; then
  echo "未找到 python3。请先安装 Python 3.11+：https://www.python.org/downloads/macos/"
  exit 1
fi

python3 -c 'import sys; assert sys.version_info >= (3, 11), "需要 Python 3.11+，当前为 " + sys.version'

if [[ ! -d .venv ]]; then
  echo "==> 创建虚拟环境"
  python3 -m venv .venv
fi

echo "==> 安装依赖"
.venv/bin/python -m pip install -r requirements.txt

if [[ ! -f .env ]]; then
  echo "==> 创建 .env；需要真实采集时，请稍后补全其中的密钥"
  cp .env.example .env
fi

fresh_db=0
if [[ ! -f data/monitor.db ]]; then
  fresh_db=1
fi

echo "==> 初始化数据库"
.venv/bin/python -m app.cli init-db

if [[ "$fresh_db" == 1 && -d imports/2026-10-12_2026-11-12 ]]; then
  echo "==> 首次运行，导入演示数据"
  .venv/bin/python -m app.cli import-history imports/2026-10-12_2026-11-12
fi

echo "==> 启动 http://127.0.0.1:8000/changes"
echo "    按 Ctrl+C 停止；请勿同时运行多个调度实例。"
(
  sleep 2
  open "http://127.0.0.1:8000/changes" >/dev/null 2>&1 || true
) &

exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
