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
import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import socket
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
from fastapi.responses import FileResponse, JSONResponse, Response
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
SECRET_KEY_PATH = Path(os.getenv("SYNAPSE_SECRET_KEY_PATH", str(DATA_DIR / ".synapse_secret.key")))
TELEGRAM_API_BASE = os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
TG_SETUP_TTL_SEC = int(os.getenv("TG_SETUP_TTL_SEC", "900"))
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
# Authenticated Secret Encryption (Encrypt-then-MAC, Zero Extra Dependencies)
# & Strict Token Masking / Redaction
# ---------------------------------------------------------------------------
def _get_master_key() -> bytes:
    env_key = os.getenv("SYNAPSE_MASTER_KEY", "").strip()
    if env_key:
        return hashlib.sha256(env_key.encode("utf-8")).digest()
    if SECRET_KEY_PATH.exists():
        raw = SECRET_KEY_PATH.read_bytes()
        if len(raw) >= 32:
            return raw[:32]
    key = secrets.token_bytes(32)
    try:
        SECRET_KEY_PATH.write_bytes(key)
        os.chmod(SECRET_KEY_PATH, 0o600)
    except Exception:
        pass
    return key


def _derive_subkeys(master: bytes) -> tuple[bytes, bytes]:
    enc_key = hmac.new(master, b"synapse-tw-enc-v1", hashlib.sha256).digest()
    mac_key = hmac.new(master, b"synapse-tw-mac-v1", hashlib.sha256).digest()
    return enc_key, mac_key


def _keystream(enc_key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    ctr = 0
    while len(out) < length:
        block = hmac.new(enc_key, nonce + ctr.to_bytes(4, "big"), hashlib.sha256).digest()
        out.extend(block)
        ctr += 1
    return bytes(out[:length])


def encrypt_secret(plaintext: str) -> str:
    data = plaintext.encode("utf-8")
    enc_key, mac_key = _derive_subkeys(_get_master_key())
    nonce = secrets.token_bytes(16)
    stream = _keystream(enc_key, nonce, len(data))
    ct = bytes(a ^ b for a, b in zip(data, stream))
    tag = hmac.new(mac_key, b"v1" + nonce + ct, hashlib.sha256).digest()
    return "v1:" + base64.urlsafe_b64encode(nonce + ct + tag).decode("ascii")


def decrypt_secret(blob: str) -> str:
    if not blob or not blob.startswith("v1:"):
        raise ValueError("無效的加密資料格式")
    raw = base64.urlsafe_b64decode(blob[3:].encode("ascii"))
    if len(raw) < 48:
        raise ValueError("加密資料長度異常")
    nonce, ct, tag = raw[:16], raw[16:-32], raw[-32:]
    enc_key, mac_key = _derive_subkeys(_get_master_key())
    expected = hmac.new(mac_key, b"v1" + nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        raise ValueError("加密憑證驗證失敗（金鑰不符或資料遭竄改）")
    stream = _keystream(enc_key, nonce, len(ct))
    return bytes(a ^ b for a, b in zip(ct, stream)).decode("utf-8")


def mask_tg_token(token: str) -> str:
    t = (token or "").strip()
    if not t:
        return ""
    if ":" in t:
        bot_id, secret_part = t.split(":", 1)
        prefix = bot_id[:4] + ("*" * max(0, len(bot_id) - 4))
        tail = secret_part[-4:] if len(secret_part) >= 8 else ""
        return f"{prefix}:{'*' * 10}{tail}"
    return t[:3] + "********" + (t[-3:] if len(t) > 8 else "")


_TG_TOKEN_URL_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]{8,}", re.IGNORECASE)
_TG_RAW_TOKEN_RE = re.compile(r"\b\d{6,15}:[A-Za-z0-9_-]{20,}\b")


def redact_secrets(text: Any, extra_secrets: Optional[list[str]] = None) -> str:
    s = str(text or "")
    for sec in (extra_secrets or []):
        if sec:
            s = s.replace(sec, "[REDACTED_TOKEN]")
            s = s.replace(urllib.parse.quote(sec, safe=""), "[REDACTED_TOKEN]")
    s = _TG_TOKEN_URL_RE.sub("bot[REDACTED_TOKEN]", s)
    s = _TG_RAW_TOKEN_RE.sub("[REDACTED_TOKEN]", s)
    return s


def format_telegram_html(text: str) -> str:
    """
    安全轉換純文字、基本 Markdown (**粗體**, `等寬`) 與交易警示表情為 Telegram HTML，
    避免 < > & 等符號導致 Telegram parse_mode=HTML 解析失敗。
    """
    escaped = html.escape(str(text or ""), quote=False)
    # Basic safe Markdown -> HTML conversion
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", escaped)
    return escaped


# ---------------------------------------------------------------------------
# Database Initialization (SQLite millisecond execution audit log & TG binding)
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS telegram_bindings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            token_hash TEXT NOT NULL,
            token_enc TEXT NOT NULL,
            token_masked TEXT NOT NULL,
            bot_id TEXT DEFAULT '',
            bot_username TEXT DEFAULT '',
            bot_name TEXT DEFAULT '',
            owner_chat_id TEXT DEFAULT '',
            owner_label TEXT DEFAULT '',
            setup_code TEXT DEFAULT '',
            setup_code_expires_at INTEGER DEFAULT 0,
            bound INTEGER DEFAULT 0,
            last_update_id INTEGER DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS encrypted_vault_sync (
            vault_id TEXT PRIMARY KEY,
            auth_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            device_name TEXT DEFAULT '',
            updated_at TEXT NOT NULL,
            updated_ts_ms INTEGER NOT NULL
        )
        """
    )
    # v3.8.0 — Device Matching / Unified Owner Database (same person, many devices → one owner_id)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS device_owners (
            owner_id TEXT PRIMARY KEY,
            owner_label TEXT DEFAULT '',
            sync_space TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS device_registry (
            device_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            device_name TEXT DEFAULT '',
            platform TEXT DEFAULT '',
            paired_at TEXT NOT NULL,
            last_seen TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS device_pair_codes (
            code_hash TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            expires_ts INTEGER NOT NULL,
            attempts INTEGER DEFAULT 0,
            used INTEGER DEFAULT 0,
            created_by TEXT DEFAULT ''
        )
        """
    )
    cols = [r[1] for r in conn.execute("PRAGMA table_info(trade_orders)").fetchall()]
    if "owner_id" not in cols:
        conn.execute("ALTER TABLE trade_orders ADD COLUMN owner_id TEXT DEFAULT ''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_owner ON trade_orders(owner_id, ts_ms DESC)")
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


def _safe_pct(v: Any) -> Optional[int]:
    try:
        n = int(round(float(str(v).replace("%", "").strip())))
        return max(0, min(100, n))
    except Exception:
        return None


def tw_now_str() -> str:
    return datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="SHL TW — 全自動台股交易系統 API",
    version="3.9.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def optional_access_password_guard(request: Request, call_next):
    """
    若伺服器環境變數設定了 SHL_ACCESS_PASSWORD，則所有 /api/v1/* 私有端點
    （/api/v1/health 與 /api/v1/webhook/tv 除外）皆須於標頭攜帶相符的
    X-SHL-Access-Token，防止同區網或外部陌生人存取。
    """
    required_pw = os.getenv("SHL_ACCESS_PASSWORD", "").strip()
    path = request.url.path or ""
    if (
        required_pw
        and request.method != "OPTIONS"
        and path.startswith("/api/v1/")
        and path not in ("/api/v1/health", "/api/v1/webhook/tv")
    ):
        provided = (request.headers.get("x-shl-access-token") or "").strip()
        if not provided or not hmac.compare_digest(provided, required_pw):
            return JSONResponse(
                status_code=401,
                content={
                    "detail": "此伺服器已啟用存取密碼保護，請於「安全保險箱 & 多設備連動」填入正確的後端存取密碼（X-SHL-Access-Token）。",
                    "access_password_required": True,
                },
            )
    return await call_next(request)


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
async def serve_frontend():
    if INDEX_HTML.exists():
        return FileResponse(INDEX_HTML)
    return JSONResponse({"status": "ok", "message": "SYNAPSE TW Backend Running"})


@app.get("/manifest.webmanifest", include_in_schema=False)
async def serve_manifest():
    p = ASSET_DIR / "manifest.webmanifest"
    if p.exists():
        return FileResponse(p, media_type="application/manifest+json")
    raise HTTPException(status_code=404, detail="manifest.webmanifest not found")


@app.get("/sw.js", include_in_schema=False)
async def serve_service_worker():
    p = ASSET_DIR / "sw.js"
    if p.exists():
        return FileResponse(p, media_type="application/javascript", headers={"Service-Worker-Allowed": "/"})
    raise HTTPException(status_code=404, detail="sw.js not found")


@app.get("/icons/{filename}", include_in_schema=False)
async def serve_pwa_icon(filename: str):
    safe = Path(filename).name
    p = ASSET_DIR / "icons" / safe
    if p.exists() and p.is_file():
        return FileResponse(p, media_type="image/png")
    raise HTTPException(status_code=404, detail="icon not found")


def _detect_lan_ips() -> list[str]:
    ips: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ips


@app.get("/api/v1/mobile/info")
async def get_mobile_access_info(request: Request):
    host_hdr = request.headers.get("host", "")
    port = int(os.getenv("SYNAPSE_ACTIVE_PORT", os.getenv("PORT", "8000")))
    if ":" in host_hdr:
        try:
            port = int(host_hdr.rsplit(":", 1)[-1])
        except ValueError:
            pass
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme or "http"
    lan_ips = _detect_lan_ips()
    lan_urls = [f"http://{ip}:{port}" for ip in lan_ips]
    primary_url = lan_urls[0] if lan_urls else f"{scheme}://{host_hdr or f'127.0.0.1:{port}'}"
    return {
        "ok": True,
        "version": "v3.5.2",
        "port": port,
        "lan_ips": lan_ips,
        "lan_urls": lan_urls,
        "primary_mobile_url": primary_url,
        "pwa_ready": (ASSET_DIR / "manifest.webmanifest").exists() and (ASSET_DIR / "sw.js").exists(),
        "bind_host": os.getenv("SYNAPSE_BIND_HOST", os.getenv("HOST", "0.0.0.0")),
    }


@app.get("/api/v1/health")
async def health_check():
    shioaji_configured = _shioaji_has_token()
    ca_path = os.getenv("SHIOAJI_CA_PATH", "")
    ca_ready = bool(ca_path and Path(ca_path).exists())
    return {
        "status": "ok",
        "version": "3.9.0",
        "e2ee_sync_supported": True,
        "realtime_relay_supported": True,
        "device_pairing_supported": True,
        "access_password_required": bool(os.getenv("SHL_ACCESS_PASSWORD", "").strip()),
        "server_time": tw_now_str(),
        "trading_mode": SERVER_TRADING_MODE,
        "kill_switch": KILL_SWITCH_ACTIVE,
        "finmind_tokens": len(FINMIND_TOKENS),
        "shioaji_configured": shioaji_configured,
        "shioaji_ca_ready": ca_ready,
        "ai_env_configured": {
            "groq": bool(os.getenv("GROQ_API_KEY")),
            "ollama": bool(os.getenv("OLLAMA_BASE_URL")),
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
                (_tg_get_binding_row() or {}).get("bound")
            ),
        },
        "telegram_binding": _tg_build_public_status(_tg_get_binding_row()),
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
    if interval in ("1d", "1wk", "1mo") and len(candles) >= 2:
        prev_close = candles[-2]["close"]
    market_ts = meta.get("regularMarketTime")
    last_date = (
        datetime.fromtimestamp(market_ts, TW_TZ).strftime("%Y-%m-%d %H:%M")
        if market_ts
        else tw_now_str()[:16]
    )

    payload = {
        "symbol": yahoo_symbol,
        "interval": interval,
        "range": range_str,
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
        "candles": candles[-75:],
    }
    cache_set(cache_key, payload)
    return {"source": "yahoo_live", **payload}


