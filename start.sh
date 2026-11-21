#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo "============================================================"
echo "  SYNAPSE TW · 全自動台股交易系統 一鍵啟動 (Linux / macOS)"
echo "============================================================"

if [ ! -d ".venv" ]; then
  echo "[1/3] 建立 Python 虛擬環境 (.venv)..."
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "[2/3] 檢查並安裝必要套件..."
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

if [ ! -f ".env" ] && [ -f ".env.example" ]; then
  cp .env.example .env
  echo "[提示] 已自動建立 .env 預設設定檔（預設模擬盤 SIM 模式）。"
fi

echo "[3/3] 啟動 SYNAPSE TW 系統..."
exec python launcher.py "$@"
