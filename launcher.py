"""
SYNAPSE TW — 一鍵啟動與獨立執行檔入口 (launcher.py)
支援：
- 直接以 Python 執行：`python launcher.py`
- 經由 PyInstaller 打包為單一免安裝執行檔（Windows .exe / macOS / Linux）直接雙擊啟動
- 自動載入同目錄 .env、啟動 FastAPI 伺服器並自動開啟瀏覽器
"""

from __future__ import annotations

import argparse
import os
import socket
import threading
import time
import webbrowser

import uvicorn

from server import app


def find_available_port(preferred: int) -> int:
    for port in (preferred, preferred + 1, preferred + 2, 8080, 8888):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return preferred


def open_browser_later(url: str, delay: float = 1.4) -> None:
    def _worker() -> None:
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=_worker, daemon=True).start()


def main() -> None:
    parser = argparse.ArgumentParser(description="SYNAPSE TW 全自動台股交易系統啟動器")
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"), help="監聽位址（預設 127.0.0.1，雲端可設 0.0.0.0）")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")), help="監聽埠號（預設 8000）")
    parser.add_argument("--no-browser", action="store_true", help="啟動時不自動開啟瀏覽器")
    args = parser.parse_args()

    port = find_available_port(args.port) if args.host in ("127.0.0.1", "localhost") else args.port
    display_host = "127.0.0.1" if args.host == "0.0.0.0" else args.host
    url = f"http://{display_host}:{port}"

    print("=" * 64)
    print("  SYNAPSE TW · 全自動台股交易系統 v3.3.0")
    print(f"  介面網址：{url}")
    print(f"  健康檢查：{url}/api/v1/health")
    print("  按 Ctrl+C 即可安全停止系統")
    print("=" * 64)

    if not args.no_browser:
        open_browser_later(url)

    uvicorn.run(app, host=args.host, port=port, log_level="info")


if __name__ == "__main__":
    main()