def _aggregate_finmind_weekly(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate real daily FinMind rows into genuine ISO weekly OHLCV candles."""
    buckets: dict[tuple[int, int], list[dict[str, Any]]] = {}
    order: list[tuple[int, int]] = []
    for r in rows:
        d_str = str(r.get("date") or "")[:10]
        try:
            dt = datetime.strptime(d_str, "%Y-%m-%d")
            key = (dt.isocalendar().year, dt.isocalendar().week)
        except Exception:
            continue
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(r)

    weekly: list[dict[str, Any]] = []
    for key in order:
        grp = buckets[key]
        first_r = grp[0]
        last_r = grp[-1]
        o_val = round(float(first_r.get("open") or first_r.get("close")), 2)
        c_val = round(float(last_r.get("close")), 2)
        h_val = round(max(float(x.get("max") or x.get("close")) for x in grp), 2)
        l_val = round(min(float(x.get("min") or x.get("close")) for x in grp), 2)
        v_val = sum(int(x.get("Trading_Volume") or x.get("volume") or 0) for x in grp)
        d_end = str(last_r.get("date") or "")[:10]
        weekly.append(
            {
                "label": f"{d_end[2:]} 週",
                "date": d_end,
                "open": o_val,
                "high": h_val,
                "low": l_val,
                "close": c_val,
                "volume": v_val,
            }
        )
    return weekly


@app.get("/api/v1/market/finmind/{stock_id}")
async def get_finmind_market_data(
    stock_id: str,
    dataset: str = "TaiwanStockPrice",
    days: int = 120,
    interval: str = "D",
    token: str = "",
):
    iv = (interval or "D").strip().upper()
    is_weekly = iv in ("W", "1WK", "WEEK")
    effective_days = max(days, 380) if is_weekly else max(7, min(days, 540))
    start_date = (datetime.now(TW_TZ) - timedelta(days=min(effective_days, 540))).strftime(
        "%Y-%m-%d"
    )
    cache_key = f"fm:{dataset}:{stock_id}:{start_date}:{iv}"
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

    latest = rows[-1]
    prev = rows[-2] if len(rows) > 1 else latest

    if is_weekly:
        candles = _aggregate_finmind_weekly(rows)[-60:]
        resolved_iv = "W"
    else:
        limit_map = {"5": 15, "15": 24, "60": 36, "D": 75}
        lim = limit_map.get(iv, 60)
        recent = rows[-lim:]
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
        resolved_iv = "D"

    last_date = str(latest.get("date"))
    if dataset == "TaiwanFuturesDaily" and latest.get("contract_date"):
        last_date = f"{last_date} ({latest['contract_date']})"

    payload = {
        "symbol": stock_id,
        "dataset": dataset,
        "interval": resolved_iv,
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


@app.get("/api/v1/market/quant-history/{stock_id}")
async def get_quant_history(
    stock_id: str,
    years: int = 5,
    source: str = "auto",
    token: str = "",
):
    """Fetch full multi-year real daily OHLCV (+ FinMind ex-dividend diffs) for Quant Strategy Lab."""
    sid = stock_id.strip().upper()
    yrs = max(1, min(15, int(years or 5)))
    src_mode = (source or "auto").strip().lower()
    cache_key = f"qhist:{sid}:{yrs}:{src_mode}"
    cached = cache_get(cache_key, 600)
    if cached:
        return {"cached": True, **cached}

    start_dt = (datetime.now(TW_TZ) - timedelta(days=yrs * 366 + 14)).strftime("%Y-%m-%d")
    use_token = token.strip() or (next(_token_cycle) if _token_cycle else "")
    errs: list[str] = []

    # 1) Try FinMind TaiwanStockPrice (+ TaiwanStockDividendResult for real ex-dividend adjustment)
    if src_mode != "yahoo":
        try:
            params = {"dataset": "TaiwanStockPrice", "data_id": sid, "start_date": start_dt}
            if use_token:
                params["token"] = use_token
            url = "https://api.finmindtrade.com/api/v4/data?" + urllib.parse.urlencode(params)
            st, data = await http_json(url, timeout=18.0)
            raw_rows = (data.get("data") or []) if (st == 200 and isinstance(data, dict) and data.get("status") == 200) else []
            rows = [
                {
                    "d": str(r.get("date")),
                    "o": round(float(r.get("open") or r.get("close")), 2),
                    "h": round(float(r.get("max") or r.get("close")), 2),
                    "l": round(float(r.get("min") or r.get("close")), 2),
                    "c": round(float(r.get("close")), 2),
                    "v": int(r.get("Trading_Volume") or 0),
                }
                for r in raw_rows
                if float(r.get("close") or 0) > 0 and r.get("date")
            ]
            if len(rows) > 30:
                divs: dict[str, float] = {}
                if sid != "TAIEX":
                    try:
                        d_params = {"dataset": "TaiwanStockDividendResult", "data_id": sid, "start_date": start_dt}
                        if use_token:
                            d_params["token"] = use_token
                        d_url = "https://api.finmindtrade.com/api/v4/data?" + urllib.parse.urlencode(d_params)
                        dst, ddata = await http_json(d_url, timeout=14.0)
                        if dst == 200 and isinstance(ddata, dict) and ddata.get("status") == 200:
                            for x in ddata.get("data") or []:
                                dt_str = str(x.get("date") or "").strip()
                                diff = float(x.get("before_price") or 0) - float(x.get("after_price") or 0)
                                if dt_str and diff > 0:
                                    divs[dt_str] = round(divs.get(dt_str, 0.0) + diff, 4)
                    except Exception:
                        pass
                payload = {
                    "stock_id": sid,
                    "name": "",
                    "src": "FinMind TaiwanStockPrice（未還原權息）",
                    "adjusted": False,
                    "rows": rows,
                    "divs": divs,
                }
                cache_set(cache_key, payload)
                return payload
            errs.append(f"FinMind: HTTP {st} / {len(rows)} 筆")
        except Exception as exc:
            errs.append(f"FinMind: {exc}")

    # 2) Fallback / Explicit Yahoo Finance (adjusted close for TWSE .TW / TPEx .TWO / ^TWII)
    if src_mode != "finmind":
        rng = "1y" if yrs <= 1 else ("2y" if yrs <= 2 else ("5y" if yrs <= 5 else "10y"))
        yh_candidates = ["^TWII"] if sid == "TAIEX" else [f"{sid}.TW", f"{sid}.TWO"]
        for sym in yh_candidates:
            try:
                encoded = urllib.parse.quote(sym, safe="^.-=")
                yurl = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?interval=1d&range={rng}"
                yst, ydata = await http_json(yurl, timeout=16.0)
                if yst != 200 or not isinstance(ydata, dict):
                    continue
                res_list = (ydata.get("chart") or {}).get("result") or []
                if not res_list:
                    continue
                res = res_list[0]
                meta = res.get("meta") or {}
                ts_list = res.get("timestamp") or []
                q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
                adj_list = ((res.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose") or []
                opens = q.get("open") or []
                highs = q.get("high") or []
                lows = q.get("low") or []
                closes = q.get("close") or []
                vols = q.get("volume") or []
                rows = []
                for idx, ts in enumerate(ts_list):
                    c = closes[idx] if idx < len(closes) else None
                    if c is None or float(c) <= 0:
                        continue
                    c_val = float(c)
                    adj_c = adj_list[idx] if idx < len(adj_list) else None
                    k = (float(adj_c) / c_val) if (adj_c is not None and float(adj_c) > 0) else 1.0
                    dt_str = datetime.fromtimestamp(ts, TW_TZ).strftime("%Y-%m-%d")
                    o_val = float(opens[idx] if idx < len(opens) and opens[idx] else c_val) * k
                    h_val = float(highs[idx] if idx < len(highs) and highs[idx] else c_val) * k
                    l_val = float(lows[idx] if idx < len(lows) and lows[idx] else c_val) * k
                    rows.append({
                        "d": dt_str,
                        "o": round(o_val, 4),
                        "h": round(max(h_val, o_val, c_val * k), 4),
                        "l": round(min(l_val, o_val, c_val * k), 4),
                        "c": round(c_val * k, 4),
                        "v": int(vols[idx] or 0) if idx < len(vols) else 0,
                    })
                if len(rows) > 30:
                    payload = {
                        "stock_id": sid,
                        "name": str(meta.get("longName") or meta.get("shortName") or ""),
                        "src": f"Yahoo {sym}（還原權息）",
                        "adjusted": True,
                        "rows": rows,
                        "divs": {},
                    }
                    cache_set(cache_key, payload)
                    return payload
            except Exception as exc:
                errs.append(f"Yahoo {sym}: {exc}")

    raise HTTPException(
        status_code=502,
        detail=f"無法取得 {sid} 近 {yrs} 年真實日線資料（{'; '.join(errs)}）",
    )



# ---------------------------------------------------------------------------
# 2. Multi-Model AI Proxy (Groq, Ollama Local, Grok, Gemini, Claude, ChatGPT)
#    + Real-Time Price-Volume Multi-Agent Orchestrator
# ---------------------------------------------------------------------------
class AIAnalyzeRequest(BaseModel):
    symbol: str = ""
    stock_name: str = ""
    market_snapshot: dict[str, Any] = Field(default_factory=dict)
    models: list[str] = Field(
        default_factory=lambda: ["groq", "ollama", "grok", "gemini", "claude", "chatgpt"]
    )
    api_keys: dict[str, str] = Field(default_factory=dict)
    model_options: dict[str, str] = Field(default_factory=dict)
    custom_prompt: str = ""
    focus_mode: str = "AUTO"  # AUTO | ACTIVE | WATCHLIST | CUSTOM_ONLY
    target_symbols: list[str] = Field(default_factory=list)
    multi_snapshots: list[dict[str, Any]] = Field(default_factory=list)


_TW_SYM_RE = re.compile(r"(?<![0-9A-Za-z])(00\d{2,4}[A-Z]?|[1-9]\d{3})(?![0-9A-Za-z])")
_ACTIVE_HINT_RE = re.compile(r"(目前標的|這檔|本檔|此檔|現價|目前價位|這支|這隻)")


def _extract_tw_symbols_from_prompt(text: str, limit: int = 8) -> list[str]:
    """Extract unique Taiwan stock/ETF codes from user custom focus prompt while skipping calendar years."""
    if not text:
        return []
    found: list[str] = []
    for m in _TW_SYM_RE.finditer(text.upper()):
        sym = m.group(1)
        end_idx = m.end()
        tail = text[end_idx : end_idx + 2]
        # Skip obvious calendar years like 2026年, 2026-11, 2026/05
        if sym.isdigit() and 2015 <= int(sym) <= 2035 and tail[:1] in ("年", "-", "/"):
            continue
        if sym not in found:
            found.append(sym)
            if len(found) >= limit:
                break
    return found


async def _fetch_live_snapshot_for_ai(sid: str) -> Optional[dict[str, Any]]:
    """Fetch 100% real quote, Chinese name, and basic technicals for a Taiwan stock/ETF/TAIEX."""
    code = sid.strip().upper()
    if not code:
        return None

    # Fetch real Chinese stock name from FinMind TaiwanStockInfo
    async def _get_name() -> str:
        if code in ("TAIEX", "^TWII"):
            return "台灣加權指數"
        try:
            info = await get_tw_stock_info(code)
            return str(info.get("stock_name") or "")
        except Exception:
            return ""

    async def _get_quote() -> Optional[dict[str, Any]]:
        yh_list = ["^TWII"] if code in ("TAIEX", "^TWII") else [f"{code}.TW", f"{code}.TWO"]
        for yh_sym in yh_list:
            try:
                y = await get_yahoo_market_data(yh_sym, interval="1d", range_str="3mo")
                if y and y.get("price") is not None:
                    mkt = "大盤" if yh_sym == "^TWII" else ("上市" if yh_sym.endswith(".TW") else "上櫃")
                    return {
                        "price": y["price"],
                        "prev_close": y.get("prev_close"),
                        "open": y.get("open"),
                        "high": y.get("high"),
                        "low": y.get("low"),
                        "volume": y.get("volume", 0),
                        "last_date": y.get("last_date", ""),
                        "source": f"Yahoo ({yh_sym}·{mkt})",
                        "candles": y.get("candles") or [],
                    }
            except Exception:
                continue
        if code not in ("TAIEX", "^TWII"):
            try:
                fm = await get_finmind_market_data(code, dataset="TaiwanStockPrice", days=60)
                if fm and fm.get("price") is not None:
                    return {
                        "price": fm["price"],
                        "prev_close": fm.get("prev_close"),
                        "open": fm.get("open"),
                        "high": fm.get("high"),
                        "low": fm.get("low"),
                        "volume": fm.get("volume", 0),
                        "last_date": fm.get("last_date", ""),
                        "source": f"FinMind ({code})",
                        "candles": fm.get("candles") or [],
                    }
            except Exception:
                pass
        return None

    stock_name, q = await asyncio.gather(_get_name(), _get_quote())
    if not q or q.get("price") is None:
        return None

    price = float(q["price"])
    candles = q.get("candles") or []
    closes = [float(c["close"]) for c in candles if c.get("close") is not None]
    if not closes:
        closes = [price]
    # Use previous trading day's close (closes[-2]) for accurate 1-day daily change %
    prev_c = float(closes[-2]) if len(closes) >= 2 else float(q.get("open") or price)
    change_pct = round(((price - prev_c) / prev_c) * 100.0, 2) if prev_c > 0 else 0.0

    # Lightweight technical indicators from real daily candles
    def _ema(vals: list[float], p: int) -> float:
        k = 2.0 / (p + 1)
        v = vals[0]
        for x in vals[1:]:
            v = x * k + v * (1.0 - k)
        return round(v, 2)

    ema5 = _ema(closes, 5)
    ema12 = _ema(closes, 12)
    n = len(closes)
    rsi_len = min(14, max(1, n - 1))
    gains = sum(max(0.0, closes[i] - closes[i - 1]) for i in range(n - rsi_len, n))
    losses = sum(max(0.0, closes[i - 1] - closes[i]) for i in range(n - rsi_len, n))
    rsi = 100.0 if losses == 0 else round(100.0 - 100.0 / (1.0 + (gains / rsi_len) / (losses / rsi_len)), 1)

    vols = [float(c.get("volume") or 0) for c in candles]
    nz_vols = [v for v in vols if v > 0]
    if len(nz_vols) >= 2:
        avg5 = sum(nz_vols[-6:-1]) / max(1, len(nz_vols[-6:-1]))
        vol_ratio = round(nz_vols[-1] / avg5, 2) if avg5 > 0 else 1.0
    else:
        vol_ratio = 1.0

    cum_pv = sum(((float(c.get("high") or c["close"]) + float(c.get("low") or c["close"]) + float(c["close"])) / 3.0) * max(1.0, float(c.get("volume") or 1.0)) for c in candles[-20:])
    cum_v = sum(max(1.0, float(c.get("volume") or 1.0)) for c in candles[-20:])
    vwap = cum_pv / cum_v if cum_v > 0 else price
    vwap_diff = round(((price - vwap) / vwap) * 100.0, 2) if vwap > 0 else 0.0

    return {
        "symbol": "TAIEX" if code == "^TWII" else code,
        "stock_name": stock_name or code,
        "source": q["source"],
        "last_date": q["last_date"],
        "open": q["open"],
        "high": q["high"],
        "low": q["low"],
        "price": price,
        "volume": q["volume"],
        "change_pct": change_pct,
        "volume_ratio": vol_ratio,
        "ema5": ema5,
        "ema12": ema12,
        "rsi": rsi,
        "vwap_diff": vwap_diff,
    }


def _build_grounded_prompt(
    req: AIAnalyzeRequest,
    role_hint: str,
    resolved_mode: str = "SINGLE",
    resolved_targets: Optional[list[dict[str, Any]]] = None,
) -> str:
    targets = resolved_targets or []
    focus = (req.custom_prompt or "").strip()

    if resolved_mode == "MULTI_TARGETS" and len(targets) >= 2:
        lines = []
        for idx, s in enumerate(targets, 1):
            lines.append(
                f"{idx}. {s.get('symbol')} {s.get('stock_name', '')}｜來源：{s.get('source', '--')}（{s.get('last_date', '--')}）"
                f"｜開 {s.get('open')} 高 {s.get('high')} 低 {s.get('low')} 現價 {s.get('price')}（{s.get('change_pct')}%）"
                f"｜量 {s.get('volume')}（量比 {s.get('volume_ratio', '--')}x）"
                f"｜EMA5={s.get('ema5')} EMA12={s.get('ema12')} RSI14={s.get('rsi')} VWAP乖離={s.get('vwap_diff')}%"
            )
        table_txt = "\n".join(lines)
        return (
            f"你是一位嚴謹的台灣股市量化分析師（角色專長：{role_hint}）。\n"
            f"使用者自訂分析焦點（首要任務）：{focus or '請整合比較下列台股標的之強弱、技術位階與操作優先順序'}\n\n"
            f"以下為系統即時抓取之「指定分析標的」真實市場數據（共 {len(targets)} 檔，請嚴格針對這 {len(targets)} 檔進行整合比較，切勿分析未列出的其他股票，亦嚴禁捏造未提供的價格或財報數據）：\n"
            f"{table_txt}\n\n"
            f"請以繁體中文回覆純 JSON（勿加 Markdown 圍欄），格式如下：\n"
            f'{{"verdict":"BULL|NEUTRAL|BEAR","confidence":0-100整數,"bull_pct":多方勝率0-100整數,"bear_pct":空方勝率0-100整數(兩者相加=100),"summary":"220字內整合比較研判（須涵蓋每檔強弱排序與核心差異）","key_levels":"各標的關鍵支撐壓力或首選順序","risk_note":"60字內共同風險與倉位提醒"}}'
        )

    if resolved_mode == "CUSTOM_FOCUS":
        ref_line = ""
        if targets:
            s = targets[0]
            ref_line = (
                f"\n即時大盤基準參考：{s.get('symbol')} {s.get('stock_name', '')} 現價 {s.get('price')}（{s.get('change_pct')}%，"
                f"RSI14={s.get('rsi')}，日期 {s.get('last_date', '--')}）。\n"
            )
        return (
            f"你是一位嚴謹的台灣股市量化研究與交易策略師（角色專長：{role_hint}）。\n"
            f"請完全以使用者的「自訂分析焦點」為核心進行整合解析，切勿自行離題去評論未提及的個股；若缺乏即時個股報價則嚴禁捏造價格數據。\n"
            f"使用者自訂分析焦點：{focus}\n"
            f"{ref_line}\n"
            f"請以繁體中文回覆純 JSON（勿加 Markdown 圍欄），格式如下：\n"
            f'{{"verdict":"BULL|NEUTRAL|BEAR","confidence":0-100整數,"bull_pct":多方勝率0-100整數,"bear_pct":空方勝率0-100整數(兩者相加=100),"summary":"200字內針對自訂焦點之深度整合分析","key_levels":"關鍵觀察指標、位階或篩選條件","risk_note":"60字內風控與執行紀律提醒"}}'
        )

    # Single target mode (either extracted single custom stock OR active stock)
    s = targets[0] if targets else {
        "symbol": req.symbol,
        "stock_name": req.stock_name,
        **(req.market_snapshot or {}),
    }
    return (
        f"你是一位嚴謹的台灣股市量化分析師（角色專長：{role_hint}）。\n"
        f"以下為剛抓取之真實市場數據，嚴禁猜測或捏造任何未提供的價格與財報數據：\n"
        f"- 標的：{s.get('symbol')} {s.get('stock_name', '')}\n"
        f"- 數據來源：{s.get('source', '未知')}（日期：{s.get('last_date', '--')}）\n"
        f"- 開盤：{s.get('open')} ｜ 最高：{s.get('high')} ｜ 最低：{s.get('low')} ｜ 現價/收盤：{s.get('price')}\n"
        f"- 漲跌幅：{s.get('change_pct')}% ｜ 成交量：{s.get('volume')} ｜ 量比：{s.get('volume_ratio', '--')}x\n"
        f"- 技術指標：EMA5={s.get('ema5')}, EMA12={s.get('ema12')}, MACD_OSC={s.get('macd_osc', '--')}, K/D={s.get('kd', '--')}, RSI(14)={s.get('rsi')}, VWAP乖離={s.get('vwap_diff')}%\n"
        f"{('使用者自訂分析焦點（請優先回答）：' + focus) if focus else ''}\n\n"
        f"請以繁體中文回覆純 JSON（勿加 Markdown 圍欄），格式如下：\n"
        f'{{"verdict":"BULL|NEUTRAL|BEAR","confidence":0-100整數,"bull_pct":多方勝率0-100整數,"bear_pct":空方勝率0-100整數(兩者相加=100),"summary":"120字內核心研判","key_levels":"支撐與壓力價位","risk_note":"50字內風控提醒"}}'
    )


def _extract_ai_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if "<think>" in cleaned and "</think>" in cleaned:
        cleaned = cleaned.split("</think>", 1)[-1].strip()
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
        "summary": cleaned[:240],
        "key_levels": "請參考即時 K 線支撐壓力",
        "risk_note": "模型回覆非標準 JSON，已顯示原始摘要",
    }


async def _call_single_ai(
    model_id: str,
    req: AIAnalyzeRequest,
    resolved_mode: str = "SINGLE",
    resolved_targets: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    roles = {
        "groq": "Groq 極速推理 — 毫秒級盤中爆量突圍、高波動短線訊號研判",
        "ollama": "Ollama 地端模型 — 零外傳隱私本地推論、全天候低延遲策略判定",
        "grok": "Grok Bot (xAI) — 市場動能突圍、短線籌碼與情緒轉折",
        "gemini": "Gemini (Google) — 技術結構、量價配合與產業趨勢脈絡",
        "claude": "Claude (Anthropic) — 嚴格風控稽核、支撐壓力與停損紀律",
        "chatgpt": "ChatGPT (OpenAI) — 結構化多空交易劇本與倉位配置",
    }
    prompt = _build_grounded_prompt(req, roles.get(model_id, "量化分析"), resolved_mode=resolved_mode, resolved_targets=resolved_targets)
    opts = req.model_options or {}

    try:
        if model_id == "groq":
            key = req.api_keys.get("groq") or os.getenv("GROQ_API_KEY", "")
            if not key:
                return {"model": "groq", "connected": False, "error": "尚未設定 Groq API Key"}
            groq_model = opts.get("groq_model") or os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
            status, body = await http_json(
                "https://api.groq.com/openai/v1/chat/completions",
                method="POST",
                headers={"Authorization": f"Bearer {key}"},
                json_body={
                    "model": groq_model,
                    "temperature": 0.2,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=26.0,
            )
            if status != 200:
                return {"model": "groq", "connected": False, "error": f"Groq HTTP {status}"}
            text = body["choices"][0]["message"]["content"]
            used_name = groq_model

        elif model_id == "ollama":
            base_url = (opts.get("ollama_url") or os.getenv("OLLAMA_BASE_URL", "")).strip().rstrip("/")
            if not base_url:
                return {"model": "ollama", "connected": False, "error": "尚未設定地端 Ollama 網址"}
            ollama_model = (opts.get("ollama_model") or os.getenv("OLLAMA_MODEL", "qwen2.5:14b")).strip()
            status, body = await http_json(
                f"{base_url}/api/chat",
                method="POST",
                json_body={
                    "model": ollama_model,
                    "stream": False,
                    "options": {"temperature": 0.2},
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=35.0,
            )
            if status == 200 and isinstance(body, dict) and body.get("message", {}).get("content"):
                text = body["message"]["content"]
            else:
                st2, body2 = await http_json(
                    f"{base_url}/v1/chat/completions",
                    method="POST",
                    json_body={
                        "model": ollama_model,
                        "temperature": 0.2,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                    timeout=35.0,
                )
                if st2 != 200 or not isinstance(body2, dict):
                    return {"model": "ollama", "connected": False, "error": f"Ollama/地端連線失敗 (HTTP {status}/{st2})"}
                text = body2["choices"][0]["message"]["content"]
            used_name = ollama_model

        elif model_id == "grok":
            key = req.api_keys.get("grok") or os.getenv("XAI_API_KEY", "")
            if not key:
                return {"model": "grok", "connected": False, "error": "尚未設定 xAI Grok API Key"}
            used_name = opts.get("grok_model") or os.getenv("GROK_MODEL", "grok-3-mini")
            status, body = await http_json(
                "https://api.x.ai/v1/chat/completions",
                method="POST",
                headers={"Authorization": f"Bearer {key}"},
                json_body={
                    "model": used_name,
                    "temperature": 0.2,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=26.0,
            )
            if status != 200:
                return {"model": "grok", "connected": False, "error": f"xAI HTTP {status}"}
            text = body["choices"][0]["message"]["content"]

        elif model_id == "gemini":
            key = req.api_keys.get("gemini") or os.getenv("GEMINI_API_KEY", "")
            if not key:
                return {"model": "gemini", "connected": False, "error": "尚未設定 Google Gemini API Key"}
            primary_gem = (opts.get("gemini_model") or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")).strip()
            if primary_gem.startswith("models/"):
                primary_gem = primary_gem[7:]
            gem_candidates = [primary_gem] + ([m for m in ("gemini-2.0-flash",) if m != primary_gem])
            used_name = primary_gem
            status, body = 0, {}
            for idx, gm in enumerate(gem_candidates):
                used_name = gm
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{urllib.parse.quote(gm, safe='.-_')}:generateContent?key={urllib.parse.quote(key)}"
                status, body = await http_json(
                    url,
                    method="POST",
                    json_body={
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {"temperature": 0.2},
                    },
                    timeout=26.0,
                )
                if status == 200 and isinstance(body, dict) and (body.get("candidates") or []):
                    break
                # Only fallback if first candidate returned 404/400 (model unavailable)
                if idx == 0 and status not in (400, 404):
                    break
            if status != 200 or not isinstance(body, dict) or not (body.get("candidates") or []):
                return {"model": "gemini", "connected": False, "error": f"Gemini HTTP {status} ({used_name})"}
            parts = ((body["candidates"][0].get("content") or {}).get("parts") or [])
            # Prefer non-thought text parts (Gemini 2.5 thinking models may emit thought parts first)
            non_thought = [str(p.get("text") or "") for p in parts if p.get("text") and not p.get("thought")]
            text = "\n".join(non_thought).strip() or "\n".join(str(p.get("text") or "") for p in parts if p.get("text")).strip()

        elif model_id == "claude":
            key = req.api_keys.get("claude") or os.getenv("ANTHROPIC_API_KEY", "")
            if not key:
                return {"model": "claude", "connected": False, "error": "尚未設定 Anthropic Claude API Key"}
            used_name = (opts.get("claude_model") or os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")).strip()
            status, body = await http_json(
                "https://api.anthropic.com/v1/messages",
                method="POST",
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                },
                json_body={
                    "model": used_name,
                    "max_tokens": 900,
                    "temperature": 0.2,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=26.0,
            )
            if status != 200:
                return {"model": "claude", "connected": False, "error": f"Claude HTTP {status} ({used_name})"}
            text = body["content"][0]["text"]

        elif model_id == "chatgpt":
            key = req.api_keys.get("chatgpt") or os.getenv("OPENAI_API_KEY", "")
            if not key:
                return {"model": "chatgpt", "connected": False, "error": "尚未設定 OpenAI ChatGPT API Key"}
            used_name = (opts.get("chatgpt_model") or os.getenv("OPENAI_MODEL", "gpt-4o-mini")).strip()
            oai_payload: dict[str, Any] = {
                "model": used_name,
                "messages": [{"role": "user", "content": prompt}],
            }
            # OpenAI o1/o3/o4 reasoning models reject custom temperature
            if not used_name.lower().startswith(("o1", "o3", "o4")):
                oai_payload["temperature"] = 0.2
            status, body = await http_json(
                "https://api.openai.com/v1/chat/completions",
                method="POST",
                headers={"Authorization": f"Bearer {key}"},
                json_body=oai_payload,
                timeout=26.0,
            )
            if status != 200:
                return {"model": "chatgpt", "connected": False, "error": f"OpenAI HTTP {status} ({used_name})"}
            text = body["choices"][0]["message"]["content"]

        else:
            return {"model": model_id, "connected": False, "error": "不支援的模型"}

        parsed = _extract_ai_json(text)
        return {
            "model": model_id,
            "model_name": used_name,
            "connected": True,
            "latency_ms": round((time.perf_counter() - t0) * 1000),
            "verdict": str(parsed.get("verdict", "NEUTRAL")).upper(),
            "confidence": int(parsed.get("confidence", 60)),
            "bull_pct": _safe_pct(parsed.get("bull_pct")),
            "bear_pct": _safe_pct(parsed.get("bear_pct")),
            "summary": str(parsed.get("summary", "")),
            "key_levels": str(parsed.get("key_levels", "--")),
            "risk_note": str(parsed.get("risk_note", "")),
        }
    except Exception as exc:
        return {
            "model": model_id,
            "connected": False,
            "latency_ms": round((time.perf_counter() - t0) * 1000),
            "error": redact_secrets(str(exc), list((req.api_keys or {}).values()))[:140],
        }


def _filter_and_sort_ai_models(mid: str, raw_models: list[str]) -> list[str]:
    """Filter non-chat utility models and sort flagship/latest analysis models first."""
    seen: set[str] = set()
    cleaned: list[str] = []
    for raw in raw_models:
        m = str(raw or "").strip()
        if mid == "gemini" and m.startswith("models/"):
            m = m[7:]
        if not m or m in seen:
            continue
        low = m.lower()
        if mid == "groq":
            if any(x in low for x in ("whisper", "tts", "playai", "prompt-guard", "llama-guard", "safeguard")):
                continue
        elif mid == "gemini":
            if not ("gemini" in low or "gemma" in low):
                continue
            if any(x in low for x in ("embedding", "imagen", "veo", "aqa", "tts", "audio", "robotics", "computer-use")):
                continue
        elif mid == "chatgpt":
            if not any(low.startswith(p) for p in ("gpt-", "o1", "o3", "o4", "chatgpt-")):
                continue
            if any(x in low for x in ("realtime", "audio", "transcribe", "tts", "image", "instruct", "embed", "search-preview", "diarize", "codex-mini")):
                continue
        elif mid == "grok":
            if any(x in low for x in ("image", "embed", "tts", "whisper")):
                continue
        elif mid == "claude":
            if not low.startswith("claude"):
                continue
        seen.add(m)
        cleaned.append(m)

    priority_rules: dict[str, list[str]] = {
        "gemini": ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-pro", "gemini-1.5-flash"],
        "chatgpt": ["gpt-4.1", "gpt-4o", "o4-mini", "o3-mini", "o3", "gpt-4o-mini", "gpt-4.1-mini"],
        "claude": ["claude-sonnet-4", "claude-opus-4", "claude-3-7-sonnet", "claude-3-5-sonnet", "claude-3-5-haiku"],
        "grok": ["grok-4", "grok-3-mini", "grok-3", "grok-2"],
        "groq": ["llama-3.3-70b", "deepseek-r1", "qwen", "llama-3.1-8b"],
    }
    prefs = priority_rules.get(mid, [])

    def _rank(name: str) -> tuple[int, int, str]:
        low = name.lower()
        for idx, pat in enumerate(prefs):
            if low == pat:
                return (0, idx, low)
            if low.startswith(pat):
                return (1, idx, low)
        return (2, 99, low)

    if mid == "ollama":
        return cleaned
    return sorted(cleaned, key=_rank)


@app.get("/api/v1/ai/ollama-models")
async def list_ollama_models(base_url: str = ""):
    url = (base_url or os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")).strip().rstrip("/")
    try:
        status, body = await http_json(f"{url}/api/tags", timeout=5.0)
        if status == 200 and isinstance(body, dict):
            models = [m.get("name") for m in (body.get("models") or []) if m.get("name")]
            return {"ok": True, "base_url": url, "models": models}
        # OpenAI-compatible local server (/v1/models)
        st2, body2 = await http_json(f"{url}/v1/models", timeout=5.0)
        if st2 == 200 and isinstance(body2, dict):
            models = [m.get("id") for m in (body2.get("data") or []) if m.get("id")]
            return {"ok": True, "base_url": url, "models": models}
        return {"ok": False, "base_url": url, "models": [], "error": f"HTTP {status}/{st2}"}
    except Exception as exc:
        return {"ok": False, "base_url": url, "models": [], "error": str(exc)[:120]}


@app.get("/api/v1/ai/groq-models")
async def list_groq_models(api_key: str = ""):
    key = api_key.strip() or os.getenv("GROQ_API_KEY", "")
    if not key:
        return {
            "ok": False,
            "models": [
                "llama-3.3-70b-versatile",
                "deepseek-r1-distill-llama-70b",
                "qwen-qwq-32b",
                "llama-3.1-8b-instant",
            ],
            "error": "未提供 Groq API Key（顯示預設推薦清單）",
        }
    try:
        status, body = await http_json(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=8.0,
        )
        if status == 200 and isinstance(body, dict):
            raw = [m.get("id") for m in (body.get("data") or []) if m.get("id")]
            models = _filter_and_sort_ai_models("groq", raw)
            return {"ok": True, "models": models}
        return {"ok": False, "models": [], "error": f"Groq HTTP {status}"}
    except Exception as exc:
        return {"ok": False, "models": [], "error": redact_secrets(str(exc), [key])[:120]}


class AITestRequest(BaseModel):
    model: str  # groq | ollama | grok | gemini | claude | chatgpt
    api_key: str = ""
    model_options: dict[str, str] = Field(default_factory=dict)


class AITestAllRequest(BaseModel):
    api_keys: dict[str, str] = Field(default_factory=dict)
    model_options: dict[str, str] = Field(default_factory=dict)
    models: list[str] = Field(
        default_factory=lambda: ["groq", "ollama", "grok", "gemini", "claude", "chatgpt"]
    )


async def _test_single_ai_connection(
    model_id: str,
    api_key: str = "",
    model_options: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    mid = (model_id or "").strip().lower()
    opts = model_options or {}
    key = (api_key or "").strip()

    def _err_msg(status: int, body: Any, secret: str) -> str:
        detail = ""
        if isinstance(body, dict):
            err_obj = body.get("error")
            if isinstance(err_obj, dict):
                detail = str(err_obj.get("message") or err_obj.get("type") or "")
            elif isinstance(err_obj, str):
                detail = err_obj
            elif body.get("message"):
                detail = str(body.get("message"))
        elif isinstance(body, str):
            detail = body[:120]
        if status in (401, 403):
            base = f"Token 無效或權限不足 (HTTP {status})"
        elif status == 429:
            base = "配額或速率受限，但金鑰格式有效 (HTTP 429)"
        elif status == 0:
            base = "無法連線至 API 端點"
        else:
            base = f"連線異常 (HTTP {status})"
        full = f"{base}：{detail}" if detail else base
        return redact_secrets(full, [secret])[:160]

    try:
        if mid == "groq":
            use_key = key or os.getenv("GROQ_API_KEY", "").strip()
            if not use_key:
                return {"ok": False, "model": mid, "latency_ms": 0, "models": [], "message": "尚未輸入 Groq API Key"}
            base = os.getenv("GROQ_API_BASE", "https://api.groq.com/openai/v1").rstrip("/")
            st, body = await http_json(f"{base}/models", headers={"Authorization": f"Bearer {use_key}"}, timeout=8.0)
            lat = round((time.perf_counter() - t0) * 1000)
            if st == 200 and isinstance(body, dict):
                raw = [m.get("id") for m in (body.get("data") or []) if m.get("id")]
                models = _filter_and_sort_ai_models("groq", raw)
                return {
                    "ok": True, "model": mid, "latency_ms": lat,
                    "models": models, "models_count": len(models),
                    "recommended_model": models[0] if models else "",
                    "message": f"Groq 驗證成功（偵測到 {len(models)} 個可用分析模型）",
                }
            return {"ok": False, "model": mid, "latency_ms": lat, "models": [], "message": _err_msg(st, body, use_key)}

        if mid == "ollama":
            base_url = (opts.get("ollama_url") or os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")).strip().rstrip("/")
            target_model = (opts.get("ollama_model") or os.getenv("OLLAMA_MODEL", "qwen2.5:14b")).strip()
            if not base_url:
                return {"ok": False, "model": mid, "latency_ms": 0, "models": [], "message": "尚未輸入地端 Ollama 網址"}
            st, body = await http_json(f"{base_url}/api/tags", timeout=5.0)
            models: list[str] = []
            if st == 200 and isinstance(body, dict):
                models = [m.get("name") for m in (body.get("models") or []) if m.get("name")]
            else:
                st2, body2 = await http_json(f"{base_url}/v1/models", timeout=5.0)
                if st2 == 200 and isinstance(body2, dict):
                    models = [m.get("id") for m in (body2.get("data") or []) if m.get("id")]
                    st = st2
            lat = round((time.perf_counter() - t0) * 1000)
            if st == 200:
                matched = any(m == target_model or m.startswith(target_model + ":") for m in models) if target_model else bool(models)
                if matched:
                    return {
                        "ok": True, "warning": False, "model": mid, "latency_ms": lat,
                        "models": models, "models_count": len(models),
                        "recommended_model": target_model if target_model in models else (models[0] if models else ""),
                        "message": f"Ollama 連線成功，模型 {target_model} 已就緒（共 {len(models)} 個）",
                    }
                return {
                    "ok": True, "warning": True, "model": mid, "latency_ms": lat,
                    "models": models, "models_count": len(models),
                    "recommended_model": models[0] if models else "",
                    "message": f"Ollama 服務在線（共 {len(models)} 個模型），已更新清單供選擇",
                }
            return {"ok": False, "model": mid, "latency_ms": lat, "models": [], "message": f"無法連線至 {base_url}（請確認本機已啟動 ollama serve）"}

        if mid == "grok":
            use_key = key or os.getenv("XAI_API_KEY", "").strip()
            if not use_key:
                return {"ok": False, "model": mid, "latency_ms": 0, "models": [], "message": "尚未輸入 xAI Grok API Key"}
            base = os.getenv("XAI_API_BASE", "https://api.x.ai/v1").rstrip("/")
            st, body = await http_json(f"{base}/models", headers={"Authorization": f"Bearer {use_key}"}, timeout=8.0)
            lat = round((time.perf_counter() - t0) * 1000)
            if st == 200 and isinstance(body, dict):
                raw = [m.get("id") for m in (body.get("data") or []) if m.get("id")]
                models = _filter_and_sort_ai_models("grok", raw)
                return {
                    "ok": True, "model": mid, "latency_ms": lat,
                    "models": models, "models_count": len(models),
                    "recommended_model": models[0] if models else "",
                    "message": f"xAI Grok 驗證成功（偵測到 {len(models)} 個可用模型）",
                }
            return {"ok": False, "model": mid, "latency_ms": lat, "models": [], "message": _err_msg(st, body, use_key)}

        if mid == "gemini":
            use_key = key or os.getenv("GEMINI_API_KEY", "").strip()
            if not use_key:
                return {"ok": False, "model": mid, "latency_ms": 0, "models": [], "message": "尚未輸入 Google Gemini API Key"}
            base = os.getenv("GEMINI_API_BASE", "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
            st, body = await http_json(f"{base}/models?key={urllib.parse.quote(use_key)}&pageSize=100", timeout=9.0)
            lat = round((time.perf_counter() - t0) * 1000)
            if st == 200 and isinstance(body, dict) and isinstance(body.get("models"), list):
                raw = [
                    m.get("name", "")
                    for m in body["models"]
                    if isinstance(m, dict)
                    and "generateContent" in (m.get("supportedGenerationMethods") or ["generateContent"])
                ]
                models = _filter_and_sort_ai_models("gemini", raw)
                return {
                    "ok": True, "model": mid, "latency_ms": lat,
                    "models": models, "models_count": len(models),
                    "recommended_model": models[0] if models else "",
                    "message": f"Google Gemini 驗證成功（偵測到 {len(models)} 個可用分析模型）",
                }
            return {"ok": False, "model": mid, "latency_ms": lat, "models": [], "message": _err_msg(st, body, use_key)}

        if mid == "claude":
            use_key = key or os.getenv("ANTHROPIC_API_KEY", "").strip()
            if not use_key:
                return {"ok": False, "model": mid, "latency_ms": 0, "models": [], "message": "尚未輸入 Anthropic Claude API Key"}
            base = os.getenv("ANTHROPIC_API_BASE", "https://api.anthropic.com/v1").rstrip("/")
            st, body = await http_json(
                f"{base}/models?limit=100",
                headers={"x-api-key": use_key, "anthropic-version": "2023-06-01"},
                timeout=8.0,
            )
            lat = round((time.perf_counter() - t0) * 1000)
            if st == 200 and isinstance(body, dict):
                raw = [m.get("id") for m in (body.get("data") or []) if m.get("id")]
                models = _filter_and_sort_ai_models("claude", raw)
                return {
                    "ok": True, "model": mid, "latency_ms": lat,
                    "models": models, "models_count": len(models),
                    "recommended_model": models[0] if models else "",
                    "message": f"Anthropic Claude 驗證成功（偵測到 {len(models)} 個可用模型）",
                }
            return {"ok": False, "model": mid, "latency_ms": lat, "models": [], "message": _err_msg(st, body, use_key)}

        if mid == "chatgpt":
            use_key = key or os.getenv("OPENAI_API_KEY", "").strip()
            if not use_key:
                return {"ok": False, "model": mid, "latency_ms": 0, "models": [], "message": "尚未輸入 OpenAI ChatGPT API Key"}
            base = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1").rstrip("/")
            st, body = await http_json(f"{base}/models", headers={"Authorization": f"Bearer {use_key}"}, timeout=8.0)
            lat = round((time.perf_counter() - t0) * 1000)
            if st == 200 and isinstance(body, dict):
                raw = [m.get("id") for m in (body.get("data") or []) if m.get("id")]
                models = _filter_and_sort_ai_models("chatgpt", raw)
                return {
                    "ok": True, "model": mid, "latency_ms": lat,
                    "models": models, "models_count": len(models),
                    "recommended_model": models[0] if models else "",
                    "message": f"OpenAI ChatGPT 驗證成功（偵測到 {len(models)} 個可用分析模型）",
                }
            return {"ok": False, "model": mid, "latency_ms": lat, "models": [], "message": _err_msg(st, body, use_key)}

        return {"ok": False, "model": mid, "latency_ms": 0, "models": [], "message": f"不支援的模型：{mid}"}
    except Exception as exc:
        lat = round((time.perf_counter() - t0) * 1000)
        return {"ok": False, "model": mid, "latency_ms": lat, "models": [], "message": redact_secrets(str(exc), [key])[:140]}


@app.post("/api/v1/ai/test-connection")
@app.post("/api/v1/ai/models")
async def test_single_ai_endpoint(req: AITestRequest):
    return await _test_single_ai_connection(req.model, req.api_key, req.model_options)


@app.post("/api/v1/ai/test-all")
@app.post("/api/v1/ai/discover-models")
async def test_all_ai_endpoints(req: AITestAllRequest):
    results = await asyncio.gather(
        *(
            _test_single_ai_connection(m, req.api_keys.get(m, ""), req.model_options)
            for m in req.models
        )
    )
    ok_count = sum(1 for r in results if r.get("ok"))
    return {
        "timestamp": tw_now_str(),
        "ok_count": ok_count,
        "total": len(results),
        "results": results,
    }


@app.post("/api/v1/ai/analyze")
async def analyze_multi_ai(req: AIAnalyzeRequest):
    mode_in = (req.focus_mode or "AUTO").strip().upper()
    focus = (req.custom_prompt or "").strip()
    extracted_from_prompt = _extract_tw_symbols_from_prompt(focus, limit=8)

    # Deduplicate explicit target_symbols + prompt-extracted symbols
    explicit_targets: list[str] = []
    for raw_s in (req.target_symbols or []):
        s = str(raw_s).strip().upper()
        if s and s not in explicit_targets:
            explicit_targets.append(s)
    for s in extracted_from_prompt:
        if s not in explicit_targets:
            explicit_targets.append(s)
    explicit_targets = explicit_targets[:8]

    # Map any pre-supplied snapshots by symbol
    pre_map: dict[str, dict[str, Any]] = {}
    for snap in (req.multi_snapshots or []):
        if isinstance(snap, dict) and snap.get("symbol") and snap.get("price") is not None:
            pre_map[str(snap["symbol"]).strip().upper()] = snap
    if req.symbol and isinstance(req.market_snapshot, dict) and req.market_snapshot.get("price") is not None:
        pre_map.setdefault(
            req.symbol.strip().upper(),
            {"symbol": req.symbol.strip().upper(), "stock_name": req.stock_name or req.symbol, **req.market_snapshot},
        )

    resolved_mode = "SINGLE"
    resolved_targets: list[dict[str, Any]] = []

    async def _resolve_symbols(syms: list[str]) -> list[dict[str, Any]]:
        use_uniform_daily = len(syms) >= 2
        tasks = []
        for s in syms:
            if not use_uniform_daily and s in pre_map:
                tasks.append(asyncio.sleep(0, result=pre_map[s]))
            else:
                tasks.append(_fetch_live_snapshot_for_ai(s))
        fetched = await asyncio.gather(*tasks)
        out = []
        for s, x in zip(syms, fetched):
            if isinstance(x, dict) and x.get("price") is not None:
                out.append(x)
            elif s in pre_map:
                out.append(pre_map[s])
        return out

    if mode_in == "ACTIVE":
        active_sid = req.symbol.strip().upper()
        if not active_sid:
            raise HTTPException(status_code=400, detail="請指定分析標的代號")
        resolved_targets = await _resolve_symbols([active_sid])
        if not resolved_targets:
            raise HTTPException(status_code=400, detail=f"無法取得 {active_sid} 真實報價，嚴禁無數據猜測")
        resolved_mode = "SINGLE"
    elif mode_in == "WATCHLIST":
        wl_syms = explicit_targets or list(pre_map.keys())
        if not wl_syms:
            raise HTTPException(status_code=400, detail="自選股清單尚無有效標的")
        resolved_targets = await _resolve_symbols(wl_syms[:8])
        if not resolved_targets:
            raise HTTPException(status_code=400, detail="無法取得自選股真實報價")
        resolved_mode = "MULTI_TARGETS" if len(resolved_targets) >= 2 else "SINGLE"
    elif mode_in == "CUSTOM_ONLY":
        if not focus and not explicit_targets:
            raise HTTPException(status_code=400, detail="請輸入自訂分析焦點內容")
        if explicit_targets:
            resolved_targets = await _resolve_symbols(explicit_targets)
            resolved_mode = "MULTI_TARGETS" if len(resolved_targets) >= 2 else ("SINGLE" if len(resolved_targets) == 1 else "CUSTOM_FOCUS")
        else:
            taiex = await _fetch_live_snapshot_for_ai("TAIEX")
            resolved_targets = [taiex] if taiex else []
            resolved_mode = "CUSTOM_FOCUS"
    else:
        # AUTO mode:
        # 1) If prompt mentions specific stock codes (e.g. 6213, 2368, 3037, 3189) -> fetch ONLY those real quotes!
        if explicit_targets:
            resolved_targets = await _resolve_symbols(explicit_targets)
            if not resolved_targets:
                raise HTTPException(
                    status_code=400,
                    detail=f"查無焦點標的 {', '.join(explicit_targets)} 之真實報價，請確認台股代號是否正確",
                )
            resolved_mode = "MULTI_TARGETS" if len(resolved_targets) >= 2 else "SINGLE"
        # 2) If custom_prompt has no stock codes AND does NOT ask about "目前標的/這檔" -> Custom Focus mode (don't lock onto 2330!)
        elif focus and not _ACTIVE_HINT_RE.search(focus):
            taiex = await _fetch_live_snapshot_for_ai("TAIEX")
            resolved_targets = [taiex] if taiex else []
            resolved_mode = "CUSTOM_FOCUS"
        # 3) Empty prompt or prompt explicitly referencing current stock -> analyze active symbol
        else:
            active_sid = req.symbol.strip().upper()
            if not active_sid:
                raise HTTPException(status_code=400, detail="缺少分析標的或自訂分析焦點")
            resolved_targets = await _resolve_symbols([active_sid])
            if not resolved_targets:
                raise HTTPException(status_code=400, detail="缺少真實報價，嚴禁在無真實數據時進行 AI 猜測分析")
            resolved_mode = "SINGLE"

    results = await asyncio.gather(
        *(
            _call_single_ai(m, req, resolved_mode=resolved_mode, resolved_targets=resolved_targets)
            for m in req.models
        )
    )
    primary_sym = resolved_targets[0]["symbol"] if len(resolved_targets) == 1 else (
        "MULTI" if resolved_mode == "MULTI_TARGETS" else "CUSTOM"
    )
    primary_name = resolved_targets[0]["stock_name"] if len(resolved_targets) == 1 else (
        "、".join(f"{t['symbol']} {t.get('stock_name', '')}".strip() for t in resolved_targets[:4]) + ("…" if len(resolved_targets) > 4 else "")
        if resolved_mode == "MULTI_TARGETS"
        else "自訂焦點整合解析"
    )
    return {
        "symbol": primary_sym,
        "stock_name": primary_name,
        "analyzed_mode": resolved_mode,
        "analyzed_targets": resolved_targets,
        "focus_prompt": focus,
        "timestamp": tw_now_str(),
        "results": results,
    }


# ---------------------------------------------------------------------------
# 2.5 Real-Time Price Detection + Price-Volume Multi-Agent Orchestrator
# ---------------------------------------------------------------------------
class MultiAgentRequest(BaseModel):
    symbol: str
    stock_name: str = ""
    yahoo_symbol: str = ""
    intended_side: str = "AUTO"  # BUY | SELL | AUTO
    intended_price: Optional[float] = None
    max_slippage_pct: float = 1.0
    tech_switches: dict[str, bool] = Field(
        default_factory=lambda: {
            "ma": True,
            "macd": True,
            "kdj": True,
            "rsi": True,
            "obv": True,
            "bollinger": True,
            "vwap": True,
            "momentum": True,
            "inst": True,
            "rs": True,
        }
    )
    api_keys: dict[str, str] = Field(default_factory=dict)
    model_options: dict[str, str] = Field(default_factory=dict)
    routing_policy: str = "AUTO"  # AUTO | SPEED_FIRST | LOCAL_FIRST | DEEP_REASONING
    quant_lab_signal: Optional[dict[str, Any]] = None


def _calc_ema_list(vals: list[float], period: int) -> list[float]:
    if not vals:
        return []
    k = 2.0 / (period + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1.0 - k))
    return out


async def _fetch_institutional_summary(stock_id: str) -> Optional[dict[str, Any]]:
    sid = stock_id.strip().upper()
    if not sid.isdigit():
        return None
    cache_key = f"inst_chip:{sid}"
    cached = cache_get(cache_key, 900)
    if cached is not None:
        return cached
    try:
        start_d = (datetime.now(TW_TZ) - timedelta(days=20)).strftime("%Y-%m-%d")
        params = {
            "dataset": "TaiwanStockInstitutionalInvestorsBuySell",
            "data_id": sid,
            "start_date": start_d,
        }
        use_token = next(_token_cycle) if _token_cycle else ""
        if use_token:
            params["token"] = use_token
        url = "https://api.finmindtrade.com/api/v4/data?" + urllib.parse.urlencode(params)
        st, body = await http_json(url, timeout=7.0)
        if st != 200 or not isinstance(body, dict):
            return None
        rows = body.get("data") or []
        if not rows:
            return None
        by_date: dict[str, int] = {}
        for r in rows:
            dt = str(r.get("date") or "")
            net = int(r.get("buy") or 0) - int(r.get("sell") or 0)
            by_date[dt] = by_date.get(dt, 0) + net
        sorted_days = sorted(by_date.items())
        recent = sorted_days[-5:]
        net_3d_lots = round(sum(v for _, v in recent[-3:]) / 1000.0, 1)
        latest_lots = round(recent[-1][1] / 1000.0, 1)
        streak = 0
        if recent[-1][1] > 0:
            for _, v in reversed(recent):
                if v > 0:
                    streak += 1
                else:
                    break
        elif recent[-1][1] < 0:
            for _, v in reversed(recent):
                if v < 0:
                    streak -= 1
                else:
                    break
        res = {
            "last_date": recent[-1][0],
            "latest_lots": latest_lots,
            "net_3d_lots": net_3d_lots,
            "streak_days": streak,
        }
        cache_set(cache_key, res)
        return res
    except Exception:
        return None


async def _fetch_taiex_benchmark_return() -> Optional[float]:
    cache_key = "taiex_ret_20d"
    cached = cache_get(cache_key, 900)
    if cached is not None:
        return cached
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ETWII?interval=1d&range=1mo"
        st, body = await http_json(url, timeout=6.0)
        if st == 200 and isinstance(body, dict) and (body.get("chart", {}).get("result") or []):
            q = (body["chart"]["result"][0].get("indicators", {}).get("quote") or [{}])[0]
            closes = [float(c) for c in (q.get("close") or []) if c is not None]
            if len(closes) >= 2:
                base = closes[max(0, len(closes) - 20)]
                ret = round(((closes[-1] - base) / base) * 100.0, 2) if base > 0 else 0.0
                cache_set(cache_key, ret)
                return ret
    except Exception:
        pass
    return None


def _evaluate_v01_indicators(
    candles: list[dict[str, Any]],
    switches: dict[str, bool],
    inst_info: Optional[dict[str, Any]] = None,
    taiex_ret: Optional[float] = None,
    quant_lab_signal: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    closes = [float(c["close"]) for c in candles]
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    vols = [float(c.get("volume") or 0) for c in candles]
    n = len(closes)
    latest_p = closes[-1]

    # Volume Ratio & OBV (handle zero-volume closing auction tick gracefully)
    nz_vols = [v for v in vols if v > 0]
    if len(nz_vols) >= 2:
        last_v = nz_vols[-1]
        prev_v = nz_vols[-6:-1]
        avg_vol5 = sum(prev_v) / max(1, len(prev_v))
        vol_ratio = round(last_v / avg_vol5, 2) if avg_vol5 > 0 else 1.0
    else:
        vol_ratio = 1.0
    obv = 0.0
    obv_series = [0.0]
    for i in range(1, n):
        if closes[i] > closes[i - 1]:
            obv += vols[i]
        elif closes[i] < closes[i - 1]:
            obv -= vols[i]
        obv_series.append(obv)
    obv_rising = obv_series[-1] > obv_series[max(0, n - 5)]

    # EMA 5/12 & MA 5/20
    ema5 = _calc_ema_list(closes, 5)
    ema12 = _calc_ema_list(closes, 12)
    ma5 = sum(closes[-5:]) / min(5, n)
    ma20 = sum(closes[-20:]) / min(20, n)

    # MACD (12, 26, 9)
    ema26 = _calc_ema_list(closes, 26)
    dif = [a - b for a, b in zip(ema12, ema26)]
    dea = _calc_ema_list(dif, 9)
    osc = dif[-1] - dea[-1] if dif else 0.0

    # RSI(14)
    rsi_len = min(14, max(1, n - 1))
    gains = sum(max(0.0, closes[i] - closes[i - 1]) for i in range(n - rsi_len, n))
    losses = sum(max(0.0, closes[i - 1] - closes[i]) for i in range(n - rsi_len, n))
    rsi = 100.0 if losses == 0 else round(100.0 - 100.0 / (1.0 + (gains / rsi_len) / (losses / rsi_len)), 1)

    # KDJ (9, 3, 3)
    k_val, d_val = 50.0, 50.0
    for i in range(n):
        lo9 = min(lows[max(0, i - 8) : i + 1])
        hi9 = max(highs[max(0, i - 8) : i + 1])
        rsv = 50.0 if hi9 == lo9 else ((closes[i] - lo9) / (hi9 - lo9)) * 100.0
        k_val = (2.0 / 3.0) * k_val + (1.0 / 3.0) * rsv
        d_val = (2.0 / 3.0) * d_val + (1.0 / 3.0) * k_val
    j_val = 3.0 * k_val - 2.0 * d_val

    # Bollinger Bands (20, 2)
    win_bb = closes[-20:]
    bb_mid = sum(win_bb) / len(win_bb)
    bb_std = (sum((x - bb_mid) ** 2 for x in win_bb) / len(win_bb)) ** 0.5
    bb_upper = bb_mid + 2.0 * bb_std
    bb_lower = bb_mid - 2.0 * bb_std

    # VWAP
    cum_pv = sum(((c["high"] + c["low"] + c["close"]) / 3.0) * max(1.0, float(c.get("volume") or 1.0)) for c in candles)
    cum_v = sum(max(1.0, float(c.get("volume") or 1.0)) for c in candles)
    vwap = cum_pv / cum_v if cum_v > 0 else latest_p
    vwap_diff = round(((latest_p - vwap) / vwap) * 100.0, 2) if vwap > 0 else 0.0

    # 5/20 Momentum & Session Intraday Volatility
    mom5 = round(((latest_p - closes[max(0, n - 6)]) / closes[max(0, n - 6)]) * 100.0, 2) if n >= 2 else 0.0
    mom20 = round(((latest_p - closes[max(0, n - 20)]) / closes[max(0, n - 20)]) * 100.0, 2) if n >= 2 else 0.0
    sess_hi = max(highs[-18:])
    sess_lo = min(lows[-18:])
    intraday_vol_pct = round(((sess_hi - sess_lo) / max(0.01, sess_lo)) * 100.0, 2)

    # Price-Volume Regime classification
    chg_last = ((closes[-1] - closes[-2]) / closes[-2] * 100.0) if n >= 2 and closes[-2] > 0 else 0.0
    if chg_last > 0.5 and vol_ratio >= 1.5:
        pv_regime = "爆量上攻"
    elif chg_last > 0 and vol_ratio >= 1.0:
        pv_regime = "價漲量增"
    elif chg_last > 0.3 and vol_ratio < 0.85:
        pv_regime = "價漲量縮背離"
    elif chg_last < -0.5 and vol_ratio >= 1.5:
        pv_regime = "爆量殺盤"
    elif chg_last < 0 and vol_ratio < 0.85:
        pv_regime = "量縮回測"
    else:
        pv_regime = "價量盤整"

    # Evaluate enabled v01 technical + chip + RS switches (10 switches total)
    votes = []
    score = 0
    enabled_cnt = 0

    def _add_vote(key: str, label: str, val_str: str, pts: int, note: str):
        nonlocal score, enabled_cnt
        if not switches.get(key, False):
            return
        enabled_cnt += 1
        score += pts
        votes.append({
            "key": key,
            "label": label,
            "value": val_str,
            "points": pts,
            "signal": "BULL" if pts > 0 else ("BEAR" if pts < 0 else "NEUTRAL"),
            "note": note,
        })

    _add_vote(
        "ma", "MA/EMA 均線", f"EMA5={ema5[-1]:.1f} / MA20={ma20:.1f}",
        18 if (ema5[-1] > ema12[-1] and ma5 >= ma20) else (-18 if (ema5[-1] < ema12[-1] and ma5 <= ma20) else 0),
        "多頭均線排列" if ema5[-1] > ema12[-1] else "空頭均線壓制",
    )
    _add_vote(
        "macd", "MACD 動能", f"OSC={osc:+.2f}",
        16 if osc > 0 else (-16 if osc < 0 else 0),
        "柱狀偏多翻紅" if osc > 0 else "柱狀偏空翻綠",
    )
    _add_vote(
        "kdj", "KDJ 隨機指標", f"K={k_val:.1f} / D={d_val:.1f}",
        15 if (k_val > d_val and k_val < 80) else (-15 if (k_val < d_val and k_val > 20) else (12 if k_val <= 22 else -12 if k_val >= 82 else 0)),
        "KD 偏多/低檔金叉" if k_val >= d_val else "KD 偏空/高檔死叉",
    )
    _add_vote(
        "rsi", "RSI(14) 強弱", f"RSI={rsi}",
        15 if rsi <= 32 else (-15 if rsi >= 75 else (8 if rsi > 52 else -8 if rsi < 48 else 0)),
        "超賣反彈區" if rsi <= 32 else ("超買警戒區" if rsi >= 75 else "多空區間"),
    )
    _add_vote(
        "obv", "OBV 能量潮與量比", f"量比 {vol_ratio}x",
        16 if (obv_rising and vol_ratio >= 1.2) else (-16 if (not obv_rising and vol_ratio >= 1.2) else 0),
        f"{pv_regime}（OBV {'上揚' if obv_rising else '走弱'}）",
    )
    _add_vote(
        "bollinger", "布林通道 (20,2σ)", f"下{bb_lower:.1f} / 上{bb_upper:.1f}",
        14 if latest_p <= bb_lower * 1.01 else (-14 if latest_p >= bb_upper * 0.99 else (6 if latest_p > bb_mid else -6)),
        "觸及下軌支撐" if latest_p <= bb_lower * 1.01 else ("觸及上軌壓力" if latest_p >= bb_upper * 0.99 else "中軌附近運行"),
    )
    _add_vote(
        "vwap", "VWAP 加權均價", f"{vwap_diff:+.2f}%",
        14 if vwap_diff > 0.2 else (-14 if vwap_diff < -0.2 else 0),
        "站上機構均價" if vwap_diff > 0 else "跌破機構均價",
    )
    _add_vote(
        "momentum", "5/20日動能輪動", f"5D:{mom5:+.1f}% / 20D:{mom20:+.1f}%",
        12 if (mom5 > 0 and mom20 > 0) else (-12 if (mom5 < 0 and mom20 < 0) else 0),
        "短中期動能共振向上" if (mom5 > 0 and mom20 > 0) else ("短中期動能偏弱" if (mom5 < 0 and mom20 < 0) else "動能分歧"),
    )
    if inst_info:
        net3 = float(inst_info.get("net_3d_lots") or 0)
        stk = int(inst_info.get("streak_days") or 0)
        inst_pts = 15 if (net3 > 0 and stk >= 1) else (-15 if (net3 < 0 and stk <= -1) else (8 if net3 > 0 else -8 if net3 < 0 else 0))
        _add_vote(
            "inst", "三大法人籌碼", f"3日{net3:+.0f}張 / 連{abs(stk)}日{'買' if stk > 0 else '賣' if stk < 0 else '平'}",
            inst_pts,
            f"法人近期偏多（{inst_info.get('last_date')}）" if inst_pts > 0 else (f"法人近期偏空（{inst_info.get('last_date')}）" if inst_pts < 0 else "法人籌碼中性"),
        )
    else:
        _add_vote("inst", "三大法人籌碼", "盤後更新/無資料", 0, "FinMind 三大法人盤後 18:00 後更新")

    if taiex_ret is not None:
        rs_diff = round(mom20 - taiex_ret, 2)
        rs_pts = 14 if rs_diff >= 1.5 else (-14 if rs_diff <= -1.5 else (6 if rs_diff > 0.3 else -6 if rs_diff < -0.3 else 0))
        _add_vote(
            "rs", "相對大盤 RS 強弱", f"RS {rs_diff:+.1f}% (大盤 {taiex_ret:+.1f}%)",
            rs_pts,
            "跑贏加權指數（強勢領漲）" if rs_pts > 0 else ("弱於加權指數（相對偏弱）" if rs_pts < 0 else "與大盤同步"),
        )
    else:
        _add_vote("rs", "相對大盤 RS 強弱", "大盤基準待同步", 0, "暫無加權指數基準")

    # Optional bridge vote from linked Quant Strategy Lab strategy
    if isinstance(quant_lab_signal, dict) and quant_lab_signal.get("name"):
        ql_sig = str(quant_lab_signal.get("signal") or "FLAT").upper()
        ql_pts = 16 if ql_sig == "BUY" else (-16 if ql_sig == "SELL" else (6 if ql_sig == "HOLD" else 0))
        ql_sharpe = quant_lab_signal.get("sharpe")
        ql_win = quant_lab_signal.get("win_rate")
        ql_desc = f"{ql_sig}" + (f" · 夏普 {ql_sharpe}" if ql_sharpe is not None else "") + (f" · 勝率 {ql_win}%" if ql_win is not None else "")
        enabled_cnt += 1
        score += ql_pts
        votes.append({
            "key": "quant_lab",
            "label": f"量化實驗室：{quant_lab_signal.get('name')}",
            "value": ql_desc,
            "points": ql_pts,
            "signal": "BULL" if ql_pts > 0 else ("BEAR" if ql_pts < 0 else "NEUTRAL"),
            "note": f"已連結量化策略最新日線訊號（{quant_lab_signal.get('last_date') or '最新'}）",
        })

    norm_score = max(-100, min(100, round(score * (8 / max(1, enabled_cnt))))) if enabled_cnt > 0 else 0
    tech_signal = "BUY" if norm_score >= 25 else ("SELL" if norm_score <= -25 else "HOLD")

    return {
        "ema5": round(ema5[-1], 2),
        "ema12": round(ema12[-1], 2),
        "macd_osc": round(osc, 2),
        "kd": f"{k_val:.1f}/{d_val:.1f}",
        "rsi": rsi,
        "vwap": round(vwap, 2),
        "vwap_diff": vwap_diff,
        "volume_ratio": vol_ratio,
        "obv_rising": obv_rising,
        "intraday_vol_pct": intraday_vol_pct,
        "pv_regime": pv_regime,
        "mom5": mom5,
        "mom20": mom20,
        "inst_info": inst_info,
        "taiex_ret": taiex_ret,
        "enabled_count": enabled_cnt,
        "tech_score": norm_score,
        "tech_signal": tech_signal,
        "votes": votes,
    }


def _select_optimal_model_order(
    pv_regime: str,
    vol_ratio: float,
    intraday_vol_pct: float,
    policy: str,
    api_keys: dict[str, str],
    model_options: dict[str, str],
) -> tuple[list[str], str]:
    policy_u = (policy or "AUTO").upper()
    if policy_u == "LOCAL_FIRST":
        order = ["ollama", "groq", "gemini", "claude", "grok", "chatgpt"]
        reason = "使用者指定地端隱私優先 → 優先切換 Ollama 地端模型"
    elif policy_u == "SPEED_FIRST":
        order = ["groq", "ollama", "gemini", "grok", "chatgpt", "claude"]
        reason = "使用者指定極速低延遲優先 → 優先切換 Groq 毫秒推理"
    elif policy_u == "DEEP_REASONING":
        order = ["claude", "gemini", "groq", "ollama", "chatgpt", "grok"]
        reason = "使用者指定深度邏輯推理 → 優先切換 Claude / Gemini"
    else:
        if vol_ratio >= 1.5 or intraday_vol_pct >= 2.5:
            order = ["groq", "ollama", "grok", "gemini", "chatgpt", "claude"]
            reason = f"偵測到高波動／放量（{pv_regime}，量比 {vol_ratio}x、振幅 {intraday_vol_pct}%）→ 自動切換極速模型 Groq / Ollama"
        elif "背離" in pv_regime:
            order = ["claude", "gemini", "ollama", "groq", "chatgpt", "grok"]
            reason = f"偵測到「{pv_regime}」風險結構 → 自動切換深度風控模型 Claude / Gemini / Ollama"
        elif pv_regime in ("爆量上攻", "價漲量增"):
            order = ["grok", "groq", "gemini", "ollama", "chatgpt", "claude"]
            reason = f"偵測到多頭動能（{pv_regime}）→ 自動切換動能趨勢模型 Grok / Groq"
        else:
            order = ["ollama", "groq", "gemini", "chatgpt", "claude", "grok"]
            reason = f"常態整理格局（{pv_regime}）→ 自動優先地端 Ollama / 經濟高速模型 Groq・Gemini"

    available = []
    for m in order:
        if m == "ollama" and (model_options.get("ollama_url") or os.getenv("OLLAMA_BASE_URL")):
            available.append(m)
        elif m == "groq" and (api_keys.get("groq") or os.getenv("GROQ_API_KEY")):
            available.append(m)
        elif m == "grok" and (api_keys.get("grok") or os.getenv("XAI_API_KEY")):
            available.append(m)
        elif m == "gemini" and (api_keys.get("gemini") or os.getenv("GEMINI_API_KEY")):
            available.append(m)
        elif m == "claude" and (api_keys.get("claude") or os.getenv("ANTHROPIC_API_KEY")):
            available.append(m)
        elif m == "chatgpt" and (api_keys.get("chatgpt") or os.getenv("OPENAI_API_KEY")):
            available.append(m)

    return available, reason


@app.post("/api/v1/agent/orchestrate")
async def orchestrate_multi_agent(req: MultiAgentRequest):
    t0 = time.perf_counter()
    sid = req.symbol.strip().upper()

    # Step 1: Real-time live quote detection supporting both TWSE (.TW) and TPEx (.TWO)
    yh_candidates = []
    if req.yahoo_symbol.strip():
        yh_candidates.append(req.yahoo_symbol.strip())
    if sid.isdigit():
        if f"{sid}.TW" not in yh_candidates:
            yh_candidates.append(f"{sid}.TW")
        if f"{sid}.TWO" not in yh_candidates:
            yh_candidates.append(f"{sid}.TWO")
    elif not yh_candidates:
        yh_candidates.append(sid)

    live_data = None
    live_source = ""
    for yh_sym in yh_candidates:
        try:
            encoded = urllib.parse.quote(yh_sym, safe="^.-=")
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?interval=15m&range=5d"
            st, body = await http_json(url, timeout=7.0)
            if st == 200 and isinstance(body, dict) and (body.get("chart", {}).get("result") or []):
                res = body["chart"]["result"][0]
                meta = res.get("meta", {})
                q = (res.get("indicators", {}).get("quote") or [{}])[0]
                ts_list = res.get("timestamp") or []
                candles = []
                for i, ts in enumerate(ts_list):
                    c = (q.get("close") or [None])[i] if i < len(q.get("close") or []) else None
                    if c is None:
                        continue
                    candles.append({
                        "open": round(float((q.get("open") or [c])[i] or c), 2),
                        "high": round(float((q.get("high") or [c])[i] or c), 2),
                        "low": round(float((q.get("low") or [c])[i] or c), 2),
                        "close": round(float(c), 2),
                        "volume": int((q.get("volume") or [0])[i] or 0),
                    })
                if candles:
                    live_p = round(float(meta.get("regularMarketPrice") or candles[-1]["close"]), 2)
                    prev_c = float(meta.get("chartPreviousClose") or candles[0]["open"])
                    mkt_tag = "TPEx上櫃" if yh_sym.endswith(".TWO") else "TWSE上市"
                    live_data = {
                        "price": live_p,
                        "open": round(float(meta.get("regularMarketOpen") or candles[-1]["open"]), 2),
                        "high": round(float(meta.get("regularMarketDayHigh") or max(x["high"] for x in candles[-20:])), 2),
                        "low": round(float(meta.get("regularMarketDayLow") or min(x["low"] for x in candles[-20:])), 2),
                        "volume": int(meta.get("regularMarketVolume") or candles[-1]["volume"]),
                        "change_pct": round(((live_p - prev_c) / prev_c) * 100.0, 2) if prev_c > 0 else 0.0,
                        "last_date": tw_now_str()[:16],
                        "candles": candles[-40:],
                    }
                    live_source = f"Yahoo Finance 即時偵測 ({yh_sym} · {mkt_tag})"
                    break
        except Exception:
            continue

    if not live_data:
        fm = await get_finmind_market_data(sid, dataset="TaiwanFuturesDaily" if sid == "TXF" else "TaiwanStockPrice", days=45)
        prev_c = float(fm.get("prev_close") or fm["open"] or fm["price"])
        live_data = {
            "price": fm["price"],
            "open": fm["open"],
            "high": fm["high"],
            "low": fm["low"],
            "volume": fm["volume"],
            "change_pct": round(((fm["price"] - prev_c) / prev_c) * 100.0, 2) if prev_c > 0 else 0.0,
            "last_date": fm["last_date"],
            "candles": fm["candles"],
        }
        live_source = f"FinMind 真實偵測 ({sid})"

    # Step 2: Parallel fetch real v01 chip (FinMind institutional) + TAIEX RS benchmark + evaluate switches
    inst_task = _fetch_institutional_summary(sid) if req.tech_switches.get("inst", False) else asyncio.sleep(0, result=None)
    rs_task = _fetch_taiex_benchmark_return() if req.tech_switches.get("rs", False) else asyncio.sleep(0, result=None)
    inst_info, taiex_ret = await asyncio.gather(inst_task, rs_task)
    ql_sig = req.quant_lab_signal if (isinstance(req.quant_lab_signal, dict) and str(req.quant_lab_signal.get("symbol") or sid).upper() == sid) else None
    tech = _evaluate_v01_indicators(live_data["candles"], req.tech_switches, inst_info=inst_info, taiex_ret=taiex_ret, quant_lab_signal=ql_sig)

    # Step 3: Risk Gatekeeper Agent (slippage + kill switch + extremes)
    slippage_pct = 0.0
    if req.intended_price and req.intended_price > 0:
        slippage_pct = round(((live_data["price"] - req.intended_price) / req.intended_price) * 100.0, 2)
    risk_warnings = []
    if KILL_SWITCH_ACTIVE:
        risk_warnings.append("緊急熔斷啟動中，禁止任何自動或手動下單")
    if abs(slippage_pct) > req.max_slippage_pct:
        risk_warnings.append(f"即時滑價 {slippage_pct:+.2f}% 超過容忍上限 ±{req.max_slippage_pct}%")
    if req.intended_side.upper() == "BUY" and tech["rsi"] >= 76:
        risk_warnings.append(f"RSI(14)={tech['rsi']} 嚴重超買，追高風險偏高")
    if req.intended_side.upper() == "SELL" and tech["rsi"] <= 24:
        risk_warnings.append(f"RSI(14)={tech['rsi']} 嚴重超賣，殺低風險偏高")
    risk_approved = len(risk_warnings) == 0

    # Step 4: Adaptive Model Router Agent (auto-select optimal model & execute with failover)
    candidate_models, route_reason = _select_optimal_model_order(
        tech["pv_regime"],
        tech["volume_ratio"],
        tech["intraday_vol_pct"],
        req.routing_policy,
        req.api_keys,
        req.model_options,
    )

    ai_snapshot = {
        "source": live_source,
        "last_date": live_data["last_date"],
        "open": live_data["open"],
        "high": live_data["high"],
        "low": live_data["low"],
        "price": live_data["price"],
        "volume": live_data["volume"],
        "change_pct": live_data["change_pct"],
        "ema5": tech["ema5"],
        "ema12": tech["ema12"],
        "macd_osc": tech["macd_osc"],
        "kd": tech["kd"],
        "rsi": tech["rsi"],
        "vwap_diff": tech["vwap_diff"],
        "volume_ratio": tech["volume_ratio"],
    }

    selected_model = "local_quant_agent"
    selected_model_name = "本地多 Agent 量化決策引擎（未設定外部/Ollama模型）"
    ai_decision = None
    failover_log = []

    for cand in candidate_models:
        ai_req = AIAnalyzeRequest(
            symbol=sid,
            stock_name=req.stock_name or sid,
            market_snapshot=ai_snapshot,
            models=[cand],
            api_keys=req.api_keys,
            model_options=req.model_options,
            custom_prompt=(
                f"價量Agent判讀：{tech['pv_regime']}（量比{tech['volume_ratio']}x、VWAP乖離{tech['vwap_diff']:+.2f}%）；"
                f"已啟用 {tech['enabled_count']} 項台股技術開關綜合評分：{tech['tech_score']}（建議 {tech['tech_signal']}）；"
                f"預定動作：{req.intended_side}。請給出最終下單判定。"
            ),
        )
        res = await _call_single_ai(cand, ai_req)
        if res.get("connected"):
            selected_model = cand
            selected_model_name = f"{cand.upper()} ({res.get('model_name', cand)})"
            ai_decision = res
            break
        failover_log.append(f"{cand}: {res.get('error', '不可用')}")

    if not ai_decision:
        verdict = "BULL" if tech["tech_signal"] == "BUY" else ("BEAR" if tech["tech_signal"] == "SELL" else "NEUTRAL")
        conf = min(92, max(48, 52 + abs(tech["tech_score"]) // 2))
        ai_decision = {
            "model": "local_quant_agent",
            "connected": False,
            "verdict": verdict,
            "confidence": conf,
            "summary": f"實時偵測價 {live_data['price']}（{tech['pv_regime']}，量比 {tech['volume_ratio']}x），已啟用 {tech['enabled_count']} 項台股技術開關總評 {tech['tech_score']} 分，建議 {tech['tech_signal']}。",
            "key_levels": f"支撐 {live_data['low']} ｜ 壓力 {live_data['high']}",
            "risk_note": "；".join(risk_warnings) if risk_warnings else "滑價與風控檢核通過",
        }

    # Combine Strategy + AI Decision into Final Action
    ai_v = ai_decision["verdict"]
    side_u = req.intended_side.upper()
    if side_u in ("BUY", "SELL"):
        opposes = (side_u == "BUY" and tech["tech_signal"] == "SELL" and ai_v == "BEAR") or (
            side_u == "SELL" and tech["tech_signal"] == "BUY" and ai_v == "BULL"
        )
        final_action = "WARN_CONFLICT" if opposes else side_u
    elif tech["tech_signal"] == "BUY" and ai_v == "BULL":
        final_action = "BUY"
    elif tech["tech_signal"] == "SELL" and ai_v == "BEAR":
        final_action = "SELL"
    else:
        final_action = "HOLD"

    return {
        "timestamp": tw_now_str(),
        "latency_ms": round((time.perf_counter() - t0) * 1000),
        "symbol": sid,
        "stock_name": req.stock_name or sid,
        "live_quote": {
            "price": live_data["price"],
            "open": live_data["open"],
            "high": live_data["high"],
            "low": live_data["low"],
            "volume": live_data["volume"],
            "change_pct": live_data["change_pct"],
            "source": live_source,
            "slippage_pct": slippage_pct,
        },
        "agents": {
            "price_volume": {
                "regime": tech["pv_regime"],
                "volume_ratio": tech["volume_ratio"],
                "obv_rising": tech["obv_rising"],
                "intraday_vol_pct": tech["intraday_vol_pct"],
                "vwap": tech["vwap"],
                "vwap_diff": tech["vwap_diff"],
            },
            "technical_strategy": {
                "enabled_count": tech["enabled_count"],
                "score": tech["tech_score"],
                "signal": tech["tech_signal"],
                "votes": tech["votes"],
            },
            "risk_gatekeeper": {
                "approved": risk_approved,
                "slippage_pct": slippage_pct,
                "max_slippage_pct": req.max_slippage_pct,
                "warnings": risk_warnings,
            },
            "model_router": {
                "policy": req.routing_policy,
                "selected_model": selected_model,
                "selected_model_name": selected_model_name,
                "reason": route_reason,
                "failover_log": failover_log,
                "decision": ai_decision,
            },
        },
        "final_action": final_action,
        "risk_approved": risk_approved,
    }


# ---------------------------------------------------------------------------
# 3. LINE Bot & Telegram Bot Secure Auto-Binding Notification Proxy
#    - Token-only setup: validate via getMe, encrypt at rest, issue one-time setup_code
#    - Creator sends `/start <setup_code>` -> poll getUpdates -> bind owner_chat_id -> invalidate setup_code
#    - Strict zero-leakage: token never returned, never logged, masked in UI, redacted in errors
# ---------------------------------------------------------------------------
class TelegramSaveTokenRequest(BaseModel):
    bot_token: str


class NotifyRequest(BaseModel):
    text: str
    line_token: str = ""
    line_target_id: str = ""
    channels: list[str] = Field(default_factory=lambda: ["line", "telegram"])
    is_html: bool = False


def _tg_get_binding_row() -> Optional[dict[str, Any]]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM telegram_bindings WHERE id = 1").fetchone()
    conn.close()
    return dict(row) if row else None


def _tg_issue_new_setup_code() -> tuple[str, int]:
    code = secrets.token_hex(4).upper()
    expires_at = int(time.time()) + TG_SETUP_TTL_SEC
    return code, expires_at


def _tg_build_public_status(row: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not row:
        return {
            "configured": False,
            "bound": False,
            "status": "unconfigured",
            "status_text": "尚未設定 Bot Token",
            "token_masked": "",
            "bot_username": "",
            "bot_name": "",
            "owner_label": "",
            "setup_code": "",
            "setup_command": "",
            "setup_deep_link": "",
            "setup_expires_in_sec": 0,
        }

    is_bound = bool(row.get("bound")) and bool(row.get("owner_chat_id"))
    bot_user = str(row.get("bot_username") or "")
    if is_bound:
        return {
            "configured": True,
            "bound": True,
            "status": "bound",
            "status_text": "已綁定通知目標",
            "token_masked": str(row.get("token_masked") or ""),
            "bot_username": bot_user,
            "bot_name": str(row.get("bot_name") or ""),
            "owner_label": str(row.get("owner_label") or "已綁定建立者私訊"),
            "setup_code": "",
            "setup_command": "",
            "setup_deep_link": "",
            "setup_expires_in_sec": 0,
        }

    code = str(row.get("setup_code") or "")
    exp_at = int(row.get("setup_code_expires_at") or 0)
    rem = max(0, exp_at - int(time.time()))
    if not code or rem == 0:
        code, exp_at = _tg_issue_new_setup_code()
        rem = max(0, exp_at - int(time.time()))
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "UPDATE telegram_bindings SET setup_code=?, setup_code_expires_at=?, updated_at=? WHERE id=1",
            (code, exp_at, tw_now_str()),
        )
        conn.commit()
        conn.close()

    deep_link = f"https://t.me/{bot_user}?start={code}" if bot_user else ""
    return {
        "configured": True,
        "bound": False,
        "status": "awaiting_start",
        "status_text": "等待建立者對 Bot 傳送 /start",
        "token_masked": str(row.get("token_masked") or ""),
        "bot_username": bot_user,
        "bot_name": str(row.get("bot_name") or ""),
        "owner_label": "",
        "setup_code": code,
        "setup_command": f"/start {code}",
        "setup_deep_link": deep_link,
        "setup_expires_in_sec": rem,
    }


async def _tg_api_call(
    token: str,
    method_name: str,
    http_method: str = "GET",
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[dict[str, Any]] = None,
    timeout: float = 10.0,
) -> tuple[int, Any]:
    url = f"{TELEGRAM_API_BASE}/bot{urllib.parse.quote(token, safe=':')}/{method_name}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        status, body = await http_json(url, method=http_method, json_body=json_body, timeout=timeout)
        if isinstance(body, str):
            body = redact_secrets(body, [token])
        elif isinstance(body, dict) and "description" in body:
            body = {**body, "description": redact_secrets(body["description"], [token])}
        return status, body
    except Exception as exc:
        return 0, {"ok": False, "description": redact_secrets(str(exc), [token])}


async def _send_telegram_bound_message(text: str, is_html: bool = False) -> dict[str, Any]:
    row = _tg_get_binding_row()
    if not row:
        return {
            "ok": False,
            "code": "NOT_CONFIGURED",
            "error": "尚未設定 Telegram Bot Token，請先於設定頁貼上 Bot Token 並儲存驗證。",
        }

    if not row.get("bound") or not row.get("owner_chat_id"):
        pub = _tg_build_public_status(row)
        return {
            "ok": False,
            "code": "NOT_BOUND",
            "error": f"尚未綁定通知目標，禁止推播。請先開啟您的 Bot 傳送 {pub['setup_command']} 完成建立者綁定。",
        }

    try:
        token = decrypt_secret(row["token_enc"])
    except Exception:
        return {
            "ok": False,
            "code": "DECRYPT_ERROR",
            "error": "已儲存的 Bot Token 無法解密，請重新輸入 Bot Token 並驗證。",
        }

    payload_text = text if is_html else format_telegram_html(text)
    status, body = await _tg_api_call(
        token,
        "sendMessage",
        http_method="POST",
        json_body={
            "chat_id": str(row["owner_chat_id"]),
            "text": payload_text,
            "parse_mode": "HTML",
        },
        timeout=10.0,
    )
    ok = 200 <= status < 300 and isinstance(body, dict) and bool(body.get("ok", True))
    if ok:
        return {
            "ok": True,
            "status": status,
            "owner_label": row.get("owner_label") or "已綁定通知目標",
        }
    desc = body.get("description") if isinstance(body, dict) else str(body)
    return {
        "ok": False,
        "status": status,
        "error": redact_secrets(desc or f"Telegram sendMessage HTTP {status}", [token]),
    }


@app.get("/api/v1/telegram/status")
async def get_telegram_binding_status():
    return _tg_build_public_status(_tg_get_binding_row())


@app.post("/api/v1/telegram/save-token")
async def save_telegram_bot_token(req: TelegramSaveTokenRequest):
    token = (req.bot_token or "").strip()
    if not token or ":" not in token or len(token) < 20:
        raise HTTPException(
            status_code=400,
            detail="Bot Token 格式不正確，請貼上 @BotFather 提供的完整 Token（例如 123456789:AAH...）。",
        )

    # 1. Verify token validity via Telegram getMe
    status, body = await _tg_api_call(token, "getMe", http_method="GET", timeout=10.0)
    if status != 200 or not isinstance(body, dict) or not body.get("ok"):
        err_desc = body.get("description") if isinstance(body, dict) else f"HTTP {status}"
        raise HTTPException(
            status_code=400,
            detail=f"Telegram Bot Token 驗證失敗：{redact_secrets(err_desc, [token])}",
        )

    bot_info = body.get("result") or {}
    bot_id = str(bot_info.get("id") or "")
    bot_username = str(bot_info.get("username") or "")
    bot_name = str(bot_info.get("first_name") or bot_username or "Telegram Bot")

    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    token_enc = encrypt_secret(token)
    token_masked = mask_tg_token(token)

    existing = _tg_get_binding_row()
    # Keep existing verified owner binding only if exact same token was already bound
    if existing and existing.get("token_hash") == token_hash and existing.get("bound") and existing.get("owner_chat_id"):
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            """
            UPDATE telegram_bindings
            SET token_enc=?, token_masked=?, bot_id=?, bot_username=?, bot_name=?, updated_at=?
            WHERE id=1
            """,
            (token_enc, token_masked, bot_id, bot_username, bot_name, tw_now_str()),
        )
        conn.commit()
        conn.close()
        return {
            "ok": True,
            "message": "Token 驗證成功，保留既有已綁定通知目標。",
            **_tg_build_public_status(_tg_get_binding_row()),
        }

    # New token or unbound token: issue one-time setup_code and clear any prior owner binding
    last_update_id = 0
    up_st, up_body = await _tg_api_call(
        token,
        "getUpdates",
        http_method="GET",
        params={"limit": 1, "offset": -1, "timeout": 0},
        timeout=8.0,
    )
    if up_st == 200 and isinstance(up_body, dict) and up_body.get("ok"):
        updates = up_body.get("result") or []
        if updates:
            last_update_id = int(updates[-1].get("update_id") or 0)

    setup_code, expires_at = _tg_issue_new_setup_code()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        INSERT INTO telegram_bindings (
            id, token_hash, token_enc, token_masked,
            bot_id, bot_username, bot_name,
            owner_chat_id, owner_label,
            setup_code, setup_code_expires_at, bound, last_update_id, updated_at
        ) VALUES (1, ?, ?, ?, ?, ?, ?, '', '', ?, ?, 0, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            token_hash=excluded.token_hash,
            token_enc=excluded.token_enc,
            token_masked=excluded.token_masked,
            bot_id=excluded.bot_id,
            bot_username=excluded.bot_username,
            bot_name=excluded.bot_name,
            owner_chat_id='',
            owner_label='',
            setup_code=excluded.setup_code,
            setup_code_expires_at=excluded.setup_code_expires_at,
            bound=0,
            last_update_id=excluded.last_update_id,
            updated_at=excluded.updated_at
        """,
        (
            token_hash,
            token_enc,
            token_masked,
            bot_id,
            bot_username,
            bot_name,
            setup_code,
            expires_at,
            last_update_id,
            tw_now_str(),
        ),
    )
    conn.commit()
    conn.close()

    pub = _tg_build_public_status(_tg_get_binding_row())
    return {
        "ok": True,
        "message": f"Bot Token 驗證成功（@{bot_username}），請開啟 Bot 傳送 {pub['setup_command']} 完成建立者綁定。",
        **pub,
    }


