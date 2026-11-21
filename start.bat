@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo   SYNAPSE TW 全自動台股交易系統 一鍵啟動 (Windows)
echo ============================================================

where python >nul 2>nul
if errorlevel 1 (
  echo [錯誤] 找不到 Python 3，請先安裝 Python 3.10+ 並勾選 Add to PATH。
  echo 或直接至 GitHub Releases 下載免安裝 .exe 執行檔！
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] 建立 Python 虛擬環境 (.venv)...
  python -m venv .venv
)

echo [2/3] 檢查並安裝必要套件...
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt

if not exist ".env" (
  if exist ".env.example" (
    copy /Y ".env.example" ".env" >nul
    echo [提示] 已自動建立 .env 預設設定檔（預設模擬盤 SIM 模式）。
  )
)

echo [3/3] 啟動 SYNAPSE TW 系統...
".venv\Scripts\python.exe" launcher.py %*
pause
