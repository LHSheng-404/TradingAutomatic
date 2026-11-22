#!/data/data/com.termux/files/usr/bin/bash
# ==============================================================================
# SYNAPSE TW — Android Termux 本機一鍵啟動腳本（零電腦直接於手機運行完整後端）
# 使用方式（於 Android Termux 執行）：
#   chmod +x mobile/termux-start.sh && ./mobile/termux-start.sh
# ==============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SCRIPT_DIR"

echo "================================================================"
echo "  SYNAPSE TW · Android Termux 手機本機執行啟動器"
echo "================================================================"

if ! command -v python3 >/dev/null 2>&1; then
  echo "[1/3] 安裝 Python 環境..."
  pkg update -y && pkg install -y python
fi

if ! python3 -c "import fastapi, uvicorn" >/dev/null 2>&1; then
  echo "[2/3] 安裝後端套件 (FastAPI / Uvicorn)..."
  pip install --upgrade pip
  pip install -r requirements.txt
fi

PORT="${PORT:-8000}"
URL="http://127.0.0.1:${PORT}"

echo "[3/3] 啟動 SYNAPSE TW 手機本機伺服器：${URL}"
if command -v termux-open-url >/dev/null 2>&1; then
  (sleep 2 && termux-open-url "$URL") &
fi

exec python3 launcher.py --host 0.0.0.0 --port "$PORT" --no-browser