@app.post("/api/v1/telegram/poll-bind")
async def poll_telegram_binding():
    row = _tg_get_binding_row()
    if not row:
        return {"ok": False, "newly_bound": False, **_tg_build_public_status(None)}

    if row.get("bound") and row.get("owner_chat_id"):
        return {"ok": True, "newly_bound": False, **_tg_build_public_status(row)}

    # Ensure valid unexpired one-time setup_code
    pub_before = _tg_build_public_status(row)
    row = _tg_get_binding_row() or row
    expected_code = str(row.get("setup_code") or "").strip()
    if not expected_code:
        return {"ok": False, "newly_bound": False, **pub_before}

    try:
        token = decrypt_secret(row["token_enc"])
    except Exception:
        raise HTTPException(status_code=500, detail="已儲存之 Bot Token 解密失敗，請重新儲存。")

    params: dict[str, Any] = {
        "timeout": 0,
        "allowed_updates": json.dumps(["message"]),
    }
    last_uid = int(row.get("last_update_id") or 0)
    if last_uid > 0:
        params["offset"] = last_uid + 1

    status, body = await _tg_api_call(token, "getUpdates", http_method="GET", params=params, timeout=10.0)
    if status != 200 or not isinstance(body, dict) or not body.get("ok"):
        err_desc = body.get("description") if isinstance(body, dict) else f"HTTP {status}"
        return {
            "ok": False,
            "newly_bound": False,
            "error": redact_secrets(err_desc or "輪詢 getUpdates 失敗", [token]),
            **pub_before,
        }

    updates = body.get("result") or []
    max_uid = last_uid
    matched_chat_id = ""
    matched_label = ""
    expected_cmd = f"/start {expected_code}".lower()

    for upd in updates:
        uid = int(upd.get("update_id") or 0)
        if uid > max_uid:
            max_uid = uid
        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        text = str(msg.get("text") or "").strip()
        # Require private direct message + exact one-time setup_code
        if chat.get("type") != "private":
            continue
        if text.lower() == expected_cmd:
            cid = str(chat.get("id") or "").strip()
            if cid:
                matched_chat_id = cid
                uname = str(chat.get("username") or "").strip()
                fname = str(chat.get("first_name") or "").strip()
                tail = cid[-4:] if len(cid) >= 4 else cid
                if uname:
                    matched_label = f"@{uname}（建立者私訊）"
                elif fname:
                    matched_label = f"{fname}（私訊 ••••{tail}）"
                else:
                    matched_label = f"建立者私訊（••••{tail}）"
                break

    conn = sqlite3.connect(DB_PATH)
    if matched_chat_id:
        # Bind owner_chat_id & immediately invalidate one-time setup_code (anti-hijack)
        conn.execute(
            """
            UPDATE telegram_bindings
            SET owner_chat_id=?, owner_label=?, bound=1,
                setup_code='', setup_code_expires_at=0,
                last_update_id=?, updated_at=?
            WHERE id=1 AND bound=0
            """,
            (matched_chat_id, matched_label, max_uid, tw_now_str()),
        )
        conn.commit()
        conn.close()

        # Send immediate HTML welcome confirmation to owner_chat_id
        welcome_html = (
            "✅ <b>SYNAPSE TW 通知目標已綁定</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🤖 Bot：<code>@{html.escape(str(row.get('bot_username') or 'Bot'))}</code>\n"
            "🔐 一次性設定碼已立即失效（每 Token 限 1 預設目標）\n"
            "📈 後續台股交易訊號、多 Agent 警示與成交回報將自動推播至此私訊。"
        )
        await _tg_api_call(
            token,
            "sendMessage",
            http_method="POST",
            json_body={
                "chat_id": matched_chat_id,
                "text": welcome_html,
                "parse_mode": "HTML",
            },
            timeout=10.0,
        )
        return {
            "ok": True,
            "newly_bound": True,
            "message": "通知目標已綁定！已發送確認訊息至建立者 Telegram。",
            **_tg_build_public_status(_tg_get_binding_row()),
        }

    if max_uid > last_uid:
        conn.execute(
            "UPDATE telegram_bindings SET last_update_id=?, updated_at=? WHERE id=1",
            (max_uid, tw_now_str()),
        )
        conn.commit()
    conn.close()

    return {
        "ok": True,
        "newly_bound": False,
        **_tg_build_public_status(_tg_get_binding_row()),
    }


