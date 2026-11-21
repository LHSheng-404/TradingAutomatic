"""
SYNAPSE TW — 全自動台股交易系統雲端後端 (FastAPI)
功能：
1. 靜態託管單頁前端 index.html（支援一鍵部署至 Render / Railway / Fly.io / Docker）
2. Yahoo Finance & FinMind v4 真實台股行情與個股中文名稱代理（嚴禁猜測、含 TTL 快取與多 Token 輪替）
3. 多模型 AI 平行解析代理：Grok Bot (xAI)、Gemini (Google)、Claude (Anthropic)、ChatGPT (OpenAI)
4. LINE Bot (Messaging API Push) & Telegram Bot 結構化訊號推播代理（免瀏覽器 CORS 限制）
5. 模擬下單 (SIM) / 真實下單 (LIVE, 永豐金 Shioaji) 切換與 SQLite 毫秒時間戳稽核日誌
6. TradingView Webhook 官方 IP 白名單、密鑰驗證與去重派單端點
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from itertools import cycle
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

if getattr(sys, "frozen", False):
    ASSET_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    DATA_DIR = Path(sys.executable).resolve().parent
else:
    ASSET_DIR = Path(__file__).resolve().parent
    DATA_DIR = ASSET_DIR


def _load_local_env() -> None:
    env_file = DATA_DIR / ".env"
    if not env_file.exists():
        return
    try:
        for raw_line in env_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception:
        pass


_load_local_env()

BASE_DIR = ASSET_DIR
INDEX_HTML = ASSET_DIR / "index.html"
DB_PATH = Path(os.getenv("SYNAPSE_DB_PATH", str(DATA_DIR / "synapse_trades.db")))
TW_TZ = timezone(timedelta(hours=8))

TV_OFFICIAL_IPS = {
    "52.89.214.238",
    "34.212.75.30",
    "54.218.53.128",
    "52.32.178.7",
}

FINMIND_TOKENS = [
    t.strip() for t in os.getenv("FINMIND_TOKENS", "").split(",") if t.strip()
]
_token_cycle = cycle(FINMIND_TOKENS) if FINMIND_TOKENS else None

_CACHE: dict[str, tuple[float, Any]] = {}
CACHE_TTL_MARKET = int(os.getenv("CACHE_TTL_MARKET", "60"))
CACHE_TTL_INFO = 86400

_SEEN_SIGNALS: dict[str, float] = {}
KILL_SWITCH_ACTIVE = False
SERVER_TRADING_MODE = os.getenv("TRADING_MODE", "SIM").upper()
if SERVER_TRADING_MODE not in ("SIM", "LIVE"):
    SERVER_TRADING_MODE = "SIM"


# ---------------------------------------------------------------------------
# Database Initialization (SQLite millisecond execution audit log)
# ---------------------------------------------------------------------------
def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trade_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_ms INTEGER NOT NULL,
            iso_time TEXT NOT NULL,
            order_id TEXT UNIQUE NOT NULL,
            env_mode TEXT NOT NULL,
            symbol TEXT NOT NULL,
            stock_name TEXT NOT NULL,
            side TEXT NOT NULL,
            order_type TEXT NOT NULL,
            tif TEXT NOT NULL,
            qty INTEGER NOT NULL,
            unit TEXT NOT NULL,
            price REAL NOT NULL,
            fee_twd REAL DEFAULT 0,
            tax_twd REAL DEFAULT 0,
            status TEXT NOT NULL,
            broker_msg TEXT DEFAULT '',
            source TEXT DEFAULT 'WEB_UI'
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_orders_ts ON trade_orders(ts_ms DESC)"
    )
    conn.commit()
    conn.close()


init_db()


# ---------------------------------------------------------------------------
# Lightweight Stdlib Async HTTP Helpers (Zero Extra Runtime Dependencies)
# ---------------------------------------------------------------------------
def _sync_http_request(
    url: str,
    method: str = "GET",
    headers: Optional[dict[str, str]] = None,
    json_body: Optional[dict[str, Any]] = None,
    timeout: float = 12.0,
) -> tuple[int, str]:
    req_headers = {
        "User-Agent": "Mozilla/5.0 (compatible; SynapseTW/3.0)",
        "Accept": "application/json",
    }
    if headers:
        req_headers.update(headers)
    data = None
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json; charset=utf-8")

    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        return exc.code, err_body


async def http_json(
    url: str,
    method: str = "GET",
    headers: Optional[dict[str, str]] = None,
    json_body: Optional[dict[str, Any]] = None,
    timeout: float = 12.0,
) -> tuple[int, Any]:
    status, text = await asyncio.to_thread(
        _sync_http_request, url, method, headers, json_body, timeout
    )
    try:
        return status, json.loads(text)
    except Exception:
        return status, {"raw": text[:500]}


def cache_get(key: str, ttl: int) -> Any:
    item = _CACHE.get(key)
    if not item:
        return None
    ts, val = item
    if (time.time() - ts) < ttl:
        return val
    return None


def cache_set(key: str, val: Any) -> None:
    _CACHE[key] = (time.time(), val)


def tw_now_str() -> str:
    return datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="SYNAPSE TW — 全自動台股交易系統 API",
    version="3.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", include_in_schema=False)
async def serve_frontend():
    if INDEX_HTML.exists():
        return FileResponse(INDEX_HTML)
    return JSONResponse({"status": "ok", "message": "SYNAPSE TW Backend Running"})


@app.get("/api/v1/health")
async def health_check():
    shioaji_configured = bool(
        os.getenv("SHIOAJI_API_KEY") and os.getenv("SHIOAJI_SECRET_KEY")
    )
    ca_path = os.getenv("SHIOAJI_CA_PATH", "")
    ca_ready = bool(ca_path and Path(ca_path).exists())
    return {
        "status": "ok",
        "server_time": tw_now_str(),
        "trading_mode": SERVER_TRADING_MODE,
        "kill_switch": KILL_SWITCH_ACTIVE,
        "finmind_tokens": len(FINMIND_TOKENS),
        "shioaji_configured": shioaji_configured,
        "shioaji_ca_ready": ca_ready,
        "ai_env_configured": {
            "grok": bool(os.getenv("XAI_API_KEY")),
            "gemini": bool(os.getenv("GEMINI_API_KEY")),
            "claude": bool(os.getenv("ANTHROPIC_API_KEY")),
            "chatgpt": bool(os.getenv("OPENAI_API_KEY")),
        },
        "notifier_env_configured": {
            "line": bool(
                os.getenv("LINE_CHANNEL_ACCESS_TOKEN") and os.getenv("LINE_TARGET_ID")
            ),
            "telegram": bool(
                os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")
            ),
        },
    }


# ---------------------------------------------------------------------------
# 1. Real Market Data Endpoints (Yahoo Finance & FinMind v4 — Strictly Real)
# ---------------------------------------------------------------------------
@app.get("/api/v1/market/yahoo/{yahoo_symbol}")
async def get_yahoo_market_data(
    yahoo_symbol: str,
    interval: str = "15m",
    range_str: str = "5d",
):
    cache_key = f"yh:{yahoo_symbol}:{interval}:{range_str}"
    cached = cache_get(cache_key, CACHE_TTL_MARKET)
    if cached:
        return {"source": "yahoo_cache", **cached}

    encoded = urllib.parse.quote(yahoo_symbol, safe="^.-=")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?interval={urllib.parse.quote(interval)}&range={urllib.parse.quote(range_str)}"
    status, data = await http_json(url, timeout=10.0)
    if status != 200 or not isinstance(data, dict):
        raise HTTPException(
            status_code=502,
            detail=f"Yahoo Finance 抓取失敗 (HTTP {status})",
        )

    result_list = data.get("chart", {}).get("result") or []
    if not result_list:
        raise HTTPException(status_code=404, detail=f"Yahoo 查無標的 {yahoo_symbol}")

    res = result_list[0]
    meta = res.get("meta", {})
    quotes = (res.get("indicators", {}).get("quote") or [{}])[0]
    timestamps = res.get("timestamp") or []
    closes_raw = quotes.get("close") or []
    opens_raw = quotes.get("open") or []
    highs_raw = quotes.get("high") or []
    lows_raw = quotes.get("low") or []
    vols_raw = quotes.get("volume") or []

    candles = []
    for i, ts in enumerate(timestamps):
        c = closes_raw[i] if i < len(closes_raw) else None
        if c is None:
            continue
        dt = datetime.fromtimestamp(ts, TW_TZ)
        label = (
            dt.strftime("%m/%d %H:%M")
            if interval.endswith(("m", "h"))
            else dt.strftime("%Y-%m-%d")
        )
        candles.append(
            {
                "label": label,
                "open": round(float(opens_raw[i] or c), 2),
                "high": round(float(highs_raw[i] or c), 2),
                "low": round(float(lows_raw[i] or c), 2),
                "close": round(float(c), 2),
                "volume": int(vols_raw[i] or 0),
            }
        )

    latest_price = meta.get("regularMarketPrice")
    if latest_price is None and candles:
        latest_price = candles[-1]["close"]
    if latest_price is None:
        raise HTTPException(status_code=502, detail="Yahoo 回傳無有效價格")

    prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
    market_ts = meta.get("regularMarketTime")
    last_date = (
        datetime.fromtimestamp(market_ts, TW_TZ).strftime("%Y-%m-%d %H:%M")
        if market_ts
        else tw_now_str()[:16]
    )

    payload = {
        "symbol": yahoo_symbol,
        "currency": meta.get("currency", "TWD"),
        "price": round(float(latest_price), 2),
        "prev_close": round(float(prev_close), 2) if prev_close else None,
        "open": round(
            float(
                meta.get("regularMarketOpen")
                or (candles[-1]["open"] if candles else latest_price)
            ),
            2,
        ),
        "high": round(
            float(
                meta.get("regularMarketDayHigh")
                or (max(x["high"] for x in candles[-24:]) if candles else latest_price)
            ),
            2,
        ),
        "low": round(
            float(
                meta.get("regularMarketDayLow")
                or (min(x["low"] for x in candles[-24:]) if candles else latest_price)
            ),
            2,
        ),
        "volume": int(meta.get("regularMarketVolume") or 0),
        "last_date": last_date,
        "candles": candles[-40:],
    }
    cache_set(cache_key, payload)
    return {"source": "yahoo_live", **payload}


@app.get("/api/v1/market/finmind/{stock_id}")
async def get_finmind_market_data(
    stock_id: str,
    dataset: str = "TaiwanStockPrice",
    days: int = 45,
    token: str = "",
):
    start_date = (datetime.now(TW_TZ) - timedelta(days=max(7, min(days, 180)))).strftime(
        "%Y-%m-%d"
    )
    cache_key = f"fm:{dataset}:{stock_id}:{start_date}"
    cached = cache_get(cache_key, CACHE_TTL_MARKET)
    if cached:
        return {"source": "finmind_cache", **cached}

    use_token = token.strip() or (next(_token_cycle) if _token_cycle else "")
    params = {
        "dataset": dataset,
        "data_id": stock_id,
        "start_date": start_date,
    }
    if use_token:
        params["token"] = use_token

    url = "https://api.finmindtrade.com/api/v4/data?" + urllib.parse.urlencode(params)
    status, data = await http_json(url, timeout=12.0)
    if status != 200 or not isinstance(data, dict):
        raise HTTPException(
            status_code=502,
            detail=f"FinMind 抓取失敗 (HTTP {status})",
        )

    rows = [r for r in (data.get("data") or []) if float(r.get("close") or 0) > 0]
    if dataset == "TaiwanFuturesDaily" and rows:
        reg = [
            r
            for r in rows
            if not r.get("trading_session") or r.get("trading_session") == "position"
        ]
        if reg:
            rows = reg
        by_date: dict[str, dict[str, Any]] = {}
        for r in rows:
            d = str(r.get("date"))
            if d not in by_date or int(r.get("volume") or 0) > int(
                by_date[d].get("volume") or 0
            ):
                by_date[d] = r
        rows = sorted(by_date.values(), key=lambda x: str(x.get("date")))

    if not rows:
        raise HTTPException(status_code=404, detail=f"FinMind 查無 {stock_id} 真實資料")

    recent = rows[-40:]
    latest = recent[-1]
    prev = recent[-2] if len(recent) > 1 else latest
    candles = [
        {
            "label": str(r.get("date"))[5:],
            "date": str(r.get("date")),
            "open": round(float(r.get("open") or r.get("close")), 2),
            "high": round(float(r.get("max") or r.get("close")), 2),
            "low": round(float(r.get("min") or r.get("close")), 2),
            "close": round(float(r.get("close")), 2),
            "volume": int(r.get("Trading_Volume") or r.get("volume") or 0),
        }
        for r in recent
    ]

    last_date = str(latest.get("date"))
    if dataset == "TaiwanFuturesDaily" and latest.get("contract_date"):
        last_date = f"{last_date} ({latest['contract_date']})"

    payload = {
        "symbol": stock_id,
        "dataset": dataset,
        "price": round(float(latest["close"]), 2),
        "prev_close": round(float(prev["close"]), 2),
        "open": round(float(latest.get("open") or prev["close"]), 2),
        "high": round(float(latest.get("max") or latest["close"]), 2),
        "low": round(float(latest.get("min") or latest["close"]), 2),
        "volume": int(latest.get("Trading_Volume") or latest.get("volume") or 0),
        "last_date": last_date,
        "candles": candles,
    }
    cache_set(cache_key, payload)
    return {"source": "finmind_live", **payload}


@app.get("/api/v1/market/stock-info/{stock_id}")
async def get_tw_stock_info(stock_id: str, token: str = ""):
    sid = stock_id.strip().upper()
    cache_key = f"fminfo:{sid}"
    cached = cache_get(cache_key, CACHE_TTL_INFO)
    if cached:
        return cached

    params = {"dataset": "TaiwanStockInfo", "data_id": sid}
    use_token = token.strip() or (next(_token_cycle) if _token_cycle else "")
    if use_token:
        params["token"] = use_token
    url = "https://api.finmindtrade.com/api/v4/data?" + urllib.parse.urlencode(params)
    status, data = await http_json(url, timeout=10.0)
    if status != 200 or not isinstance(data, dict):
        raise HTTPException(status_code=502, detail=f"FinMind StockInfo HTTP {status}")

    rows = data.get("data") or []
    if not rows:
        raise HTTPException(status_code=404, detail=f"FinMind 查無台股代號 {sid}")

    row = rows[0]
    market_type = str(row.get("type", "twse")).lower()
    info = {
        "stock_id": sid,
        "stock_name": row.get("stock_name", sid),
        "industry": row.get("industry_category", "台股"),
        "market_type": market_type,
        "yahoo_symbol": f"{sid}.TWO" if market_type == "tpex" else f"{sid}.TW",
        "tv_symbol": f"TPEX:{sid}" if market_type == "tpex" else f"TWSE:{sid}",
    }
    cache_set(cache_key, info)
    return info


@app.get("/api/v1/market/finmind-history/{stock_id}")
async def get_finmind_history(
    stock_id: str,
    start_date: str = "2023-01-01",
    token: str = "",
):
    sid = stock_id.strip().upper()
    cache_key = f"fmhist:{sid}:{start_date}"
    cached = cache_get(cache_key, 600)
    if cached:
        return {"source": "finmind_history_cache", **cached}

    params = {
        "dataset": "TaiwanStockPrice",
        "data_id": sid,
        "start_date": start_date,
    }
    use_token = token.strip() or (next(_token_cycle) if _token_cycle else "")
    if use_token:
        params["token"] = use_token

    url = "https://api.finmindtrade.com/api/v4/data?" + urllib.parse.urlencode(params)
    status, data = await http_json(url, timeout=18.0)
    if status != 200 or not isinstance(data, dict):
        raise HTTPException(status_code=502, detail=f"FinMind History HTTP {status}")

    rows = [
        {
            "date": str(r.get("date")),
            "open": round(float(r.get("open") or r.get("close")), 2),
            "max": round(float(r.get("max") or r.get("close")), 2),
            "min": round(float(r.get("min") or r.get("close")), 2),
            "close": round(float(r.get("close")), 2),
            "Trading_Volume": int(r.get("Trading_Volume") or 0),
        }
        for r in (data.get("data") or [])
        if float(r.get("close") or 0) > 0
    ]
    if not rows:
        raise HTTPException(status_code=404, detail=f"FinMind 查無 {sid} 自 {start_date} 起之真實歷史股價")

    payload = {"stock_id": sid, "start_date": start_date, "count": len(rows), "data": rows}
    cache_set(cache_key, payload)
    return {"source": "finmind_history_live", **payload}


@app.get("/api/v1/market/finmind-dividend/{stock_id}")
async def get_finmind_dividend(
    stock_id: str,
    start_date: str = "2023-01-01",
    token: str = "",
):
    sid = stock_id.strip().upper()
    cache_key = f"fmdiv:{sid}:{start_date}"
    cached = cache_get(cache_key, 3600)
    if cached:
        return {"source": "finmind_dividend_cache", **cached}

    params = {
        "dataset": "TaiwanStockDividend",
        "data_id": sid,
        "start_date": start_date,
    }
    use_token = token.strip() or (next(_token_cycle) if _token_cycle else "")
    if use_token:
        params["token"] = use_token

    url = "https://api.finmindtrade.com/api/v4/data?" + urllib.parse.urlencode(params)
    status, data = await http_json(url, timeout=15.0)
    if status != 200 or not isinstance(data, dict):
        raise HTTPException(status_code=502, detail=f"FinMind Dividend HTTP {status}")

    ex_map: dict[str, dict[str, float]] = {}
    for d in data.get("data") or []:
        cash_val = (
            float(d.get("CashEarningsDistribution") or 0)
            + float(d.get("CashStatutorySurplus") or 0)
            or float(d.get("cash_dividend") or 0)
        )
        stock_val = (
            float(d.get("StockEarningsDistribution") or 0)
            + float(d.get("StockStatutorySurplus") or 0)
            or float(d.get("stock_dividend") or 0)
        )
        ex_date = (
            str(d.get("CashExDividendTradingDate") or "").strip()
            or str(d.get("StockExDividendTradingDate") or "").strip()
            or str(d.get("date") or "").strip()
        )
        if ex_date and (cash_val > 0 or stock_val > 0):
            cur = ex_map.setdefault(ex_date, {"cash": 0.0, "stock": 0.0})
            cur["cash"] += cash_val
            cur["stock"] += stock_val

    dividends = [
        {
            "date": dt,
            "cash_dividend": round(vals["cash"], 4),
            "stock_dividend": round(vals["stock"], 4),
        }
        for dt, vals in sorted(ex_map.items())
    ]
    payload = {"stock_id": sid, "count": len(dividends), "data": dividends}
    cache_set(cache_key, payload)
    return {"source": "finmind_dividend_live", **payload}


# ---------------------------------------------------------------------------
# 2. Multi-Model AI Analysis Proxy (Grok, Gemini, Claude, ChatGPT)
# ---------------------------------------------------------------------------
class AIAnalyzeRequest(BaseModel):
    symbol: str
    stock_name: str
    market_snapshot: dict[str, Any]
    models: list[str] = Field(default_factory=lambda: ["grok", "gemini", "claude", "chatgpt"])
    api_keys: dict[str, str] = Field(default_factory=dict)
    custom_prompt: str = ""


def _build_grounded_prompt(req: AIAnalyzeRequest, role_hint: str) -> str:
    snap = req.market_snapshot
    return (
        f"你是一位嚴謹的台灣股市量化分析師（角色專長：{role_hint}）。\n"
        f"以下為剛抓取之真實市場數據，嚴禁猜測或捏造任何未提供的價格與財報數據：\n"
        f"- 標的：{req.symbol} {req.stock_name}\n"
        f"- 數據來源：{snap.get('source', '未知')}（日期：{snap.get('last_date', '--')}）\n"
        f"- 開盤：{snap.get('open')} ｜ 最高：{snap.get('high')} ｜ 最低：{snap.get('low')} ｜ 現價/收盤：{snap.get('price')}\n"
        f"- 漲跌幅：{snap.get('change_pct')}% ｜ 成交量：{snap.get('volume')}\n"
        f"- 技術指標：EMA5={snap.get('ema5')}, EMA12={snap.get('ema12')}, RSI(14)={snap.get('rsi')}, VWAP乖離={snap.get('vwap_diff')}%\n"
        f"{('額外指令：' + req.custom_prompt) if req.custom_prompt else ''}\n\n"
        f"請以繁體中文回覆純 JSON（勿加 Markdown 圍欄），格式如下：\n"
        f'{{"verdict":"BULL|NEUTRAL|BEAR","confidence":0-100整數,"summary":"80字內核心研判","key_levels":"支撐與壓力價位","risk_note":"40字內風控提醒"}}'
    )


def _extract_ai_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(cleaned[start : end + 1])
        except Exception:
            pass
    return {
        "verdict": "NEUTRAL",
        "confidence": 55,
        "summary": cleaned[:180],
        "key_levels": "請參考即時 K 線支撐壓力",
        "risk_note": "模型回覆非標準 JSON，已顯示原始摘要",
    }


async def _call_single_ai(model_id: str, req: AIAnalyzeRequest) -> dict[str, Any]:
    t0 = time.perf_counter()
    roles = {
        "grok": "Grok Bot (xAI) — 市場動能突圍、短線籌碼與情緒轉折",
        "gemini": "Gemini (Google) — 技術結構、量價配合與產業趨勢脈絡",
        "claude": "Claude (Anthropic) — 嚴格風控稽核、支撐壓力與停損紀律",
        "chatgpt": "ChatGPT (OpenAI) — 結構化多空交易劇本與倉位配置",
    }
    prompt = _build_grounded_prompt(req, roles.get(model_id, "量化分析"))

    try:
        if model_id == "grok":
            key = req.api_keys.get("grok") or os.getenv("XAI_API_KEY", "")
            if not key:
                return {"model": "grok", "connected": False, "error": "尚未設定 xAI Grok API Key"}
            status, body = await http_json(
                "https://api.x.ai/v1/chat/completions",
                method="POST",
                headers={"Authorization": f"Bearer {key}"},
                json_body={
                    "model": os.getenv("GROK_MODEL", "grok-3-mini"),
                    "temperature": 0.2,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=20.0,
            )
            if status != 200:
                return {"model": "grok", "connected": False, "error": f"xAI HTTP {status}"}
            text = body["choices"][0]["message"]["content"]

        elif model_id == "gemini":
            key = req.api_keys.get("gemini") or os.getenv("GEMINI_API_KEY", "")
            if not key:
                return {"model": "gemini", "connected": False, "error": "尚未設定 Google Gemini API Key"}
            gm = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{gm}:generateContent?key={urllib.parse.quote(key)}"
            status, body = await http_json(
                url,
                method="POST",
                json_body={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.2},
                },
                timeout=20.0,
            )
            if status != 200:
                return {"model": "gemini", "connected": False, "error": f"Gemini HTTP {status}"}
            text = body["candidates"][0]["content"]["parts"][0]["text"]

        elif model_id == "claude":
            key = req.api_keys.get("claude") or os.getenv("ANTHROPIC_API_KEY", "")
            if not key:
                return {"model": "claude", "connected": False, "error": "尚未設定 Anthropic Claude API Key"}
            status, body = await http_json(
                "https://api.anthropic.com/v1/messages",
                method="POST",
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                },
                json_body={
                    "model": os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514"),
                    "max_tokens": 512,
                    "temperature": 0.2,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=20.0,
            )
            if status != 200:
                return {"model": "claude", "connected": False, "error": f"Claude HTTP {status}"}
            text = body["content"][0]["text"]

        elif model_id == "chatgpt":
            key = req.api_keys.get("chatgpt") or os.getenv("OPENAI_API_KEY", "")
            if not key:
                return {"model": "chatgpt", "connected": False, "error": "尚未設定 OpenAI ChatGPT API Key"}
            status, body = await http_json(
                "https://api.openai.com/v1/chat/completions",
                method="POST",
                headers={"Authorization": f"Bearer {key}"},
                json_body={
                    "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                    "temperature": 0.2,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=20.0,
            )
            if status != 200:
                return {"model": "chatgpt", "connected": False, "error": f"OpenAI HTTP {status}"}
            text = body["choices"][0]["message"]["content"]

        else:
            return {"model": model_id, "connected": False, "error": "不支援的模型"}

        parsed = _extract_ai_json(text)
        return {
            "model": model_id,
            "connected": True,
            "latency_ms": round((time.perf_counter() - t0) * 1000),
            "verdict": str(parsed.get("verdict", "NEUTRAL")).upper(),
            "confidence": int(parsed.get("confidence", 60)),
            "summary": str(parsed.get("summary", "")),
            "key_levels": str(parsed.get("key_levels", "--")),
            "risk_note": str(parsed.get("risk_note", "")),
        }
    except Exception as exc:
        return {
            "model": model_id,
            "connected": False,
            "latency_ms": round((time.perf_counter() - t0) * 1000),
            "error": str(exc)[:140],
        }


@app.post("/api/v1/ai/analyze")
async def analyze_multi_ai(req: AIAnalyzeRequest):
    if req.market_snapshot.get("price") is None:
        raise HTTPException(
            status_code=400,
            detail="缺少真實報價，嚴禁在無真實數據時進行 AI 猜測分析",
        )
    results = await asyncio.gather(*(_call_single_ai(m, req) for m in req.models))
    return {
        "symbol": req.symbol,
        "stock_name": req.stock_name,
        "timestamp": tw_now_str(),
        "results": results,
    }


# ---------------------------------------------------------------------------
# 3. LINE Bot & Telegram Notification Proxy
# ---------------------------------------------------------------------------
class NotifyRequest(BaseModel):
    text: str
    line_token: str = ""
    line_target_id: str = ""
    tg_token: str = ""
    tg_chat_id: str = ""
    channels: list[str] = Field(default_factory=lambda: ["line", "telegram"])


@app.post("/api/v1/notify/broadcast")
async def broadcast_notify(req: NotifyRequest):
    out: dict[str, Any] = {}

    if "line" in req.channels:
        token = req.line_token.strip() or os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
        target = req.line_target_id.strip() or os.getenv("LINE_TARGET_ID", "")
        if not token or not target:
            out["line"] = {"ok": False, "error": "尚未設定 LINE Channel Access Token 或 Target ID"}
        else:
            status, body = await http_json(
                "https://api.line.me/v2/bot/message/push",
                method="POST",
                headers={"Authorization": f"Bearer {token}"},
                json_body={"to": target, "messages": [{"type": "text", "text": req.text}]},
                timeout=10.0,
            )
            out["line"] = {"ok": 200 <= status < 300, "status": status, "detail": body}

    if "telegram" in req.channels:
        token = req.tg_token.strip() or os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = req.tg_chat_id.strip() or os.getenv("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            out["telegram"] = {"ok": False, "error": "尚未設定 Telegram Bot Token 或 Chat ID"}
        else:
            url = f"https://api.telegram.org/bot{urllib.parse.quote(token)}/sendMessage"
            status, body = await http_json(
                url,
                method="POST",
                json_body={"chat_id": chat_id, "text": req.text},
                timeout=10.0,
            )
            out["telegram"] = {"ok": 200 <= status < 300, "status": status, "detail": body}

    return {"timestamp": tw_now_str(), "results": out}


# ---------------------------------------------------------------------------
# 4. Order Execution (Simulated vs Live Shioaji Switch) & Audit Log
# ---------------------------------------------------------------------------
class PlaceOrderRequest(BaseModel):
    env_mode: str = "SIM"  # SIM | LIVE
    symbol: str
    stock_name: str
    side: str  # BUY | SELL
    order_type: str = "LMT"  # LMT | MKT
    tif: str = "ROD"  # ROD | IOC | FOK
    qty: int = 1
    unit: str = "張"  # 張 | 股 | 口
    price: float
    fee_discount: float = 2.8  # 預設 2.8 折，可修改，最低 6 折（上限 6.0）
    source: str = "WEB_UI"


@app.post("/api/v1/orders/place")
async def place_order(req: PlaceOrderRequest):
    if KILL_SWITCH_ACTIVE:
        raise HTTPException(status_code=503, detail="系統已啟動緊急熔斷，禁止任何下單")
    if req.price <= 0 or req.qty <= 0:
        raise HTTPException(status_code=400, detail="價格與數量必須大於 0（嚴禁無報價下單）")

    mode = req.env_mode.upper()
    if mode not in ("SIM", "LIVE"):
        raise HTTPException(status_code=400, detail="無效的交易模式")

    shares = req.qty * 1000 if req.unit == "張" else req.qty
    notional = shares * req.price
    is_etf = req.symbol.startswith("00")
    safe_discount = max(0.1, min(6.0, float(req.fee_discount or 2.8)))
    fee = max(20.0, round(notional * 0.001425 * (safe_discount / 10.0), 2))
    tax = round(notional * (0.001 if is_etf else 0.003), 2) if req.side.upper() == "SELL" else 0.0

    ts_ms = int(time.time() * 1000)
    iso = tw_now_str()
    order_id = f"{mode}-{req.symbol}-{ts_ms % 100000000}"

    if mode == "LIVE":
        api_key = os.getenv("SHIOAJI_API_KEY", "")
        secret_key = os.getenv("SHIOAJI_SECRET_KEY", "")
        ca_path = os.getenv("SHIOAJI_CA_PATH", "")
        ca_pass = os.getenv("SHIOAJI_CA_PASS", "")
        if not api_key or not secret_key or not ca_path or not Path(ca_path).exists():
            raise HTTPException(
                status_code=412,
                detail="實盤 LIVE 模式需要於伺服器環境變數設定 SHIOAJI_API_KEY、SHIOAJI_SECRET_KEY 與有效 Sinopac.pfx 憑證路徑",
            )
        try:
            import shioaji as sj  # type: ignore

            def _exec_shioaji() -> tuple[str, str]:
                api = sj.Shioaji(simulation=False)
                api.login(api_key=api_key, secret_key=secret_key)
                api.activate_ca(ca_path=ca_path, ca_passwd=ca_pass)
                contract = api.Contracts.Stocks[req.symbol]
                order = api.Order(
                    price=req.price,
                    quantity=req.qty,
                    action=sj.constant.Action.Buy if req.side.upper() == "BUY" else sj.constant.Action.Sell,
                    price_type=sj.constant.StockPriceType.MKT if req.order_type == "MKT" else sj.constant.StockPriceType.LMT,
                    order_type=getattr(sj.constant.OrderType, req.tif, sj.constant.OrderType.ROD),
                    order_lot=sj.constant.StockOrderLot.Odd if req.unit == "股" else sj.constant.StockOrderLot.Common,
                    account=api.stock_account,
                )
                trade = api.place_order(contract, order)
                api.logout()
                return str(trade.order.id), str(trade.status.status)

            broker_id, status_str = await asyncio.to_thread(_exec_shioaji)
            order_id = broker_id or order_id
            broker_msg = f"永豐金 Shioaji 實盤委託送出 ({status_str})"
            final_status = "SUBMITTED_LIVE"
        except ImportError:
            raise HTTPException(
                status_code=501,
                detail="伺服器尚未安裝 shioaji 套件（請執行 pip install shioaji）",
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Shioaji 實盤下單錯誤：{exc}")
    else:
        broker_msg = "模擬盤 SIM 依真實行情即時撮合成交"
        final_status = "FILLED_SIM"

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        INSERT INTO trade_orders
        (ts_ms, iso_time, order_id, env_mode, symbol, stock_name, side, order_type, tif, qty, unit, price, fee_twd, tax_twd, status, broker_msg, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ts_ms,
            iso,
            order_id,
            mode,
            req.symbol,
            req.stock_name,
            req.side.upper(),
            req.order_type,
            req.tif,
            req.qty,
            req.unit,
            req.price,
            fee,
            tax,
            final_status,
            broker_msg,
            req.source,
        ),
    )
    conn.commit()
    conn.close()

    return {
        "ok": True,
        "order_id": order_id,
        "env_mode": mode,
        "symbol": req.symbol,
        "stock_name": req.stock_name,
        "side": req.side.upper(),
        "qty": req.qty,
        "unit": req.unit,
        "price": req.price,
        "fee_twd": fee,
        "tax_twd": tax,
        "status": final_status,
        "broker_msg": broker_msg,
        "iso_time": iso,
    }


@app.get("/api/v1/orders/history")
async def get_order_history(limit: int = 50):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM trade_orders ORDER BY ts_ms DESC LIMIT ?",
        (max(1, min(limit, 200)),),
    ).fetchall()
    conn.close()
    return {"orders": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# 5. TradingView Webhook Receiver
# ---------------------------------------------------------------------------
class TVWebhookPayload(BaseModel):
    secret: str
    signal_id: str
    symbol: str
    stock_name: str = ""
    action: str  # BUY | SELL
    price: float
    qty: int = 1
    unit: str = "張"
    env_mode: str = "SIM"


@app.post("/api/v1/webhook/tv")
async def receive_tv_webhook(payload: TVWebhookPayload, request: Request):
    client_ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "").split(",")[0].strip()
    check_ip = os.getenv("CHECK_TV_IP", "false").lower() == "true"
    expected_secret = os.getenv("TV_WEBHOOK_SECRET", "SYNAPSE_TV_SECRET_2026")

    if KILL_SWITCH_ACTIVE:
        raise HTTPException(status_code=503, detail="KILL_SWITCH_ACTIVE")
    if check_ip and client_ip not in TV_OFFICIAL_IPS:
        raise HTTPException(status_code=403, detail=f"Untrusted IP: {client_ip}")
    if payload.secret != expected_secret:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    now = time.time()
    if payload.signal_id in _SEEN_SIGNALS and (now - _SEEN_SIGNALS[payload.signal_id]) < 10:
        return {"status": "ignored", "reason": "DUPLICATE_SIGNAL_ID"}
    _SEEN_SIGNALS[payload.signal_id] = now

    order_req = PlaceOrderRequest(
        env_mode=payload.env_mode,
        symbol=payload.symbol,
        stock_name=payload.stock_name or payload.symbol,
        side=payload.action.upper(),
        qty=payload.qty,
        unit=payload.unit,
        price=payload.price,
        source="TRADINGVIEW_WEBHOOK",
    )
    res = await place_order(order_req)
    return {"status": "executed", "signal_id": payload.signal_id, "order": res}


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
