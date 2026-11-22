# SYNAPSE TW · 全自動台股交易系統（TradingAutomatic）

![Release](https://img.shields.io/github/v/release/LHSheng-404/TradingAutomatic?color=2563eb&label=Release)
![License](https://img.shields.io/badge/License-MIT-0d9488)
![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab)
![FastAPI](https://img.shields.io/badge/Backend-FastAPI-009688)
![RWD](https://img.shields.io/badge/UI-RWD%20Zero--Overflow-0f172a)

**SYNAPSE TW** 是一套支援**雲端部署**與**單檔免安裝直接執行**的全自動台灣股市交易系統網頁版，整合 **雙向資料交流交握視覺化**、**TradingView 進階線圖與帳號串接**、**Yahoo Finance / FinMind 真實台股行情（嚴禁猜測）**、**六模型 AI（Groq 極速推理 · Ollama 地端模型 · Grok · Gemini · Claude · ChatGPT，附各模型 Token 即時測試）**、**下單實時偵測股價 ＋ 價量多 Agent 自動選模**、**10 大台股技術・籌碼・大盤強弱開關（策略版 v01）與 30 支 Pine 量化策略實驗室**、**LINE Bot & Telegram 單欄位 Token 加密自動推播**、**對帳單復盤**、**台股真實定投回測**，以及 **模擬下單（SIM）／真實下單（LIVE）安全切換**。

---

## 主頁與各畫面切換動態說明（GIF 實機錄製）

### 🎬 1. 主頁指揮台 → 「功能選單 ▾」七大模組彈窗切換全導覽
> 主頁保持極簡清爽；點擊頂部「**功能選單 ▾**」即可彈窗切換 **① 技術開關 (v01) · 量化策略實驗室 & 多 Agent**、**② AI 六模型 & Ollama**、**③ 對帳單交易復盤**、**④ 台股真實定投回測**、**⑤ LINE / TG 通知**、**⑥ 雲端部署與 Pine Script**、**⑦ 折數與帳號設定**，零跳轉不打斷看盤。

![主頁與七大功能彈窗切換動態演示](docs/screenshots/demo_main_navigation.gif)

| 畫面順序 | 切換入口 | 主要功能說明 |
| :--- | :--- | :--- |
| **主頁指揮台** | 預設首頁 | 自選股即時列、60fps 雙向 `SYN-ACK` 資料交握網路、TradingView / Yahoo / FinMind 真實線圖、下單執行票與六模型快速共識。 |
| **① 技術開關 (v01) & 量化實驗室** | `功能選單 ▾` → 彈窗頁籤 1 | 內建雙子分頁：① **v01 10 大開關**（技術＋FinMind 三大法人籌碼＋大盤 RS）與多 Agent 自動買賣；② **量化策略實驗室**（30 支 Alorse PineScript 策略即時台股回測、參數最佳化、多股批次、標的風險指標卡，並可一鍵將策略最新訊號連結至多 Agent 決策）。 |
| **② AI 六模型 & Ollama** | `功能選單 ▾` → 彈窗頁籤 2 | 設定 Groq 極速 API、地端 Ollama / LM Studio（一鍵偵測本機模型）及 Grok / Gemini / Claude / ChatGPT，各模型皆附即時 Token 測試按鈕與一鍵全測。 |
| **③ 對帳單交易復盤** | `功能選單 ▾` → 彈窗頁籤 3 | 匯入 PDF（含密碼）/ XLSX / CSV / JSON 對帳單，支援 2.8 折退佣計算、交易健康度評分與五維能力雷達圖。 |
| **④ 台股真實定投回測** | `功能選單 ▾` → 彈窗頁籤 4 | 多標的權重＋多階段每月定投設定，即時抓取 FinMind 真實歷史日 K 與除權息配股計算含息報酬率。 |
| **⑤ LINE / TG 通知中心** | `功能選單 ▾` → 彈窗頁籤 5 | LINE Bot 與 Telegram 單欄位 Token 加密自動綁定（免填 Chat ID）＋ 條列股票代號與中文名稱推播預覽。 |
| **⑥ 雲端部署與腳本** | `功能選單 ▾` → 彈窗頁籤 6 | 雲端 FastAPI 代理網址測試、FinMind Token 設定與 TradingView Pine Script v5 Webhook 腳本匯出。 |
| **⑦ 折數與帳號設定** | `功能選單 ▾` → 彈窗頁籤 7 | 全域手續費折數連動（預設 `2.8` 折，可修改，最低 `6.0` 折）與 TradingView 進階帳號／雲端 Layout 綁定。 |

---

### 🎬 2. 主頁雙向資料交握動畫 × 下單實時偵測多 Agent 預檢
> 即時呈現系統各節點之間的雙向封包交握；送出下單前自動即時抓取最新股價、核對滑價與 10 大開關評分，並依盤中量比與波動自動切換最適 AI 模型。

![主頁雙向交握與下單多 Agent 實時預檢演示](docs/screenshots/demo_handshake_order.gif)

---

### 🎬 3. Telegram Bot 僅填 Bot Token 加密自動綁定流程（免填 Chat ID）
> 僅輸入 `@BotFather` 提供之 `Bot Token` → 驗證有效後加密存檔並遮罩 → 發行一次性防搶先 `/start <setup_code>` → 輪詢 `getUpdates` 自動鎖定建立者 `owner_chat_id` 並令設定碼失效 → 一鍵發送 HTML 測試與自動交易推播。

![Telegram Bot 僅填 Token 自動綁定演示](docs/screenshots/demo_telegram_autobind.gif)

---

## 各模組高解析度靜態截圖（v3.3.0 實機渲染）

### 1. 總覽指揮台：雙向資料交流交握網路 × 選單彈窗架構
> 60fps 呈現 Yahoo Finance、FinMind v4、TradingView、台股訊號引擎、AI 六模型 & 多 Agent、LINE Bot、Telegram 與永豐金 Shioaji 之間的即時 `SYN-ACK` 雙向交握封包；進階模組統一收合於頂部「**功能選單 ▾**」彈窗，保持主畫面簡潔。

![SYNAPSE TW 總覽指揮台](docs/screenshots/desk_overview.png)

### 2. 下單實時偵測最新股價 ＋ 價量多 Agent 自動選模預檢
> 送單前自動繞過快取即時抓取最新真實股價、計算即時滑價，同步執行價量動能 Agent、10 大技術・籌碼・RS 開關投票與風控守門員，並依盤中量比／振幅／背離自動切換最適 AI 模型（若遇超買賣、滑價超標或方向衝突，系統會彈出警示確認機制）。

![下單實時價量多 Agent 預檢](docs/screenshots/order_agent_precheck.png)

### 3. 台股技術・籌碼・大盤強弱開關（策略版 v01）& 多 Agent 自動買賣中心
> 整理 **10 大獨立開關（v01）**（`MA/EMA 均線`、`MACD 動能`、`KDJ 隨機指標`、`RSI(14)`、`OBV 能量潮與量比`、`布林通道 (20,2σ)`、`VWAP 機構加權均價`、`5/20 日動能輪動`、`FinMind 三大法人籌碼`、`相對大盤 RS 強弱 (^TWII)`），同時支援 **TWSE 上市 (`.TW`) 與 TPEx 上櫃 (`.TWO`)** 即時股價偵測、「單一標的／全自選股輪巡」自動買賣，並可直接接收來自「量化策略實驗室」的已連結策略訊號參與加權投票。

![台股技術開關 v01 與多 Agent 自動買賣彈窗](docs/screenshots/agent_strategy_modal.png)

### 4. 量化策略實驗室（30 支 Alorse PineScript 策略 × 台股真實回測 × 風險指標卡）
> 完整整合於「技術開關 (v01) & 量化實驗室」子分頁，內建 **30 支經典策略**（趨勢 9 · 動能 13 · 均值回歸 6 · 其他 2），透過真實 FinMind／Yahoo 日線與除權息資料執行台股現股規則回測（升降單位 tick、漲跌停 ±10% 鎖死延遲撮合、自動同步全域 `2.8` 折手續費、股票/ETF/債券 ETF 證交稅與二代健保 2.11%）。支援 **策略排行、K 線買賣點與參數即時微調、1D/2D 參數最佳化熱力圖、多股批次回測矩陣、標的風險指標卡（Sharpe / Sortino / Calmar / VaR 95% / CVaR / Beta / Alpha）**，並可一鍵將任一策略最新訊號連結至 v01 多 Agent。

![量化策略實驗室 30 支策略排行與風險指標](docs/screenshots/quant_lab_modal.png)

### 5. 多模型 AI 深度解析 & 自動偵測選模中心（載入 Token → 偵測可用模型 → 供選分析模型）
> 整合 **Groq 極速雲端 API**、**Ollama 地端模型**、**Gemini**、**ChatGPT (OpenAI)**、**Claude (Anthropic)**、**Grok Bot (xAI)** 六大引擎：
> - **載入 AI Token 自動偵測可用模型**：支援貼上 Token 即時自動偵測，或透過「**匯入 Token / .env**」一次載入 `.env`／`JSON` 金鑰檔，零推論費呼叫各家模型清單端點，自動過濾並依最新旗艦排序填入每家獨立下拉選單（如 `gemini-2.5-pro`、`gpt-4.1`、`o4-mini`、`claude-sonnet-4`、`llama-3.3-70b-versatile`）。
> - **供選分析模型選擇器**：上方提供互動式模型勾選列，可自由指定參與深度解析的引擎與版本，或一鍵點擊「**僅選已驗證可用**」只跑已連通模型。
> - **彈性自訂分析焦點（不再鎖定 2330）**：在「自訂分析焦點」直接輸入多檔台股代號（如 `分析 6213、2368、3037、3189`）或策略主題，自動即時平行抓取所有標的的真實日線報價、中文股名、單日漲跌與 RSI 進行整合比較。

![AI 六模型自動偵測選模與多股深度解析](docs/screenshots/ai_six_models_modal.png)

### 6. 對帳單交易復盤（支援 PDF 含密碼 / XLSX / CSV / JSON 匯入與退佣試算）
> 支援券商對帳單匯入、手續費折數（預設 `2.8` 折，可修改，最低 `6` 折）預估退佣計算、交易健康度評分、五維能力雷達圖與一鍵送交 AI 復盤。

![對帳單交易復盤彈窗](docs/screenshots/review_modal.png)

### 7. 台股真實定投回測（FinMind 真實歷史股價＋除權息配股）
> 支援多標的權重配置與多階段每月扣款設定，即時向 FinMind 抓取真實歷史日 K 與 `TaiwanStockDividend` 除權息配股資料，計算含息總報酬率 ROI 與手續費折數影響。

![台股真實定投回測彈窗](docs/screenshots/dca_backtest_modal.png)

### 8. LINE Bot & Telegram 自動綁定通知中心（僅填 Bot Token・免填 Chat ID）
> **Telegram Bot 零門檻安全串接**：畫面僅需貼上 `Bot Token`（免填 Chat ID），後端即以 Encrypt-then-MAC 加密存檔並遮罩顯示，自動產生一次性 `/start <setup_code>` 防搶先設定碼；建立者私訊 Bot 傳送該指令後，系統透過 `getUpdates` 自動取存私訊 `owner_chat_id` 並令設定碼失效，後續交易警示自動以 HTML 格式推播至建立者。

![Telegram 等待建立者傳送 /start 驗證](docs/screenshots/tg_awaiting_start.png)
![LINE Bot 與 Telegram 已綁定通知中心](docs/screenshots/notify_line_tg.png)

### 9. 手機版獨立執行介面（Android APK × iOS/Android PWA × 底部導覽列 × 區網直連）
> 專為手機操作最佳化：內建固定底部五頁籤導覽列（`行情` · `下單` · `AI 選模` · `量化策略` · `手機安裝`）、安全區適配（`safe-area-inset`）、防 iOS 自動縮放輸入欄與全螢幕行動彈窗，零水平溢出。

<p align="center">
  <img src="docs/screenshots/mobile_app_view.png" alt="手機版 App 安裝與操作介面" width="320" />
  <img src="docs/screenshots/mobile_rwd.png" alt="行動版 RWD 看盤主畫面" width="320" />
</p>

---

## 核心功能（v3.4.0）

| 模組 | 說明 |
| :--- | :--- |
| **1. AI Token 自動偵測可用模型 ＋ 供選分析模型** | 貼上 API Token 或匯入 `.env`／`JSON` 金鑰檔後，系統自動呼叫各家輕量 `/models` 端點（零推論費），自動過濾並更新 **Gemini、ChatGPT、Claude、Grok、Groq、Ollama** 六大下拉清單；使用者可自由挑選具體模型版本並勾選參與分析的模型（支援一鍵「僅選已驗證可用」），結合自訂分析焦點多檔即時整合比較。 |
| **2. 手機版可執行（Android APK / iOS & Android PWA / 區網 / Termux）** | 提供四種零門檻手機執行方式：① **Android 獨立 APK**（內建本機 HTTP 資源與白名單安全代理，免開電腦即可抓真實報價與呼叫全部雲端 AI）；② **iOS / Android PWA**（支援「加入主畫面」全螢幕 App 與 Service Worker 離線外殼）；③ **電腦 `--lan` 區網直連**（同 Wi-Fi 手機共用電腦端 Ollama 與 Shioaji）；④ **Android Termux 完整本機後端腳本**。 |
| **3. 台股技術分析 (v01) × 量化策略實驗室整合** | 結合 **10 大獨立開關（v01）**（MA/EMA、MACD、KDJ、RSI(14)、OBV 量比、布林通道、VWAP、5/20日動能輪動、FinMind 三大法人籌碼、相對大盤 `^TWII` RS 強弱）與 **30 支 Alorse PineScript 量化策略實驗室**（台股規則日線回測、參數最佳化、多股批次矩陣、標的風險卡）；任一量化策略最新訊號可一鍵連結至 v01 多 Agent 參與加權投票與自動買賣（支援上市 `.TW` 與上櫃 `.TWO` 雙市場）。 |
| **4. 下單實時股價偵測 ＋ 價量多 Agent 自動選模** | 透過 `POST /api/v1/agent/orchestrate`（與前端靜態降級引擎），下單前繞過快取即時抓取最新真實股價、計算滑價，由 **價量動能 Agent**、**技術籌碼與量化策略 Agent**、**風控守門員 Agent**、**最適模型自動路由 Agent** 協作處理。支援 `AUTO`、`SPEED_FIRST`、`LOCAL_FIRST`、`DEEP_REASONING` 四種路由策略並具容錯切換。 |
| **5. Telegram Bot 單欄位 Token 自動綁定推播** | 僅輸入 `Bot Token`（無 Chat ID 欄位）→ 驗證 `getMe` → Encrypt-then-MAC 加密存檔與畫面遮罩 → 產生一次性 `/start <setup_code>` → 輪詢 `getUpdates` 自動取存私訊 `owner_chat_id` 並令設定碼失效（每 Token 限 1 預設目標）；未綁定禁送；支援純文字、Markdown 轉安全 HTML 與交易警示表情。 |
| **6. 雙向資料交握視覺化 & 選單彈窗架構** | 60fps Canvas 雙向 `SYN → ACK` 封包、旋轉交握環與節點檢視器；所有進階模組整合至頂部「**功能選單 ▾**」彈窗切換，保持主介面簡潔清爽。 |
| **7. 手續費折數全域連動** | 預設 **`2.8` 折**，可於下單票、復盤彈窗、定投回測、量化策略實驗室與設定中自由修改，最低優惠上限 **`6.0` 折**（超過自動校驗），即時套用至所有手續費與退佣計算。 |
| **8. 對帳單交易復盤 & 台股真實定投回測** | 支援多選匯入 PDF（含密碼解鎖）/ XLSX / CSV / JSON 對帳單復盤與退佣試算；並提供 FinMind 真實歷史股價＋除權息配股定投回測查詢，嚴禁猜測假數據。 |
| **9. TradingView × LINE Bot × SIM/LIVE 切換** | 內嵌 TradingView 進階線圖與帳號／雲端 Layout 綁定、Pine Script v5 Webhook；LINE Bot 條列股票代號＋中文名稱；支援 SIM 模擬盤與永豐金 Shioaji LIVE 實盤安全切換與緊急熔斷。 |

---

## 快速開始（電腦版 ＋ 手機版直接執行）

### 方式一：直接下載 GitHub Release 執行（免裝 Python · 含手機 Android APK）
前往本專案的 **[Releases 頁面](https://github.com/LHSheng-404/TradingAutomatic/releases)** 下載最新版本 `v3.4.0`：

- **Android 手機（獨立安裝執行檔 APK）**：
  - 下載 `SYNAPSE-TW-v3.4.0-android.apk` 直接於 Android 手機安裝開啟（內建本機資源與安全代理，無需電腦即可即時看盤、自動偵測 AI 模型與執行深度分析；亦可綁定電腦區網或雲端後端）。
- **iPhone / iPad / Android（免裝檔 PWA 手機 App）**：
  - 手機瀏覽器開啟 **[線上 PWA 網頁版](https://lhsheng-404.github.io/TradingAutomatic/)**（或同 Wi-Fi 電腦區網網址），iPhone Safari 點「分享 ↑ → 加入主畫面」、Android Chrome 點「安裝應用程式」，即可全螢幕 App 執行。
- **Android Termux（零電腦於手機本機執行完整 Python 後端）**：
  ```bash
  git clone https://github.com/LHSheng-404/TradingAutomatic.git && cd TradingAutomatic
  chmod +x mobile/termux-start.sh && ./mobile/termux-start.sh
  ```
- **Windows（免安裝單檔直接執行）**：
  - 下載 `SYNAPSE-TW-v3.4.0-windows-x64.exe` 直接雙擊執行，自動開啟瀏覽器並顯示同 Wi-Fi 手機區網連線網址。
- **macOS（Apple Silicon）**：
  ```bash
  chmod +x SYNAPSE-TW-v3.4.0-macos-arm64
  ./SYNAPSE-TW-v3.4.0-macos-arm64
  ```
- **Linux（免安裝單檔直接執行）**：
  ```bash
  chmod +x SYNAPSE-TW-v3.4.0-linux-x64
  ./SYNAPSE-TW-v3.4.0-linux-x64
  ```
- **跨平台可攜原始碼包**：
  - 下載 `SYNAPSE-TW-v3.4.0-portable.zip`（或 `.tar.gz`），解壓縮後雙擊 `start.bat`（Windows）或執行 `./start.sh`（macOS / Linux）。

啟動後瀏覽器會自動開啟 `http://127.0.0.1:8000`，同時於終端機顯示同 Wi-Fi 手機區網位址 `http://<區網IP>:8000`（如欲僅限本機存取可加 `--local-only`，或自訂 `--port 8080 --no-browser`）。

---

### 方式二：原始碼一鍵啟動

```bash
git clone https://github.com/LHSheng-404/TradingAutomatic.git
cd TradingAutomatic
```

- **Linux / macOS**：
  ```bash
  ./start.sh
  ```
- **Windows**：
  雙擊執行 `start.bat`，或於命令提示字元執行：
  ```cmd
  start.bat
  ```

---

### 方式三：一鍵打包發行版（產出單檔執行檔 + Portable ZIP/TAR.GZ）

本專案內建跨平台一鍵打包腳本 [`package.py`](package.py)：

```bash
pip install -r requirements.txt pyinstaller
python3 package.py --version v3.3.1
```

執行完成後會在 `dist/` 目錄自動產出：
1. `SYNAPSE-TW-v3.3.1-<os>-<arch>`（免安裝單一可執行檔，已內嵌 `index.html`）
2. `SYNAPSE-TW-v3.3.1-portable.zip` 與 `.tar.gz`（跨平台可攜包）
3. `SHA256SUMS.txt`（完整性校驗檔）

> 專案同時包含 GitHub Actions 自動發布管線 [`.github/workflows/release.yml`](.github/workflows/release.yml)，每次推送 `v*` 標籤時會自動於雲端同步編譯 **Windows `.exe`、macOS、Linux** 三平台直出執行檔並上傳至 Release。

---

### 方式四：雲端一鍵部署

#### A. Render.com 全棧部署（推薦，Yahoo / FinMind / 六模型 AI / 多 Agent / LINE / Telegram 加密自動綁定 / Shioaji 全支援）
1. 登入 [Render.com](https://render.com) → **New +** → **Blueprint**。
2. 選擇 `LHSheng-404/TradingAutomatic`（自動讀取 [`render.yaml`](render.yaml)）。
3. 於 Environment Variables 填入您的金鑰（見下方環境變數說明）即可取得 HTTPS 公開網址。

#### B. Docker 容器部署
```bash
cp .env.example .env
# 編輯 .env 填入您的 API 金鑰
docker build -t synapse-tw .
docker run -d -p 8000:8000 --env-file .env --name synapse-tw synapse-tw
```

#### C. 純靜態前端部署（Cloudflare Pages / Vercel / GitHub Pages）
- 本專案已內建 [`.github/workflows/pages.yml`](.github/workflows/pages.yml) 與 [`vercel.json`](vercel.json)，純靜態前端預設直連 FinMind v4 (`Access-Control-Allow-Origin: *`) 真實台股資料；亦可於網頁內「雲端部署與腳本」填入已部署的 FastAPI 後端網址，立即啟用 Yahoo 盤中代理、六模型 AI、多 Agent 與加密 Telegram / LINE 推播。

---

## 詳細使用指南與環境設定

複製 `.env.example` 為 `.env`（切勿將 `.env`、`.synapse_secret.key` 或 `.pfx` 憑證上傳至 Git）：

```env
# 預設模擬盤 SIM；完成實盤風控測試與憑證驗證後方可改為 LIVE
TRADING_MODE=SIM
CHECK_TV_IP=true
TV_WEBHOOK_SECRET=SYNAPSE_TV_SECRET_2026

# 伺服器加密主金鑰（選填；未填時自動於本機生成 0600 權限 .synapse_secret.key 加密儲存 Bot Token）
SYNAPSE_MASTER_KEY=

# FinMind API v4 Token（選填，多組請以逗號分隔輪替）
FINMIND_TOKENS=

# 六模型 AI API 金鑰 & Ollama 地端模型設定（可填入任一或全部）
GROQ_API_KEY=
GROQ_MODEL=llama-3.3-70b-versatile
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_MODEL=qwen2.5:14b
XAI_API_KEY=
GROK_MODEL=grok-3-mini
GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.5-flash
ANTHROPIC_API_KEY=
CLAUDE_MODEL=claude-sonnet-4-20250514
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4o-mini

# LINE Messaging API Bot & Telegram Bot（Telegram 建議直接於網頁貼上 Token 自動綁定，免填 Chat ID）
LINE_CHANNEL_ACCESS_TOKEN=
LINE_TARGET_ID=
TELEGRAM_BOT_TOKEN=

# 永豐金 Shioaji 實盤設定（僅 LIVE 模式需要）
SHIOAJI_API_KEY=
SHIOAJI_SECRET_KEY=
SHIOAJI_CA_PATH=./certs/Sinopac.pfx
SHIOAJI_CA_PASS=
```

### 各模組串接說明
1. **Telegram Bot 一鍵自動綁定（僅填 Bot Token・免填 Chat ID）**
   - 向 `@BotFather` 申請 `Bot Token` 後，開啟「**功能選單 → LINE / TG 通知中心**」，於 Telegram 區塊貼上 `Bot Token` 並點擊「**儲存並驗證 Bot Token**」。
   - 系統驗證 Token 有效後即加密存檔（畫面遮罩且不於前端／日誌曝光明文），並產生一次性 `/start <setup_code>` 指令。
   - 使用建立者私訊開啟您的 Bot 傳送該指令，系統會透過 `getUpdates` 自動取存 `owner_chat_id` 並令一次性設定碼失效，隨後點擊「**發送測試通知**」即可驗證；如需更換接收者，點擊「**重新綁定通知目標**」即可重設。
2. **Groq 與 Ollama 地端模型串接（含各模型即時測試）**
   - **Groq**：至 [Groq Console](https://console.groq.com/) 取得 `gsk_...` 金鑰，於「功能選單 → AI 六模型 & Ollama」填入後可按「**測試**」驗證有效並按「載入」抓回可用模型（預設 `llama-3.3-70b-versatile`）。
   - **Ollama 地端模型**：本機安裝 [Ollama](https://ollama.com/) 並執行 `ollama pull qwen2.5:14b`（或 `deepseek-r1:14b`、`llama3.1:8b`）後，於設定頁按「**偵測模型**」或「**測試**」即可驗證連線與本機清單。
3. **台股技術分析開關 (v01) · 量化策略實驗室 & 多 Agent 自動買賣**
   - 透過「**功能選單 → 台股技術分析 (v01) · 量化實驗室 & 多 Agent**」切換兩大子分頁：
     - **v01 技術開關 & 多 Agent**：自由開關 10 大技術・籌碼・大盤強弱開關，選擇模型自動路由策略（推薦 `AUTO`），並設定巡檢週期、最大滑價容忍 %、觸發評分與 AI 最低信心門檻。
     - **量化策略實驗室**：對任一台股標的執行 **30 支 Alorse PineScript 經典策略** 真實台股規則回測、參數最佳化、多股批次與標的風險指標卡，並可於策略詳情點擊「**🔗 連結此策略至 v01 多 Agent**」將最新訊號直接納入自動決策。
   - 主畫面下單票預設勾選「**下單前實時偵測最新股價 ＋ 價量多 Agent 自動選模預檢**」，確保每次送單皆同步最新真實股價並通過滑價與多空衝突保護。
4. **TradingView 帳號與進階快訊串接**
   - 點擊圖表區「**登入 TradingView 進階圖表**」即可在您的已登入帳號開啟同步台股標的，亦可綁定專屬雲端 Layout 網址與 Pine Script v5 Webhook。
5. **LINE Bot 設定**
   - 至 [LINE Developers](https://developers.line.biz/) 建立 Messaging API Channel，取得 `Channel Access Token` 與 `User ID (U...)` / `Group ID (C...)`。
6. **模擬下單（SIM）與真實下單（LIVE）切換**
   - 頂部工具列可即時切換 **模擬下單 SIM** / **真實下單 LIVE**，隨時可按「**緊急熔斷**」立即鎖定所有手動與自動委託。
   - 若要啟用永豐金 Shioaji 實盤，請於 `requirements.txt` 取消 `shioaji` 註解並掛載有效的 `Sinopac.pfx` 憑證。

---

## 專案目錄結構

```text
TradingAutomatic/
├── index.html                   # 單頁響應式前端（雙向交握、TradingView、六模型 AI、多 Agent 技術開關、復盤、定投）
├── server.py                    # FastAPI 雲端後端（Yahoo/FinMind、Groq/Ollama/四旗艦 AI、多 Agent 編排、LINE/TG、Shioaji）
├── launcher.py                  # 一鍵啟動與 PyInstaller 單檔執行檔入口
├── package.py                   # 跨平台一鍵打包腳本（產出單檔執行檔 + Portable ZIP/TAR.GZ + SHA256）
├── start.sh                     # Linux / macOS 一鍵建立環境並啟動腳本
├── start.bat                    # Windows 雙擊一鍵啟動腳本
├── requirements.txt             # Python 套件清單
├── Dockerfile                   # Docker 容器映像檔設定
├── render.yaml                  # Render.com 一鍵部署藍圖
├── vercel.json                  # Vercel 靜態前端設定
├── .env.example                 # 環境變數範本（含 Groq & Ollama）
├── .github/workflows/
│   ├── release.yml              # GitHub Actions 多平台自動打包發布 Release
│   └── pages.yml                # GitHub Pages 靜態網頁自動部署
└── docs/screenshots/            # 實機渲染介面截圖
```

---

## 風險聲明

本專案僅供量化交易系統開發、技術研究與模擬回測參考，不構成任何投資建議。金融市場交易具高度風險，切換至 **真實下單 LIVE** 模式或啟用 **多 Agent 自動買賣** 前，請務必先於 **模擬盤 SIM** 完整測試技術開關權重、滑價上限、Webhook 驗證與停損風控規則，使用者須自行承擔一切實盤交易損益與責任。