@app.post("/api/v1/telegram/rebind")
async def rebind_telegram_owner():
    row = _tg_get_binding_row()
    if not row:
        raise HTTPException(status_code=400, detail="尚未設定 Bot Token，請先貼上 Bot Token 並儲存。")

    # Advance update offset so any old messages are ignored when rebinding
    last_uid = int(row.get("last_update_id") or 0)
    try:
        token = decrypt_secret(row["token_enc"])
        up_st, up_body = await _tg_api_call(
            token,
            "getUpdates",
            http_method="GET",
            params={"limit": 1, "offset": -1, "timeout": 0},
            timeout=8.0,
        )
        if up_st == 200 and isinstance(up_body, dict) and up_body.get("ok"):
            updates = up_body.get("result") or []
            if updates:
                last_uid = max(last_uid, int(updates[-1].get("update_id") or 0))
    except Exception:
        pass

    new_code, expires_at = _tg_issue_new_setup_code()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        UPDATE telegram_bindings
        SET owner_chat_id='', owner_label='', bound=0,
            setup_code=?, setup_code_expires_at=?,
            last_update_id=?, updated_at=?
        WHERE id=1
        """,
        (new_code, expires_at, last_uid, tw_now_str()),
    )
    conn.commit()
    conn.close()

    pub = _tg_build_public_status(_tg_get_binding_row())
    return {
        "ok": True,
        "message": f"已清除原綁定目標，請開啟 Bot 傳送新一次性指令：{pub['setup_command']}",
        **pub,
    }


@app.post("/api/v1/telegram/test")
async def send_telegram_test_notification():
    row = _tg_get_binding_row()
    if not row:
        raise HTTPException(status_code=400, detail="尚未設定 Bot Token，請先貼上 Bot Token 並驗證。")
    if not row.get("bound") or not row.get("owner_chat_id"):
        pub = _tg_build_public_status(row)
        raise HTTPException(
            status_code=400,
            detail=f"尚未綁定通知目標！請先開啟您的 Telegram Bot 傳送 {pub['setup_command']} 完成綁定後再發送測試通知。",
        )

    test_html = (
        "🔔 <b>SYNAPSE TW 交易推播測試通知</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🕒 時間：<code>{tw_now_str()} (UTC+8)</code>\n"
        f"🤖 模式：<code>{SERVER_TRADING_MODE}</code>\n"
        "📈 範例訊號：🔴 <b>2330 台積電</b> ｜ <b>[買進 BUY]</b>\n"
        "✅ <b>狀態：</b>已成功串接加密儲存之 Bot Token 與預設建立者目標。"
    )
    res = await _send_telegram_bound_message(test_html, is_html=True)
    if not res.get("ok"):
        raise HTTPException(status_code=502, detail=res.get("error") or "Telegram 測試發送失敗")
    return {
        "ok": True,
        "timestamp": tw_now_str(),
        "message": "測試通知已發送至已綁定之建立者 Telegram！",
        "owner_label": res.get("owner_label", ""),
    }


@app.post("/api/v1/telegram/clear")
async def clear_telegram_binding():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM telegram_bindings WHERE id=1")
    conn.commit()
    conn.close()
    return {"ok": True, "message": "已清除加密儲存之 Telegram Bot Token 與綁定設定。", **_tg_build_public_status(None)}


@app.post("/api/v1/notify/broadcast")
async def broadcast_notify(req: NotifyRequest):
    out: dict[str, Any] = {}

    if "line" in req.channels:
        token = req.line_token.strip() or os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
        target = req.line_target_id.strip() or os.getenv("LINE_TARGET_ID", "").strip()
        clean_text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(req.text or "")).strip()
        if len(clean_text) > 4900:
            clean_text = clean_text[:4890] + "\n...(訊息過長已截斷)"
        if not token:
            out["line"] = {"ok": False, "error": "尚未設定 LINE Channel Access Token"}
        elif not clean_text:
            out["line"] = {"ok": False, "error": "推播內容不可為空"}
        elif not target or target.upper() == "BROADCAST":
            # Broadcast to all followers when Target ID is empty or BROADCAST (prevents 400 Invalid 'to')
            status, body = await http_json(
                "https://api.line.me/v2/bot/message/broadcast",
                method="POST",
                headers={"Authorization": f"Bearer {token}"},
                json_body={"messages": [{"type": "text", "text": clean_text}]},
                timeout=10.0,
            )
            err_msg = ""
            if not (200 <= status < 300):
                bmsg = body.get("message", "") if isinstance(body, dict) else str(body)[:160]
                err_msg = f"LINE Broadcast HTTP {status}: {bmsg}"
            out["line"] = {"ok": 200 <= status < 300, "status": status, "error": err_msg, "detail": redact_secrets(body, [token])}
        elif not re.fullmatch(r"[UCR][0-9a-fA-F]{32}", target):
            out["line"] = {
                "ok": False,
                "status": 400,
                "error": "LINE Target ID 格式不符（須為 U/C/R 開頭的 33 碼 User/Group ID；若要群發請清空或填 BROADCAST）",
            }
        else:
            status, body = await http_json(
                "https://api.line.me/v2/bot/message/push",
                method="POST",
                headers={"Authorization": f"Bearer {token}"},
                json_body={"to": target, "messages": [{"type": "text", "text": clean_text}]},
                timeout=10.0,
            )
            err_msg = ""
            if not (200 <= status < 300):
                bmsg = body.get("message", "") if isinstance(body, dict) else str(body)[:160]
                details = body.get("details", []) if isinstance(body, dict) else []
                dtxt = f" ({details[0].get('message', '')})" if details and isinstance(details[0], dict) else ""
                err_msg = f"LINE Push HTTP {status}: {bmsg}{dtxt}"
            out["line"] = {"ok": 200 <= status < 300, "status": status, "error": err_msg, "detail": redact_secrets(body, [token])}

    if "telegram" in req.channels:
        out["telegram"] = await _send_telegram_bound_message(req.text, is_html=req.is_html)

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


def _owner_from_request(request: Optional[Request]) -> str:
    """Resolve unified owner_id (Device Matching) from X-SHL-Owner-Id header; validated format only."""
    if request is None:
        return ""
    raw = (request.headers.get("x-shl-owner-id") or "").strip()
    if raw and re.fullmatch(r"own_[A-Za-z0-9_-]{16,64}", raw):
        return raw
    return ""


@app.post("/api/v1/orders/place")
async def place_order(req: PlaceOrderRequest, request: Request):
    owner_id = _owner_from_request(request)
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
        api_key = _SHIOAJI_CFG.get("api_key") or os.getenv("SHIOAJI_API_KEY", "")
        secret_key = _SHIOAJI_CFG.get("secret_key") or os.getenv("SHIOAJI_SECRET_KEY", "")
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
        (ts_ms, iso_time, order_id, env_mode, symbol, stock_name, side, order_type, tif, qty, unit, price, fee_twd, tax_twd, status, broker_msg, source, owner_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            owner_id,
        ),
    )
    conn.commit()
    conn.close()

    return {
        "ok": True,
        "owner_id": owner_id,
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
async def get_order_history(request: Request, limit: int = 50):
    owner_id = _owner_from_request(request)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if owner_id:
        # Unified owner database: every paired device of the same person sees the same rows
        # (legacy rows created before pairing, owner_id = '', remain visible to the server owner).
        rows = conn.execute(
            "SELECT * FROM trade_orders WHERE owner_id = ? OR owner_id = '' OR owner_id IS NULL ORDER BY ts_ms DESC LIMIT ?",
            (owner_id, max(1, min(limit, 200))),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM trade_orders WHERE owner_id = '' OR owner_id IS NULL ORDER BY ts_ms DESC LIMIT ?",
            (max(1, min(limit, 200)),),
        ).fetchall()
    conn.close()
    return {"owner_id": owner_id, "orders": [dict(r) for r in rows]}


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


# ---------------------------------------------------------------------------
# 6. Zero-Knowledge End-to-End Encrypted Multi-Device Vault Sync
#    (Server NEVER sees plaintext API keys, tokens, holdings, or master password)
# ---------------------------------------------------------------------------
_VAULT_ID_RE = re.compile(r"^[a-fA-F0-9_-]{16,128}$")
_AUTH_HASH_RE = re.compile(r"^[a-fA-F0-9]{32,128}$")


class VaultPushRequest(BaseModel):
    vault_id: str = Field(..., min_length=16, max_length=128)
    auth_hash: str = Field(..., min_length=32, max_length=128)
    payload_json: str = Field(..., min_length=2, max_length=4_000_000)
    device_name: str = Field(default="Unknown Device", max_length=120)
    base_revision: Optional[int] = None


class VaultPullRequest(BaseModel):
    vault_id: str = Field(..., min_length=16, max_length=128)
    auth_hash: str = Field(..., min_length=32, max_length=128)


def _validate_vault_creds(vault_id: str, auth_hash: str) -> tuple[str, str]:
    vid = vault_id.strip()
    ah = auth_hash.strip().lower()
    if not _VAULT_ID_RE.match(vid):
        raise HTTPException(status_code=400, detail="無效的同步空間識別碼（vault_id）")
    if not _AUTH_HASH_RE.match(ah):
        raise HTTPException(status_code=400, detail="無效的同步驗證雜湊（auth_hash）")
    return vid, ah


@app.post("/api/v1/vault/push")
async def vault_e2ee_push(req: VaultPushRequest):
    vid, ah = _validate_vault_creds(req.vault_id, req.auth_hash)
    # Verify payload is valid JSON containing ciphertext envelope (zero-knowledge: never decrypt)
    try:
        parsed = json.loads(req.payload_json)
        if not isinstance(parsed, dict) or not parsed.get("ct") or not parsed.get("iv"):
            raise ValueError("missing encrypted fields")
    except Exception:
        raise HTTPException(status_code=400, detail="同步資料必須為有效的 AES-256-GCM 加密封包")

    now_iso = tw_now_str()
    now_ms = int(time.time() * 1000)
    dev_name = (req.device_name or "Unknown Device").strip()[:120]

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT auth_hash, revision FROM encrypted_vault_sync WHERE vault_id = ?",
            (vid,),
        ).fetchone()
        if row is not None:
            if not hmac.compare_digest(str(row["auth_hash"]), ah):
                raise HTTPException(status_code=403, detail="同步驗證金鑰不符，拒絕寫入此加密保險箱空間")
            new_rev = int(row["revision"]) + 1
            conn.execute(
                """
                UPDATE encrypted_vault_sync
                SET payload_json = ?, revision = ?, device_name = ?, updated_at = ?, updated_ts_ms = ?
                WHERE vault_id = ?
                """,
                (req.payload_json, new_rev, dev_name, now_iso, now_ms, vid),
            )
        else:
            new_rev = 1
            conn.execute(
                """
                INSERT INTO encrypted_vault_sync
                (vault_id, auth_hash, payload_json, revision, device_name, updated_at, updated_ts_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (vid, ah, req.payload_json, new_rev, dev_name, now_iso, now_ms),
            )
        conn.commit()
    finally:
        conn.close()

    return {
        "ok": True,
        "revision": new_rev,
        "device_name": dev_name,
        "updated_at": now_iso,
        "updated_ts_ms": now_ms,
    }


@app.post("/api/v1/vault/pull")
async def vault_e2ee_pull(req: VaultPullRequest):
    vid, ah = _validate_vault_creds(req.vault_id, req.auth_hash)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT auth_hash, payload_json, revision, device_name, updated_at, updated_ts_ms FROM encrypted_vault_sync WHERE vault_id = ?",
            (vid,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return {"ok": True, "exists": False, "revision": 0}

    if not hmac.compare_digest(str(row["auth_hash"]), ah):
        raise HTTPException(status_code=403, detail="同步驗證金鑰不符，無法讀取此加密保險箱")

    return {
        "ok": True,
        "exists": True,
        "revision": int(row["revision"]),
        "payload_json": str(row["payload_json"]),
        "device_name": str(row["device_name"] or ""),
        "updated_at": str(row["updated_at"]),
        "updated_ts_ms": int(row["updated_ts_ms"]),
    }


@app.post("/api/v1/vault/meta")
async def vault_e2ee_meta(req: VaultPullRequest):
    vid, ah = _validate_vault_creds(req.vault_id, req.auth_hash)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT auth_hash, revision, device_name, updated_at, updated_ts_ms FROM encrypted_vault_sync WHERE vault_id = ?",
            (vid,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return {"ok": True, "exists": False, "revision": 0}
    if not hmac.compare_digest(str(row["auth_hash"]), ah):
        raise HTTPException(status_code=403, detail="同步驗證金鑰不符")
    return {
        "ok": True,
        "exists": True,
        "revision": int(row["revision"]),
        "device_name": str(row["device_name"] or ""),
        "updated_at": str(row["updated_at"]),
        "updated_ts_ms": int(row["updated_ts_ms"]),
    }


# ---------------------------------------------------------------------------
# v3.8.0 — Real-Time Public Quote Relay (TWSE/TPEx MIS 五檔即時 · TAIFEX 台指期 · Yahoo)
# 僅允許公開行情主機，回應一律 no-store，盤中「全標的同步真實行情」永不回傳隔日 / 快取舊價。
# ---------------------------------------------------------------------------
PUBLIC_RELAY_HOSTS = {
    "mis.twse.com.tw",
    "mis.taifex.com.tw",
    "query1.finance.yahoo.com",
    "query2.finance.yahoo.com",
}


def _sync_relay_request(url: str, method: str, body: Optional[bytes], content_type: str) -> tuple[int, bytes, str]:
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) SHL-TW/3.8",
        "Accept": "application/json, text/plain, */*",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host == "mis.twse.com.tw":
        headers["Referer"] = "https://mis.twse.com.tw/stock/index.jsp"
    if host == "mis.taifex.com.tw":
        headers["Referer"] = "https://mis.taifex.com.tw/futures/"
    if body is not None:
        headers["Content-Type"] = content_type or "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", "application/json")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), "application/json"


@app.api_route("/api/v1/relay/public", methods=["GET", "POST"])
async def public_quote_relay(request: Request, url: str = ""):
    target = (url or "").strip()
    parsed = urllib.parse.urlparse(target)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in PUBLIC_RELAY_HOSTS:
        raise HTTPException(status_code=403, detail="僅允許公開行情主機（TWSE MIS / TAIFEX MIS / Yahoo Finance）")
    body: Optional[bytes] = None
    if request.method == "POST":
        body = (await request.body())[: 64 * 1024]
    try:
        status, data, ctype = await asyncio.to_thread(
            _sync_relay_request, target, request.method, body, request.headers.get("content-type", "")
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"即時行情來源連線失敗：{exc}")
    return Response(
        content=data,
        status_code=status,
        media_type=(ctype or "application/json").split(";")[0],
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )


def _parse_mis_float(v: Any) -> Optional[float]:
    try:
        if v in (None, "", "-"):
            return None
        return float(str(v).split("_")[0])
    except Exception:
        return None


@app.get("/api/v1/market/realtime")
async def twse_realtime_quotes(symbols: str = "", indices: str = "t00,o00"):
    """
    盤中即時報價（三竹股市同級官方 TWSE/TPEx MIS 來源）：成交價、漲跌、開高低昨收、總量、單量、五檔。
    symbols=2330,6488 （自動同時查詢上市 tse 與上櫃 otc 通道）；indices=t00（加權）,o00（櫃買）。
    """
    syms = [x.strip().upper() for x in symbols.split(",") if re.fullmatch(r"[0-9A-Z]{4,6}", x.strip().upper())][:60]
    idx = [x.strip().lower() for x in indices.split(",") if x.strip().lower() in ("t00", "o00", "t13", "t17")]
    channels: list[str] = []
    for i in idx:
        channels.append(f"{'otc' if i.startswith('o') else 'tse'}_{i}.tw")
    for sy in syms:
        channels.append(f"tse_{sy}.tw")
        channels.append(f"otc_{sy}.tw")
    if not channels:
        raise HTTPException(status_code=400, detail="請提供 symbols 或 indices")
    url = (
        "https://mis.twse.com.tw/stock/api/getStockInfo.jsp?json=1&delay=0&ex_ch="
        + urllib.parse.quote("|".join(channels), safe="|_.")
        + f"&_={int(time.time() * 1000)}"
    )
    status, data, _ = await asyncio.to_thread(_sync_relay_request, url, "GET", None, "")
    if status != 200:
        raise HTTPException(status_code=502, detail=f"TWSE MIS HTTP {status}")
    try:
        payload = json.loads(data.decode("utf-8", errors="replace").strip())
    except Exception:
        raise HTTPException(status_code=502, detail="TWSE MIS 回應格式異常")
    out: dict[str, Any] = {}
    for m in payload.get("msgArray", []) or []:
        code = str(m.get("c", "")).strip()
        if not code:
            continue
        last = _parse_mis_float(m.get("z"))
        bids = [_parse_mis_float(x) for x in str(m.get("b", "")).split("_") if x]
        asks = [_parse_mis_float(x) for x in str(m.get("a", "")).split("_") if x]
        if last is None:
            last = _parse_mis_float(m.get("pz")) or (bids[0] if bids else None) or _parse_mis_float(m.get("y"))
        prev = _parse_mis_float(m.get("y"))
        out[code] = {
            "symbol": code,
            "name": m.get("n", ""),
            "exchange": m.get("ex", ""),
            "price": last,
            "prev_close": prev,
            "change": round(last - prev, 4) if (last is not None and prev) else None,
            "change_pct": round((last - prev) / prev * 100, 2) if (last is not None and prev) else None,
            "open": _parse_mis_float(m.get("o")),
            "high": _parse_mis_float(m.get("h")),
            "low": _parse_mis_float(m.get("l")),
            "volume_lots": _parse_mis_float(m.get("v")),
            "tick_volume": _parse_mis_float(m.get("tv")),
            "bids": bids[:5],
            "bid_sizes": [_parse_mis_float(x) for x in str(m.get("g", "")).split("_") if x][:5],
            "asks": asks[:5],
            "ask_sizes": [_parse_mis_float(x) for x in str(m.get("f", "")).split("_") if x][:5],
            "date": m.get("d", ""),
            "time": m.get("t", ""),
            "tlong": m.get("tlong", ""),
        }
    return JSONResponse(
        {"source": "TWSE/TPEx MIS 官方即時", "server_time": tw_now_str(), "quotes": out},
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# v3.8.0 — Device Matching / Pairing → Unified Owner Database
# 同一人多台裝置（iPhone / iPad / Mac / Windows）以一次性 6 碼配對碼歸屬同一 owner_id，
# 訂單紀錄 (trade_orders) 依 owner_id 共用；E2EE 保險箱同步空間亦自動對齊。
# ---------------------------------------------------------------------------
class DeviceRegisterRequest(BaseModel):
    device_id: str = Field(..., min_length=6, max_length=80)
    device_name: str = ""
    platform: str = ""
    owner_id: str = ""
    owner_label: str = ""


class DevicePairCreateRequest(BaseModel):
    owner_id: str
    device_id: str


class DevicePairClaimRequest(BaseModel):
    code: str
    device_id: str = Field(..., min_length=6, max_length=80)
    device_name: str = ""
    platform: str = ""


def _valid_owner(owner_id: str) -> bool:
    return bool(re.fullmatch(r"own_[A-Za-z0-9_-]{16,64}", owner_id or ""))


def _owner_sync_space(owner_id: str) -> str:
    return "owner-" + hashlib.sha256(owner_id.encode()).hexdigest()[:16]


@app.post("/api/v1/device/register")
async def device_register(req: DeviceRegisterRequest):
    owner_id = req.owner_id.strip()
    if owner_id and not _valid_owner(owner_id):
        raise HTTPException(status_code=400, detail="owner_id 格式錯誤")
    if not owner_id:
        owner_id = "own_" + secrets.token_urlsafe(18)
    now = tw_now_str()
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO device_owners (owner_id, owner_label, sync_space, created_at) VALUES (?, ?, ?, ?)",
            (owner_id, req.owner_label[:40], _owner_sync_space(owner_id), now),
        )
        conn.execute(
            """
            INSERT INTO device_registry (device_id, owner_id, device_name, platform, paired_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET owner_id=excluded.owner_id, device_name=excluded.device_name,
                platform=excluded.platform, last_seen=excluded.last_seen
            """,
            (req.device_id[:80], owner_id, req.device_name[:60], req.platform[:40], now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "owner_id": owner_id, "sync_space": _owner_sync_space(owner_id)}


@app.post("/api/v1/device/pair/create")
async def device_pair_create(req: DevicePairCreateRequest):
    if not _valid_owner(req.owner_id):
        raise HTTPException(status_code=400, detail="owner_id 格式錯誤，請先於本機建立 / 註冊裝置")
    code = f"{secrets.randbelow(1_000_000):06d}"
    code_hash = hashlib.sha256(f"shl-pair:{code}".encode()).hexdigest()
    expires = int(time.time()) + 600
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("DELETE FROM device_pair_codes WHERE expires_ts < ? OR used = 1", (int(time.time()),))
        conn.execute(
            "INSERT OR IGNORE INTO device_owners (owner_id, owner_label, sync_space, created_at) VALUES (?, '', ?, ?)",
            (req.owner_id, _owner_sync_space(req.owner_id), tw_now_str()),
        )
        conn.execute(
            "INSERT OR REPLACE INTO device_pair_codes (code_hash, owner_id, expires_ts, attempts, used, created_by) VALUES (?, ?, ?, 0, 0, ?)",
            (code_hash, req.owner_id, expires, req.device_id[:80]),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "code": code, "expires_in_sec": 600, "sync_space": _owner_sync_space(req.owner_id)}


_PAIR_FAILS: dict[str, list[float]] = {}


@app.post("/api/v1/device/pair/claim")
async def device_pair_claim(req: DevicePairClaimRequest, request: Request):
    client = request.client.host if request.client else "?"
    now_ts = time.time()
    fails = [t for t in _PAIR_FAILS.get(client, []) if now_ts - t < 600]
    if len(fails) >= 8:
        raise HTTPException(status_code=429, detail="配對嘗試過多，請 10 分鐘後再試")
    code = re.sub(r"\D", "", req.code or "")
    if len(code) != 6:
        raise HTTPException(status_code=400, detail="請輸入 6 位數配對碼")
    code_hash = hashlib.sha256(f"shl-pair:{code}".encode()).hexdigest()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT owner_id, expires_ts, used FROM device_pair_codes WHERE code_hash = ?", (code_hash,)
        ).fetchone()
        if row is None or int(row["used"]) == 1 or int(row["expires_ts"]) < int(now_ts):
            fails.append(now_ts)
            _PAIR_FAILS[client] = fails
            raise HTTPException(status_code=404, detail="配對碼無效或已過期（有效 10 分鐘、僅能使用一次）")
        owner_id = str(row["owner_id"])
        now = tw_now_str()
        conn.execute("UPDATE device_pair_codes SET used = 1 WHERE code_hash = ?", (code_hash,))
        conn.execute(
            """
            INSERT INTO device_registry (device_id, owner_id, device_name, platform, paired_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET owner_id=excluded.owner_id, device_name=excluded.device_name,
                platform=excluded.platform, paired_at=excluded.paired_at, last_seen=excluded.last_seen
            """,
            (req.device_id[:80], owner_id, req.device_name[:60], req.platform[:40], now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "owner_id": owner_id, "sync_space": _owner_sync_space(owner_id)}


@app.get("/api/v1/device/list")
async def device_list(request: Request):
    owner_id = _owner_from_request(request)
    if not owner_id:
        raise HTTPException(status_code=400, detail="缺少 X-SHL-Owner-Id")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT device_id, device_name, platform, paired_at, last_seen FROM device_registry WHERE owner_id = ? ORDER BY last_seen DESC",
            (owner_id,),
        ).fetchall()
        n_orders = conn.execute(
            "SELECT COUNT(*) FROM trade_orders WHERE owner_id = ?", (owner_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    return {
        "ok": True,
        "owner_id": owner_id,
        "sync_space": _owner_sync_space(owner_id),
        "devices": [dict(r) for r in rows],
        "owner_order_count": int(n_orders),
    }


@app.post("/api/v1/device/unlink")
async def device_unlink(request: Request, device_id: str = ""):
    owner_id = _owner_from_request(request)
    if not owner_id or not device_id:
        raise HTTPException(status_code=400, detail="缺少 owner 或 device_id")
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("DELETE FROM device_registry WHERE owner_id = ? AND device_id = ?", (owner_id, device_id[:80]))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


# ---------------------------------------------------------------------------
# v3.9.0 — Sinotrade Shioaji Pro Real-Time Bridge & Global Indices (NQ / SOX)
# 支援連線本機 shioaji server (http://127.0.0.1:8080) 或 Python shioaji SDK，
# 未填永豐金 Token 時自動降級至官方 TWSE/TPEx MIS + TAIFEX，確保行情不中斷。
# ---------------------------------------------------------------------------
_SHIOAJI_CFG: dict[str, Any] = {
    "api_key": os.getenv("SHIOAJI_API_KEY", "").strip(),
    "secret_key": os.getenv("SHIOAJI_SECRET_KEY", "").strip(),
    "http_url": os.getenv("SHIOAJI_HTTP_URL", "http://127.0.0.1:8080").strip().rstrip("/"),
    "simulation": True,
    "connected_source": "",
}


class ShioajiConfigRequest(BaseModel):
    api_key: str = ""
    secret_key: str = ""
    http_url: str = "http://127.0.0.1:8080"
    simulation: bool = True


def _shioaji_has_token() -> bool:
    return bool(
        (_SHIOAJI_CFG.get("api_key") or os.getenv("SHIOAJI_API_KEY", "").strip())
        and (_SHIOAJI_CFG.get("secret_key") or os.getenv("SHIOAJI_SECRET_KEY", "").strip())
    )


@app.get("/api/v1/shioaji/status")
async def shioaji_status():
    http_ok = False
    http_url = _SHIOAJI_CFG.get("http_url") or "http://127.0.0.1:8080"
    try:
        st, _ = await http_json(f"{http_url}/health", timeout=1.5)
        http_ok = 200 <= st < 500
    except Exception:
        http_ok = False
    sdk_installed = False
    try:
        import importlib.util
        sdk_installed = importlib.util.find_spec("shioaji") is not None
    except Exception:
        sdk_installed = False
    ak = _SHIOAJI_CFG.get("api_key") or os.getenv("SHIOAJI_API_KEY", "").strip()
    masked = (ak[:4] + "••••" + ak[-4:]) if len(ak) >= 8 else ("****" if ak else "")
    return {
        "ok": True,
        "token_configured": _shioaji_has_token(),
        "api_key_masked": masked,
        "http_url": http_url,
        "http_server_online": http_ok,
        "sdk_installed": sdk_installed,
        "simulation": bool(_SHIOAJI_CFG.get("simulation", True)),
        "active_source": "永豐金 Shioaji Pro HTTP" if http_ok else ("永豐金 Shioaji SDK" if (_shioaji_has_token() and sdk_installed) else "TWSE/TPEx MIS 官方即時（永豐金待連線）"),
    }


@app.post("/api/v1/shioaji/configure")
async def shioaji_configure(req: ShioajiConfigRequest):
    if req.api_key.strip():
        _SHIOAJI_CFG["api_key"] = req.api_key.strip()
    if req.secret_key.strip():
        _SHIOAJI_CFG["secret_key"] = req.secret_key.strip()
    if req.http_url.strip():
        _SHIOAJI_CFG["http_url"] = req.http_url.strip().rstrip("/")
    _SHIOAJI_CFG["simulation"] = bool(req.simulation)
    return await shioaji_status()


@app.get("/api/v1/market/global-indices")
async def global_indices_realtime():
    """Fetch live NQ=F (小那期貨) and ^SOX (費半指數) for Lock-Screen Live Activity (up to 5 items)."""
    targets = [("NQ", "小那期貨", "NQ=F"), ("SOX", "費半指數", "^SOX")]
    out: dict[str, Any] = {}
    for code, name, ysym in targets:
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ysym)}?interval=1m&range=1d&_={int(time.time()*1000)}"
            st, body = await http_json(url, timeout=6.0)
            if st == 200 and isinstance(body, dict):
                meta = ((body.get("chart") or {}).get("result") or [{}])[0].get("meta") or {}
                price = meta.get("regularMarketPrice")
                prev = meta.get("chartPreviousClose") or meta.get("previousClose") or price
                if price is not None and prev:
                    chg = round(float(price) - float(prev), 2)
                    pct = round((chg / float(prev)) * 100, 2)
                    out[code] = {
                        "code": code,
                        "name": name,
                        "price": round(float(price), 2),
                        "prev_close": round(float(prev), 2),
                        "change": chg,
                        "change_pct": pct,
                        "time": tw_now_str()[11:],
                    }
        except Exception:
            pass
    return JSONResponse({"ok": True, "indices": out, "server_time": tw_now_str()}, headers={"Cache-Control": "no-store"})


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
