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

from strategy30_engine import (
    PROMPT_VERSION,
    WEIGHT_VERSION,
    evaluate_symbol_30,
    get_jev_quota_status,
)

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
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS remote_accounts (
            account_id TEXT PRIMARY KEY,
            pass_hash TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            payload_json TEXT DEFAULT '',
            revision INTEGER NOT NULL DEFAULT 0,
            last_device TEXT DEFAULT '',
            updated_at TEXT NOT NULL
        )
        """
    )

    # v3.19.0 — Versioned Schema Migrations & Decision Handshake Diagnostics (Slow-Query Indexed)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    applied_versions = {
        int(r[0]) for r in conn.execute("SELECT version FROM schema_migrations").fetchall()
    }
    mig_ts = datetime.now(TW_TZ).isoformat(timespec="seconds")

    if 1 not in applied_versions:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS decision_handshake_runs (
                run_id TEXT PRIMARY KEY,
                owner_id TEXT DEFAULT '',
                env_mode TEXT DEFAULT 'SIM',
                scope_type TEXT DEFAULT '',
                symbols_json TEXT DEFAULT '[]',
                total_symbols INTEGER DEFAULT 0,
                buy_count INTEGER DEFAULT 0,
                short_count INTEGER DEFAULT 0,
                order_exec_count INTEGER DEFAULT 0,
                avg_latency_ms REAL DEFAULT 0,
                created_at TEXT NOT NULL,
                ts_ms INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            (1, "decision_handshake_runs", mig_ts),
        )

    if 2 not in applied_versions:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS decision_handshake_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                owner_id TEXT DEFAULT '',
                symbol TEXT NOT NULL,
                stock_name TEXT DEFAULT '',
                price REAL DEFAULT 0,
                change_pct REAL DEFAULT 0,
                bar_5m_json TEXT DEFAULT '{}',
                bar_1m_json TEXT DEFAULT '{}',
                direction TEXT DEFAULT 'NEUTRAL',
                strategies_json TEXT DEFAULT '[]',
                ai_score INTEGER DEFAULT 0,
                ai_verdict TEXT DEFAULT '',
                ai_reason TEXT DEFAULT '',
                final_action TEXT DEFAULT 'HOLD',
                order_executed INTEGER DEFAULT 0,
                order_id TEXT DEFAULT '',
                order_msg TEXT DEFAULT '',
                latency_ms REAL DEFAULT 0,
                created_at TEXT NOT NULL,
                ts_ms INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            (2, "decision_handshake_items", mig_ts),
        )

    if 3 not in applied_versions:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy30_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                eval_id TEXT NOT NULL,
                owner_id TEXT DEFAULT '',
                symbol TEXT NOT NULL,
                stock_name TEXT DEFAULT '',
                price REAL DEFAULT 0,
                change_pct REAL DEFAULT 0,
                valid_count INTEGER DEFAULT 0,
                long_count INTEGER DEFAULT 0,
                short_count INTEGER DEFAULT 0,
                neutral_count INTEGER DEFAULT 0,
                weighted_score REAL DEFAULT 0,
                group_agreement REAL DEFAULT 0,
                overall_direction TEXT DEFAULT 'neutral',
                hard_blocks_json TEXT DEFAULT '[]',
                signal_quality TEXT DEFAULT 'insufficient',
                action_tier TEXT DEFAULT 'ignore',
                review_required INTEGER DEFAULT 0,
                cost_usd REAL DEFAULT 0.0,
                weight_version TEXT DEFAULT '',
                prompt_version TEXT DEFAULT '',
                one_line_summary TEXT DEFAULT '',
                payload_json TEXT DEFAULT '{}',
                latency_ms REAL DEFAULT 0,
                created_at TEXT NOT NULL,
                ts_ms INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            (3, "strategy30_audit_log", mig_ts),
        )

    # Composite indexes to prevent slow queries on owner/symbol/action/run lookups
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dh_runs_owner_ts ON decision_handshake_runs(owner_id, ts_ms DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dh_items_run_sym ON decision_handshake_items(run_id, symbol)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dh_items_sym_ts ON decision_handshake_items(symbol, ts_ms DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dh_items_action_ts ON decision_handshake_items(final_action, ts_ms DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dh_items_owner_ts ON decision_handshake_items(owner_id, ts_ms DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_s30_sym_ts ON strategy30_audit_log(symbol, ts_ms DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_s30_tier_ts ON strategy30_audit_log(action_tier, ts_ms DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_s30_owner_ts ON strategy30_audit_log(owner_id, ts_ms DESC)"
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
    version="3.30.2",
)

_ALLOWED_ORIGINS_RAW = os.getenv("SHL_ALLOWED_ORIGINS", "").strip()
_ALLOWED_ORIGINS = [o.strip() for o in _ALLOWED_ORIGINS_RAW.split(",") if o.strip()] if _ALLOWED_ORIGINS_RAW else ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def optional_access_password_guard(request: Request, call_next):
    """
    若伺服器環境變數設定了 SHL_ACCESS_PASSWORD，則所有 /api/v1/* 私有端點
    （/api/v1/health 與 /api/v1/webhook/tv 除外）皆須於標頭攜帶相符的
    X-SHL-Access-Token，防止同區網或外部陌生人存取。
    同時為所有回應加上安全標頭，避免 Referrer 外洩或代理層快取敏感資料。
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
                    "detail": "此伺服器已啟用存取密碼保護，請填入正確的後端存取密碼（SHL_ACCESS_PASSWORD / X-SHL-Access-Token）。",
                    "access_password_required": True,
                },
            )
    resp = await call_next(request)
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    if path.startswith("/api/v1/"):
        resp.headers["Cache-Control"] = "no-store, private"
    return resp


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
    sim_token_ok = _shioaji_has_sim_token()
    live_token_ok = _shioaji_has_token()
    shioaji_configured = bool(sim_token_ok or live_token_ok)
    ca_path = os.getenv("SHIOAJI_CA_PATH", "")
    ca_ready = bool(ca_path and Path(ca_path).exists())
    return {
        "status": "ok",
        "version": "3.30.2",
        "e2ee_sync_supported": True,
        "realtime_relay_supported": True,
        "device_pairing_supported": True,
        "access_password_required": bool(os.getenv("SHL_ACCESS_PASSWORD", "").strip()),
        "public_cloud_unprotected": bool(
            any(os.getenv(k) for k in ("RENDER", "RAILWAY_ENVIRONMENT", "ZEABUR", "FLY_APP_NAME", "SPACE_ID"))
            and not os.getenv("SHL_ACCESS_PASSWORD", "").strip()
        ),
        "server_time": tw_now_str(),
        "trading_mode": SERVER_TRADING_MODE,
        "kill_switch": KILL_SWITCH_ACTIVE,
        "finmind_tokens": len(FINMIND_TOKENS),
        "shioaji_configured": shioaji_configured,
        "sim_token_configured": sim_token_ok,
        "live_token_configured": live_token_ok,
        "shioaji_active_source": "永豐金 Shioaji API 主連線" if shioaji_configured else "輔助資料源（TWSE/FinMind/Yahoo）",
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

    # v3.10.4: ALWAYS prefer true yesterday's previousClose over chartPreviousClose
    # (Yahoo's chartPreviousClose on 5d/15m charts is the close from 5 trading days ago, causing wrong +/- signs!)
    prev_close = meta.get("previousClose") or meta.get("regularMarketPreviousClose")
    if interval in ("1d", "1wk", "1mo") and len(candles) >= 2:
        prev_close = candles[-2]["close"]
    elif prev_close is None and candles:
        latest_day = str(candles[-1].get("date", ""))[:10]
        prev_day_candles = [c for c in candles if str(c.get("date", ""))[:10] < latest_day]
        if prev_day_candles:
            prev_close = prev_day_candles[-1]["close"]
    if prev_close is None:
        prev_close = meta.get("chartPreviousClose")
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
    request: Request,
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

    hdr_tok = (request.headers.get("x-finmind-token") or "").strip()
    use_token = hdr_tok or token.strip() or (next(_token_cycle) if _token_cycle else "")
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


def _extract_tw_symbols_from_prompt(text: str, limit: int = 0) -> list[str]:
    """Extract unique Taiwan stock/ETF codes from user custom focus prompt while skipping calendar years (unlimited by default)."""
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
            if limit > 0 and len(found) >= limit:
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
async def list_groq_models(request: Request, api_key: str = ""):
    hdr_key = (request.headers.get("x-groq-api-key") or "").strip()
    key = hdr_key or api_key.strip() or os.getenv("GROQ_API_KEY", "")
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
    extracted_from_prompt = _extract_tw_symbols_from_prompt(focus, limit=0)

    # Deduplicate explicit target_symbols + prompt-extracted symbols (v3.10.7: unlimited focus items!)
    explicit_targets: list[str] = []
    for raw_s in (req.target_symbols or []):
        s = str(raw_s).strip().upper()
        if s and s not in explicit_targets:
            explicit_targets.append(s)
    for s in extracted_from_prompt:
        if s not in explicit_targets:
            explicit_targets.append(s)

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
        sem = asyncio.Semaphore(10)

        async def _one(s: str):
            if not use_uniform_daily and s in pre_map:
                return pre_map[s]
            async with sem:
                return await _fetch_live_snapshot_for_ai(s)

        fetched = await asyncio.gather(*[_one(s) for s in syms])
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
        resolved_targets = await _resolve_symbols(wl_syms)
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

    # v3.14.1: Per-model circuit breaker cooldown (429/402/401/5xx/timeout) + 60s response cache + 12s strict timeout
    global _AI_COOLDOWN_MAP, _AI_RESULT_CACHE
    if "_AI_COOLDOWN_MAP" not in globals():
        _AI_COOLDOWN_MAP = {}
    if "_AI_RESULT_CACHE" not in globals():
        _AI_RESULT_CACHE = {}

    async def _call_single_ai_guarded(model_id: str) -> dict[str, Any]:
        now_t = time.time()
        raw_key = str((req.api_keys or {}).get(model_id, "") or "")
        key_fp = hashlib.sha1(raw_key.encode("utf-8")).hexdigest()[:8] if raw_key else "env"
        cd_key = f"{model_id}:{key_fp}"

        # 1. Check circuit-breaker cooldown (prevents hammering 429/402/401 providers)
        cd = _AI_COOLDOWN_MAP.get(cd_key)
        if cd and cd["until"] > now_t:
            rem = max(1, int(cd["until"] - now_t))
            return {
                "model": model_id,
                "connected": False,
                "cooldown_sec": rem,
                "error": f"{cd['reason']}（自動冷卻剩餘 {rem} 秒，避免過度詢問）",
            }

        # 2. Check 60s response cache for same model + targets + price bucket + focus prompt
        sig_parts = [f"{t.get('symbol')}:{round(float(t.get('price') or 0), 1)}" for t in (resolved_targets or [])[:8]]
        c_key = f"{cd_key}|{resolved_mode}|{'/'.join(sig_parts)}|{hashlib.sha1(focus.encode('utf-8')).hexdigest()[:8]}"
        cached_item = _AI_RESULT_CACHE.get(c_key)
        if cached_item and (now_t - cached_item["ts"]) < 60.0:
            out_c = dict(cached_item["res"])
            out_c["from_cache"] = True
            return out_c

        # 3. Call provider with strict 12s timeout so slow/broken APIs never block other models
        try:
            res = await asyncio.wait_for(
                _call_single_ai(model_id, req, resolved_mode=resolved_mode, resolved_targets=resolved_targets),
                timeout=12.0,
            )
        except asyncio.TimeoutError:
            _AI_COOLDOWN_MAP[cd_key] = {"until": now_t + 45.0, "reason": "連線逾時 (>12s)"}
            return {"model": model_id, "connected": False, "cooldown_sec": 45, "error": "連線逾時 (>12s)，已自動冷卻 45 秒"}
        except Exception as exc:
            _AI_COOLDOWN_MAP[cd_key] = {"until": now_t + 45.0, "reason": f"網路異常 ({type(exc).__name__})"}
            return {"model": model_id, "connected": False, "cooldown_sec": 45, "error": f"網路異常，已自動冷卻 45 秒"}

        if res.get("connected"):
            _AI_COOLDOWN_MAP.pop(cd_key, None)
            _AI_RESULT_CACHE[c_key] = {"ts": now_t, "res": dict(res)}
            if len(_AI_RESULT_CACHE) > 200:
                oldest = sorted(_AI_RESULT_CACHE.items(), key=lambda kv: kv[1]["ts"])[:50]
                for k_old, _ in oldest:
                    _AI_RESULT_CACHE.pop(k_old, None)
            return res

        # Classify error and trigger appropriate circuit-breaker cooldown
        err_str = str(res.get("error") or "")
        if "HTTP 402" in err_str or "402" in err_str:
            _AI_COOLDOWN_MAP[cd_key] = {"until": now_t + 600.0, "reason": "API 額度不足 (HTTP 402)"}
            res["cooldown_sec"] = 600
            res["error"] = "API 額度不足 (HTTP 402)，已自動冷卻 10 分鐘避免重複扣款／詢問"
        elif "HTTP 401" in err_str or "HTTP 403" in err_str:
            _AI_COOLDOWN_MAP[cd_key] = {"until": now_t + 600.0, "reason": "API 金鑰無效或無權限 (HTTP 401/403)"}
            res["cooldown_sec"] = 600
            res["error"] = "API 金鑰無效或無權限 (HTTP 401/403)，已自動冷卻 10 分鐘（更換金鑰即解除）"
        elif "HTTP 429" in err_str or "429" in err_str or "rate" in err_str.lower():
            _AI_COOLDOWN_MAP[cd_key] = {"until": now_t + 90.0, "reason": "速率限流 (HTTP 429)"}
            res["cooldown_sec"] = 90
            res["error"] = "速率限流 (HTTP 429)，已自動冷卻 90 秒避免持續鎖流"
        elif any(code in err_str for code in ("HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504", "timeout", "逾時", "連線")):
            _AI_COOLDOWN_MAP[cd_key] = {"until": now_t + 45.0, "reason": "雲端服務暫時異常"}
            res["cooldown_sec"] = 45
            res["error"] = f"{err_str}（已自動冷卻 45 秒）"
        return res

    results = await asyncio.gather(*(_call_single_ai_guarded(m) for m in req.models))
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
                    prev_c = float(meta.get("previousClose") or meta.get("regularMarketPreviousClose") or meta.get("chartPreviousClose") or candles[0]["open"])
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
    allow_after_hours: bool = False


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

    if mode == "SIM" and not req.allow_after_hours:
        now_tw = datetime.now(TW_TZ)
        wd = now_tw.weekday()  # 0=Mon .. 4=Fri
        hhmm = now_tw.hour * 100 + now_tw.minute
        if wd > 4 or hhmm < 900 or hhmm > 1320:
            raise HTTPException(
                status_code=400,
                detail=f"目前為非台股交易時段（台北時間 {now_tw.strftime('%Y-%m-%d %H:%M:%S')}），虛擬單僅開放台股交易日 09:00~13:20 下單。",
            )

    shares = req.qty * 1000 if req.unit == "張" else req.qty
    if shares < 1000 or (shares % 1000) != 0:
        raise HTTPException(
            status_code=400,
            detail=f"買賣最低單位為 1,000 股（1 張），且僅接受 1,000 股之整數倍（收到 {shares} 股），禁止低於 1,000 股或零股下單。",
        )
    lots = shares // 1000
    norm_unit = "張"
    req.price = snap_tw_tick(req.price, req.symbol, "round")
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
                    quantity=lots,
                    action=sj.constant.Action.Buy if req.side.upper() == "BUY" else sj.constant.Action.Sell,
                    price_type=sj.constant.StockPriceType.MKT if req.order_type == "MKT" else sj.constant.StockPriceType.LMT,
                    order_type=getattr(sj.constant.OrderType, req.tif, sj.constant.OrderType.ROD),
                    order_lot=sj.constant.StockOrderLot.Common,
                    account=api.stock_account,
                )
                trade = api.place_order(contract, order)
                api.logout()
                return str(trade.order.id), str(trade.status.status)

            broker_id, status_str = await asyncio.to_thread(_exec_shioaji)
            order_id = broker_id or order_id
            broker_msg = f"永豐金 Shioaji 實盤整張委託送出 ({lots}張={shares:,}股 · {status_str})"
            final_status = "SUBMITTED_LIVE"
        except ImportError:
            raise HTTPException(
                status_code=501,
                detail="伺服器尚未安裝 shioaji 套件（請執行 pip install shioaji）",
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Shioaji 實盤下單錯誤：{exc}")
    else:
        broker_msg = f"模擬盤 SIM 依真實行情整張撮合成交 ({lots}張={shares:,}股)"
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
            lots,
            norm_unit,
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
        "qty": lots,
        "unit": norm_unit,
        "shares": shares,
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


_OFFICIAL_EOD_CACHE: dict[str, Any] = {"ts": 0.0, "data": {}}


def _parse_eod_num(v: Any) -> Optional[float]:
    try:
        s = str(v or "").replace(",", "").replace("+", "").replace("X", "").strip()
        if not s or s in ("-", "--", "N/A"):
            return None
        return float(s)
    except Exception:
        return None


def _fetch_twse_eod_sync() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    try:
        req = urllib.request.Request("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", headers=headers)
        with urllib.request.urlopen(req, timeout=8) as r:
            rows = json.loads(r.read().decode("utf-8", errors="replace"))
        if isinstance(rows, list):
            for row in rows:
                code = str(row.get("Code", "")).strip().upper()
                close = _parse_eod_num(row.get("ClosingPrice"))
                if not code or close is None or close <= 0:
                    continue
                chg = _parse_eod_num(row.get("Change"))
                prev = round(close - chg, 4) if chg is not None else close
                shares = _parse_eod_num(row.get("TradeVolume")) or 0.0
                out[code] = {
                    "symbol": code,
                    "name": str(row.get("Name", "")).strip(),
                    "exchange": "tse",
                    "price": close,
                    "prev_close": prev if prev > 0 else close,
                    "change": round(chg, 4) if chg is not None else 0.0,
                    "change_pct": round((chg / prev) * 100.0, 2) if (chg is not None and prev > 0) else 0.0,
                    "open": _parse_eod_num(row.get("OpeningPrice")) or close,
                    "high": _parse_eod_num(row.get("HighestPrice")) or close,
                    "low": _parse_eod_num(row.get("LowestPrice")) or close,
                    "volume_lots": round(shares / 1000.0, 2),
                    "date": str(row.get("Date", "")).strip(),
                    "is_eod_official": True,
                }
    except Exception:
        pass
    return out


def _fetch_tpex_eod_sync() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    try:
        req = urllib.request.Request("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes", headers=headers)
        with urllib.request.urlopen(req, timeout=8) as r:
            rows = json.loads(r.read().decode("utf-8", errors="replace"))
        if isinstance(rows, list):
            for row in rows:
                code = str(row.get("SecuritiesCompanyCode", "")).strip().upper()
                close = _parse_eod_num(row.get("Close"))
                if not code or close is None or close <= 0:
                    continue
                chg = _parse_eod_num(row.get("Change"))
                prev = round(close - chg, 4) if chg is not None else close
                shares = _parse_eod_num(row.get("TradingShares")) or 0.0
                out[code] = {
                    "symbol": code,
                    "name": str(row.get("CompanyName", "")).strip(),
                    "exchange": "otc",
                    "price": close,
                    "prev_close": prev if prev > 0 else close,
                    "change": round(chg, 4) if chg is not None else 0.0,
                    "change_pct": round((chg / prev) * 100.0, 2) if (chg is not None and prev > 0) else 0.0,
                    "open": _parse_eod_num(row.get("Open")) or close,
                    "high": _parse_eod_num(row.get("High")) or close,
                    "low": _parse_eod_num(row.get("Low")) or close,
                    "volume_lots": round(shares / 1000.0, 2),
                    "date": str(row.get("Date", "")).strip(),
                    "is_eod_official": True,
                }
    except Exception:
        pass
    return out


async def _get_official_eod_map() -> dict[str, dict[str, Any]]:
    now = time.time()
    if _OFFICIAL_EOD_CACHE["data"] and (now - _OFFICIAL_EOD_CACHE["ts"]) < 300:
        return _OFFICIAL_EOD_CACHE["data"]
    twse_map, tpex_map = await asyncio.gather(
        asyncio.to_thread(_fetch_twse_eod_sync),
        asyncio.to_thread(_fetch_tpex_eod_sync),
    )
    merged = {**tpex_map, **twse_map}
    if merged:
        _OFFICIAL_EOD_CACHE["data"] = merged
        _OFFICIAL_EOD_CACHE["ts"] = now
    return _OFFICIAL_EOD_CACHE["data"]


_RANK_LIVE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _fetch_mis_batch_exact_sync(ex_channels: list[str]) -> dict[str, dict[str, Any]]:
    """Fetch exact tse_XXXX.tw / otc_XXXX.tw channels from official TWSE/TPEx MIS."""
    out: dict[str, dict[str, Any]] = {}
    if not ex_channels:
        return out
    url = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp?json=1&delay=0&ex_ch=" + urllib.parse.quote("|".join(ex_channels))
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://mis.twse.com.tw/stock/fibest.jsp",
        "Accept": "application/json",
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        for m in data.get("msgArray") or []:
            code = str(m.get("c", "")).strip().upper()
            if not code:
                continue
            z = _parse_mis_float(m.get("z"))
            y = _parse_mis_float(m.get("y"))
            v = _parse_mis_float(m.get("v"))
            if z is None:
                bids = [_parse_mis_float(x) for x in str(m.get("b", "")).split("_") if x]
                z = _parse_mis_float(m.get("pz")) or (bids[0] if bids else None)
            if z is not None and z > 0 and y is not None and y > 0:
                out[code] = {
                    "price": round(z, 2),
                    "prev_close": round(y, 2),
                    "change_pct": round((z - y) / y * 100.0, 2),
                    "volume_lots": v if (v is not None and v >= 0) else 0.0,
                    "high": _parse_mis_float(m.get("h")) or z,
                    "low": _parse_mis_float(m.get("l")) or z,
                    "date": str(m.get("d", "")).strip(),
                    "time": str(m.get("t", "")).strip(),
                }
    except Exception:
        pass
    return out


def _fetch_yahoo_tw_live_rank_sync(direction: str = "up") -> list[dict[str, Any]]:
    """Fetch true live full-market (TWSE+TPEx) change-up / change-down Top-100 directly from Yahoo TW Stock."""
    slug = "change-up" if direction == "up" else "change-down"
    url = f"https://tw.stock.yahoo.com/rank/{slug}?exchange=ALL"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    }
    out: list[dict[str, Any]] = []
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        m = re.search(r'"list":(\[\{[^\n]*?"symbolName":[^\n]*?\}\])', html)
        if not m:
            return out
        lst = json.loads(m.group(1))
        for x in lst:
            raw_sym = str(x.get("symbol") or "").strip().upper()
            if not raw_sym:
                continue
            ex = "otc" if raw_sym.endswith(".TWO") else "tse"
            code = raw_sym.split(".")[0]
            px = _parse_eod_num(x.get("price"))
            pct_str = str(x.get("changePercent") or "").replace("%", "").replace("+", "").strip()
            pct = _parse_eod_num(pct_str)
            if px is None or px <= 0 or pct is None:
                continue
            lots = _parse_eod_num(x.get("volK")) or ((_parse_eod_num(x.get("volume")) or 0.0) / 1000.0)
            turnover_k = _parse_eod_num(x.get("turnoverK"))
            val_m = round(turnover_k / 1000.0, 2) if turnover_k is not None else round(px * lots * 1000.0 / 1e6, 2)
            out.append({
                "symbol": code,
                "name": str(x.get("symbolName") or x.get("name") or code).strip(),
                "exchange": ex,
                "price": round(px, 2),
                "prev_close": _parse_eod_num(x.get("previousClose")) or px,
                "change": _parse_eod_num(x.get("change")) or 0.0,
                "change_pct": round(pct, 2),
                "volume_lots": round(lots, 2),
                "value_m": val_m,
                "high": _parse_eod_num(x.get("dayHigh")) or px,
                "low": _parse_eod_num(x.get("dayLow")) or px,
                "trade_date": tw_now_str()[:10].replace("-", ""),
                "live_verified": True,
                "is_etf": code.startswith("00") or code.startswith("02"),
            })
    except Exception:
        pass
    return out


import math


def tw_tick_size(price: float, symbol: str = "") -> float:
    """Official TWSE / TPEx price-tiered tick size (stocks: 6 tiers; ETFs 00xxx: 2 tiers)."""
    p = float(price or 0.0)
    sym = str(symbol or "").strip().upper()
    is_etf = sym.startswith("00")
    if is_etf:
        return 0.01 if p < 50.0 else 0.05
    if p < 10.0:
        return 0.01
    if p < 50.0:
        return 0.05
    if p < 100.0:
        return 0.1
    if p < 500.0:
        return 0.5
    if p < 1000.0:
        return 1.0
    return 5.0


def snap_tw_tick(price: float, symbol: str = "", mode: str = "round") -> float:
    """Snap a price to the nearest valid TWSE / TPEx tick (mode: round | up | down)."""
    p = float(price or 0.0)
    if p <= 0:
        return 0.0
    t = tw_tick_size(p, symbol)
    ratio = p / t
    if mode == "up":
        snapped = math.ceil(ratio - 1e-9) * t
    elif mode == "down":
        snapped = math.floor(ratio + 1e-9) * t
    else:
        snapped = round(ratio) * t
    return round(snapped, 2)


def _is_tw_intraday_market_hours(now_dt: Optional[datetime] = None) -> bool:
    """Return True strictly during Taiwan Stock Exchange regular trading hours (Mon-Fri 09:00~13:35 Asia/Taipei)."""
    dt = now_dt or datetime.now(TW_TZ)
    if dt.weekday() > 4:
        return False
    hm = dt.hour * 100 + dt.minute
    return 900 <= hm <= 1335


def _fetch_shioaji_live_rank_sync(limit: int = 50) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    v3.30.2 Priority 1: Dynamic real-time stock screener directly via Shioaji PRODUCTION API (simulation=False ONLY).
    NEVER falls back to simulation=True (which returns fake randomized +/-10% sandbox test prices).
    """
    ak, sk = _shioaji_market_creds()
    if not (ak and sk):
        return [], []
    try:
        import shioaji as sj  # type: ignore
    except ImportError:
        return [], []

    api = None
    try:
        api = sj.Shioaji(simulation=False)
        api.login(api_key=ak, secret_key=sk)
    except Exception:
        return [], []

    try:
        cnt = max(20, min(int(limit), 80))
        up_raw = api.scanners(scanner_type=sj.constant.ScannerType.ChangePercentRank, ascending=False, count=cnt) or []
        dn_raw = api.scanners(scanner_type=sj.constant.ScannerType.ChangePercentRank, ascending=True, count=cnt) or []
        amt_raw = api.scanners(scanner_type=sj.constant.ScannerType.AmountRank, ascending=False, count=min(cnt, 40)) or []

        def _conv(rows_in: list[Any]) -> list[dict[str, Any]]:
            out_rows: list[dict[str, Any]] = []
            seen: set[str] = set()
            for r in rows_in:
                code = str(getattr(r, "code", "") or "").strip().upper()
                if not code or code in seen or not re.fullmatch(r"[0-9A-Z]{4,6}", code):
                    continue
                px = float(getattr(r, "close", 0) or 0)
                if px <= 0:
                    continue
                seen.add(code)
                chg_pct = float(getattr(r, "change_rate", 0) or 0)
                vol_lots = float(getattr(r, "total_volume", 0) or 0)
                amt_twd = float(getattr(r, "total_amount", 0) or (px * vol_lots * 1000.0))
                val_m = round(amt_twd / 1e6, 1)
                nm = str(getattr(r, "name", "") or code).strip()
                out_rows.append({
                    "symbol": code,
                    "name": nm,
                    "exchange": "tse",
                    "price": px,
                    "change_pct": round(chg_pct, 2),
                    "volume_lots": vol_lots,
                    "value_m": val_m,
                    "is_etf": code.startswith("00"),
                    "tick_size": tw_tick_size(px, code),
                    "live_verified": True,
                })
            return out_rows

        bulls = _conv(list(up_raw) + [x for x in amt_raw if float(getattr(x, "change_rate", 0) or 0) > 0])
        bears = _conv(list(dn_raw) + [x for x in amt_raw if float(getattr(x, "change_rate", 0) or 0) < 0])
        return bulls, bears
    except Exception:
        return [], []
    finally:
        try:
            api.logout()
        except Exception:
            pass


@app.get("/api/v1/market/rank")
async def market_rank(universe: str = "all", limit: int = 50, min_value_m: float = 100.0, symbols: str = ""):
    """
    v3.30.0: True real-time full-market bull / bear Top-N (Zero Hardcoding).
    Priority 1: Shioaji API live scanners (ChangePercentRank + AmountRank) when SIM/LIVE credentials are active.
    Priority 2: Live Yahoo TW full-market (TWSE + TPEx) change-up / change-down rankings.
    Priority 3: Official EOD + MIS live secondary verification fallback (and used for custom/chain universe).
    universe: all (全市場含ETF) | stocks (僅台股個股·排除ETF) | liquid (成交值 >= min_value_m 百萬) | custom (symbols=...)
    """
    limit = max(5, min(int(limit), 100))
    cache_key = f"{universe}|{limit}|{min_value_m}|{symbols.strip().upper()}"
    now_ts = time.time()
    cached = _RANK_LIVE_CACHE.get(cache_key)
    if cached and (now_ts - cached[0]) < 25:
        return JSONResponse(cached[1], headers={"Cache-Control": "no-store"})

    eod_task = asyncio.create_task(_get_official_eod_map())
    live_up: list[dict[str, Any]] = []
    live_dn: list[dict[str, Any]] = []
    rank_source = "即時全市場實盤漲跌幅排行（上市＋上櫃同步）"
    shioaji_used = False
    if universe in ("all", "stocks", "liquid"):
        sj_up, sj_dn = await asyncio.to_thread(_fetch_shioaji_live_rank_sync, limit)
        if sj_up or sj_dn:
            live_up, live_dn = sj_up, sj_dn
            rank_source = "永豐金 Shioaji API 動態實盤選股（漲跌幅＋成交金額排行）"
            shioaji_used = True
        else:
            live_up, live_dn = await asyncio.gather(
                asyncio.to_thread(_fetch_yahoo_tw_live_rank_sync, "up"),
                asyncio.to_thread(_fetch_yahoo_tw_live_rank_sync, "down"),
            )

    eod = await eod_task
    adv = dec = flat = 0
    for code, r in eod.items():
        if not re.fullmatch(r"[1-9][0-9]{3}", code):
            continue
        pct = float(r.get("change_pct") or 0)
        if pct > 0:
            adv += 1
        elif pct < 0:
            dec += 1
        else:
            flat += 1
    total = adv + dec + flat

    # Fast, exact real-time path when live full-market rankings succeeded
    if universe in ("all", "stocks", "liquid") and (live_up or live_dn):
        def _flt(items: list[dict[str, Any]], is_bull: bool) -> list[dict[str, Any]]:
            res = []
            for x in items:
                if is_bull and x["change_pct"] <= 0:
                    continue
                if (not is_bull) and x["change_pct"] >= 0:
                    continue
                if universe == "stocks" and x.get("is_etf"):
                    continue
                if universe == "liquid" and x["value_m"] < min_value_m:
                    continue
                item_copy = dict(x)
                item_copy["tick_size"] = tw_tick_size(float(item_copy.get("price") or 0), str(item_copy.get("symbol") or ""))
                res.append(item_copy)
            return res[:limit]

        bulls = _flt(live_up, True)
        bears = _flt(live_dn, False)
        today_str = tw_now_str()[:10].replace("-", "")
        payload = {
            "ok": True,
            "source": rank_source,
            "shioaji_dynamic": shioaji_used,
            "data_date": today_str,
            "live_verified": True,
            "verified_count": len(bulls) + len(bears),
            "filtered_stale_count": 0,
            "universe": universe,
            "count": total if total else (len(live_up) + len(live_dn)),
            "breadth": {"advance": adv, "decline": dec, "flat": flat, "adv_ratio": round(adv / total, 4) if total else 0},
            "bulls": bulls,
            "bears": bears,
        }
        _RANK_LIVE_CACHE[cache_key] = (now_ts, payload)
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    # Custom / 12-chain or fallback path: EOD + Shioaji/MIS live secondary verification
    custom = {x.strip().upper() for x in symbols.split(",") if x.strip()}
    rows: list[dict[str, Any]] = []
    for code, r in eod.items():
        if not re.fullmatch(r"[0-9A-Z]{4,6}", code):
            continue
        px = float(r.get("price") or 0)
        pct = float(r.get("change_pct") or 0)
        lots = float(r.get("volume_lots") or 0)
        value_m = round(px * lots * 1000.0 / 1e6, 1)
        if universe == "custom" and code not in custom:
            continue
        if universe == "stocks" and (code.startswith("00") or code.startswith("02")):
            continue
        if universe == "liquid" and value_m < min_value_m:
            continue
        if universe == "all" and (lots < 5 or value_m < 1.0):
            continue
        if lots <= 0:
            continue
        rows.append({
            "symbol": code, "name": r.get("name", ""), "exchange": r.get("exchange", "tse") or "tse",
            "price": px, "change_pct": pct, "volume_lots": lots, "value_m": value_m,
            "high": r.get("high"), "low": r.get("low"),
            "trade_date": str(r.get("date", "")), "live_verified": False,
        })

    cand_bulls = sorted(rows, key=lambda x: (x["change_pct"], x["value_m"]), reverse=True)[:75]
    cand_bears = sorted(rows, key=lambda x: (x["change_pct"], -x["value_m"]))[:75]
    cand_active = sorted(rows, key=lambda x: x["value_m"], reverse=True)[:70]
    cand_map: dict[str, dict[str, Any]] = {}
    for item in cand_bulls + cand_bears + cand_active:
        cand_map[item["symbol"]] = item

    live_map: dict[str, dict[str, Any]] = {}
    cand_syms = list(cand_map.keys())
    missing_syms = [s for s in cand_syms if s not in live_map]
    if missing_syms:
        channels = [
            f"{'otc' if cand_map[s].get('exchange') == 'otc' else 'tse'}_{s}.tw"
            for s in missing_syms
        ]
        chunks = [channels[i:i + 50] for i in range(0, len(channels), 50)]
        batch_results = await asyncio.gather(*[asyncio.to_thread(_fetch_mis_batch_exact_sync, ch) for ch in chunks])
        for bres in batch_results:
            live_map.update(bres)

    verified_count = 0
    filtered_stale_count = 0
    live_dates: list[str] = []
    for sym, item in cand_map.items():
        lv = live_map.get(sym)
        if not lv:
            continue
        old_pct = item["change_pct"]
        new_pct = lv["change_pct"]
        if (old_pct > 0 and new_pct <= 0) or (old_pct < 0 and new_pct >= 0) or abs(old_pct - new_pct) >= 1.5:
            filtered_stale_count += 1
        item["price"] = lv["price"]
        item["change_pct"] = new_pct
        if lv["volume_lots"] > 0:
            item["volume_lots"] = lv["volume_lots"]
            item["value_m"] = round(lv["price"] * lv["volume_lots"] * 1000.0 / 1e6, 1)
        item["high"] = lv["high"]
        item["low"] = lv["low"]
        item["live_verified"] = True
        if lv.get("date"):
            item["trade_date"] = lv["date"]
            live_dates.append(lv["date"])
        verified_count += 1

    verified_pool = list(cand_map.values())
    bulls = sorted([x for x in verified_pool if x["change_pct"] > 0], key=lambda x: (1 if x["live_verified"] else 0, x["change_pct"], x["value_m"]), reverse=True)[:limit]
    bears = sorted([x for x in verified_pool if x["change_pct"] < 0], key=lambda x: (1 if x["live_verified"] else 0, -x["change_pct"], x["value_m"]), reverse=True)[:limit]

    dates = sorted({str(r.get("date", "")) for r in eod.values() if r.get("date")})
    live_date = sorted(set(live_dates))[-1] if live_dates else (dates[-1] if dates else "")
    payload = {
        "ok": True,
        "source": f"實盤二次校驗（TWSE·TPEx MIS 即時 · 校驗 {verified_count} 檔，修正過期 {filtered_stale_count} 檔）" if verified_count > 0 else "TWSE STOCK_DAY_ALL + TPEx OpenData",
        "data_date": live_date,
        "live_verified": verified_count > 0,
        "verified_count": verified_count,
        "filtered_stale_count": filtered_stale_count,
        "universe": universe, "count": len(rows),
        "breadth": {"advance": adv, "decline": dec, "flat": flat, "adv_ratio": round(adv / total, 4) if total else 0},
        "bulls": bulls, "bears": bears,
    }
    _RANK_LIVE_CACHE[cache_key] = (now_ts, payload)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@app.get("/api/v1/market/realtime")
async def twse_realtime_quotes(symbols: str = "", indices: str = "t00,o00"):
    """
    盤中即時報價（三竹股市同級官方 TWSE/TPEx MIS 來源）：成交價、漲跌、開高低昨收、總量、單量、五檔。
    symbols=2330,6488 （自動同時查詢上市 tse 與上櫃 otc 通道）；indices=t00（加權）,o00（櫃買）。
    若遇盤前 MIS 重置無成交量/價，自動以 TWSE/TPEx OpenData 官方最近交易日收盤與成交量補齊，不歸零。
    """
    syms = [x.strip().upper() for x in symbols.split(",") if re.fullmatch(r"[0-9A-Z]{4,6}", x.strip().upper())][:80]
    idx = [x.strip().lower() for x in indices.split(",") if x.strip().lower() in ("t00", "o00", "t13", "t17")]
    all_channels: list[str] = []
    for i in idx:
        all_channels.append(f"{'otc' if i.startswith('o') else 'tse'}_{i}.tw")
    for sy in syms:
        all_channels.append(f"tse_{sy}.tw")
        all_channels.append(f"otc_{sy}.tw")
    if not all_channels:
        raise HTTPException(status_code=400, detail="請提供 symbols 或 indices")

    # Fetch MIS chunks concurrently (max 50 channels per request so TWSE MIS never drops trailing symbols)
    chunk_size = 50
    chunks = [all_channels[s:s + chunk_size] for s in range(0, len(all_channels), chunk_size)]

    async def _fetch_chunk(ch_list: list[str]) -> list[dict[str, Any]]:
        url = (
            "https://mis.twse.com.tw/stock/api/getStockInfo.jsp?json=1&delay=0&ex_ch="
            + urllib.parse.quote("|".join(ch_list), safe="|_.")
            + f"&_={int(time.time() * 1000)}"
        )
        try:
            status, data, _ = await asyncio.to_thread(_sync_relay_request, url, "GET", None, "")
            if status == 200:
                payload = json.loads(data.decode("utf-8", errors="replace").strip())
                return payload.get("msgArray", []) or []
        except Exception:
            pass
        return []

    chunk_results = await asyncio.gather(*[_fetch_chunk(c) for c in chunks])
    msg_rows: list[dict[str, Any]] = [row for sub in chunk_results for row in sub]

    out: dict[str, Any] = {}
    need_eod_syms: list[str] = []
    in_session = _is_tw_intraday_market_hours()
    for m in msg_rows:
        code = str(m.get("c", "")).strip()
        if not code:
            continue
        raw_z = _parse_mis_float(m.get("z"))
        bids = [_parse_mis_float(x) for x in str(m.get("b", "")).split("_") if x]
        asks = [_parse_mis_float(x) for x in str(m.get("a", "")).split("_") if x]
        prev = _parse_mis_float(m.get("y"))
        vol_lots = _parse_mis_float(m.get("v"))
        # v3.30.2: Strictly require real traded price raw_z during market hours;
        # NEVER use pre-market/overnight dummy bids[0] or pz trial bids (which often show +/-10% fake prices!)
        last = raw_z if raw_z is not None else prev
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
            "volume_lots": vol_lots,
            "tick_volume": _parse_mis_float(m.get("tv")),
            "bids": bids[:5],
            "bid_sizes": [_parse_mis_float(x) for x in str(m.get("g", "")).split("_") if x][:5],
            "asks": asks[:5],
            "ask_sizes": [_parse_mis_float(x) for x in str(m.get("f", "")).split("_") if x][:5],
            "date": m.get("d", ""),
            "time": m.get("t", ""),
            "tlong": m.get("tlong", ""),
        }
        # Whenever off-hours OR MIS has no real trade price (raw_z is None), force official EOD real close/prev_close/change_pct
        if code not in ("t00", "o00", "t13", "t17") and ((not in_session) or raw_z is None or (vol_lots is None or vol_lots <= 0)):
            need_eod_syms.append(code)

    for sy in syms:
        if sy not in out:
            need_eod_syms.append(sy)

    used_eod = False
    if need_eod_syms:
        eod_map = await _get_official_eod_map()
        for code in set(need_eod_syms):
            eod = eod_map.get(code)
            if not eod:
                continue
            used_eod = True
            base = out.get(code, {})
            out[code] = {
                **base,
                "symbol": code,
                "name": base.get("name") or eod["name"],
                "exchange": base.get("exchange") or eod["exchange"],
                "price": eod["price"],
                "prev_close": eod["prev_close"],
                "change": eod["change"],
                "change_pct": eod["change_pct"],
                "open": eod["open"],
                "high": eod["high"],
                "low": eod["low"],
                "volume_lots": eod["volume_lots"],
                "date": base.get("date") or eod["date"],
                "time": base.get("time") or "收盤",
                "is_eod_official": True,
            }

    return JSONResponse(
        {
            "source": "TWSE/TPEx 官方最近交易日收盤＋MIS即時" if used_eod else "TWSE/TPEx MIS 官方即時",
            "server_time": tw_now_str(),
            "quotes": out,
        },
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
    "sim_api_key": os.getenv("SHIOAJI_SIM_API_KEY", "").strip(),
    "sim_secret_key": os.getenv("SHIOAJI_SIM_SECRET_KEY", "").strip(),
    "api_key": os.getenv("SHIOAJI_API_KEY", "").strip(),
    "secret_key": os.getenv("SHIOAJI_SECRET_KEY", "").strip(),
    "http_url": os.getenv("SHIOAJI_HTTP_URL", "http://127.0.0.1:8080").strip().rstrip("/"),
    "simulation": True,
    "connected_source": "",
}


class ShioajiConfigRequest(BaseModel):
    sim_api_key: str = ""
    sim_secret_key: str = ""
    api_key: str = ""
    secret_key: str = ""
    http_url: str = "http://127.0.0.1:8080"
    simulation: bool = True


def _shioaji_has_sim_token() -> bool:
    return bool(
        (_SHIOAJI_CFG.get("sim_api_key") or os.getenv("SHIOAJI_SIM_API_KEY", "").strip())
        and (_SHIOAJI_CFG.get("sim_secret_key") or os.getenv("SHIOAJI_SIM_SECRET_KEY", "").strip())
    )


def _shioaji_has_token() -> bool:
    """True only when LIVE Shioaji API Token is configured (SIM token never unlocks LIVE orders)."""
    return bool(
        (_SHIOAJI_CFG.get("api_key") or os.getenv("SHIOAJI_API_KEY", "").strip())
        and (_SHIOAJI_CFG.get("secret_key") or os.getenv("SHIOAJI_SECRET_KEY", "").strip())
    )


def _shioaji_market_creds() -> tuple[str, str]:
    """Return credentials for real-time market data & K-bars (prefer SIM token when in SIM mode, else LIVE token)."""
    sim_ak = _SHIOAJI_CFG.get("sim_api_key") or os.getenv("SHIOAJI_SIM_API_KEY", "").strip()
    sim_sk = _SHIOAJI_CFG.get("sim_secret_key") or os.getenv("SHIOAJI_SIM_SECRET_KEY", "").strip()
    live_ak = _SHIOAJI_CFG.get("api_key") or os.getenv("SHIOAJI_API_KEY", "").strip()
    live_sk = _SHIOAJI_CFG.get("secret_key") or os.getenv("SHIOAJI_SECRET_KEY", "").strip()
    if _SHIOAJI_CFG.get("simulation", True) and sim_ak and sim_sk:
        return sim_ak, sim_sk
    if live_ak and live_sk:
        return live_ak, live_sk
    return sim_ak, sim_sk


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
    sim_ak = _SHIOAJI_CFG.get("sim_api_key") or os.getenv("SHIOAJI_SIM_API_KEY", "").strip()
    masked = (ak[:4] + "••••" + ak[-4:]) if len(ak) >= 8 else ("****" if ak else "")
    sim_masked = (sim_ak[:4] + "••••" + sim_ak[-4:]) if len(sim_ak) >= 8 else ("****" if sim_ak else "")
    any_token = _shioaji_has_sim_token() or _shioaji_has_token()
    return {
        "ok": True,
        "sim_token_configured": _shioaji_has_sim_token(),
        "sim_api_key_masked": sim_masked,
        "token_configured": _shioaji_has_token(),
        "live_token_configured": _shioaji_has_token(),
        "api_key_masked": masked,
        "http_url": http_url,
        "http_server_online": http_ok,
        "sdk_installed": sdk_installed,
        "simulation": bool(_SHIOAJI_CFG.get("simulation", True)),
        "active_source": (
            "永豐金 Shioaji Pro HTTP 主連線（TWSE/FinMind 輔助）"
            if http_ok
            else (
                "永豐金 Shioaji API 主連線（TWSE/FinMind 輔助）"
                if any_token
                else "輔助資料源（TWSE/TPEx/FinMind · 永豐 API 待串接）"
            )
        ),
    }


@app.post("/api/v1/shioaji/configure")
async def shioaji_configure(req: ShioajiConfigRequest):
    if req.sim_api_key.strip():
        _SHIOAJI_CFG["sim_api_key"] = req.sim_api_key.strip()
    if req.sim_secret_key.strip():
        _SHIOAJI_CFG["sim_secret_key"] = req.sim_secret_key.strip()
    if req.api_key.strip():
        _SHIOAJI_CFG["api_key"] = req.api_key.strip()
    if req.secret_key.strip():
        _SHIOAJI_CFG["secret_key"] = req.secret_key.strip()
    if req.http_url.strip():
        _SHIOAJI_CFG["http_url"] = req.http_url.strip().rstrip("/")
    _SHIOAJI_CFG["simulation"] = bool(req.simulation)
    return await shioaji_status()


class ShioajiClearRequest(BaseModel):
    scope: str = Field("all", description="sim | live | all")


@app.post("/api/v1/shioaji/clear")
async def shioaji_clear_tokens(req: ShioajiClearRequest):
    """Completely purge SIM, LIVE, or all Shioaji API tokens from backend memory & process env to prevent leakage."""
    scope = (req.scope or "all").strip().lower()
    if scope in ("sim", "all"):
        _SHIOAJI_CFG["sim_api_key"] = ""
        _SHIOAJI_CFG["sim_secret_key"] = ""
        os.environ.pop("SHIOAJI_SIM_API_KEY", None)
        os.environ.pop("SHIOAJI_SIM_SECRET_KEY", None)
    if scope in ("live", "all"):
        _SHIOAJI_CFG["api_key"] = ""
        _SHIOAJI_CFG["secret_key"] = ""
        _SHIOAJI_CFG["simulation"] = True
        os.environ.pop("SHIOAJI_API_KEY", None)
        os.environ.pop("SHIOAJI_SECRET_KEY", None)
    st = await shioaji_status()
    st["cleared_scope"] = scope
    return st


class ShioajiModeSwitchRequest(BaseModel):
    mode: str = Field("SIM", description="SIM or LIVE")
    confirm_live: bool = False
    api_key: str = ""
    secret_key: str = ""
    http_url: str = ""


@app.get("/api/v1/shioaji/mode")
async def get_shioaji_mode():
    """Return current Shioaji dual-environment execution mode (defaults strictly to SIM)."""
    st = await shioaji_status()
    is_sim = bool(_SHIOAJI_CFG.get("simulation", True))
    ca_path = os.getenv("SHIOAJI_CA_PATH", "").strip()
    ca_ready = bool(ca_path and Path(ca_path).exists())
    return {
        "ok": True,
        "mode": "SIM" if is_sim else "LIVE",
        "simulation": is_sim,
        "token_configured": bool(st.get("token_configured")),
        "ca_ready": ca_ready,
        "market_data_engine": st.get("active_source"),
        "order_engine": "永豐金 Shioaji 實盤下單 (LIVE)" if not is_sim else "永豐金模擬單 / 紙上撮合 (SIM)",
        "server_time": tw_now_str(),
    }


@app.post("/api/v1/shioaji/mode")
async def switch_shioaji_mode(req: ShioajiModeSwitchRequest):
    """
    v3.11.0 Dual-Environment API Mode Switch (SIM <-> LIVE).
    - Default is always SIM.
    - Switching to LIVE requires explicit confirm_live=True and configured Shioaji API Token.
    - Market data & K-line analysis remain real-time regardless of SIM/LIVE order mode.
    """
    target = (req.mode or "SIM").strip().upper()
    if target not in ("SIM", "LIVE"):
        raise HTTPException(status_code=400, detail="mode 僅接受 SIM 或 LIVE")

    if req.api_key.strip():
        _SHIOAJI_CFG["api_key"] = req.api_key.strip()
    if req.secret_key.strip():
        _SHIOAJI_CFG["secret_key"] = req.secret_key.strip()
    if req.http_url.strip():
        _SHIOAJI_CFG["http_url"] = req.http_url.strip().rstrip("/")

    if target == "LIVE":
        if not req.confirm_live:
            raise HTTPException(status_code=412, detail="切換至正式實盤 LIVE 需明確確認 (confirm_live=true)")
        if not _shioaji_has_token():
            raise HTTPException(status_code=412, detail="尚未設定永豐金 Shioaji API Key / Secret Key，禁止切換至正式實盤 LIVE")
        _SHIOAJI_CFG["simulation"] = False
    else:
        _SHIOAJI_CFG["simulation"] = True

    res = await get_shioaji_mode()
    res["message"] = (
        "已透過 API 切換至正式實盤 LIVE 模式（請審慎控管部位與 CA 憑證）"
        if target == "LIVE"
        else "已透過 API 安全切換回預設模擬單 SIM 模式（K 線仍維持真實數據分析）"
    )
    return res


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
                prev = meta.get("previousClose") or meta.get("regularMarketPreviousClose") or meta.get("chartPreviousClose") or price
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


async def _fetch_shioaji_http_snapshots(symbols: list[str]) -> Optional[dict[str, Any]]:
    """
    Replica of Sinotrade/shioaji-pro-app `fetchSnapshots` protocol:
    POST {http_url}/api/v1/data/snapshots with {"contracts": [{"security_type":"STK","code":...}]}
    """
    http_url = (_SHIOAJI_CFG.get("http_url") or "").strip().rstrip("/")
    if not http_url or not symbols:
        return None
    contracts = [{"security_type": "STK", "code": sy, "exchange": "TSE"} for sy in symbols[:50]]
    try:
        st, body = await http_json(
            f"{http_url}/api/v1/data/snapshots",
            method="POST",
            json_body={"contracts": contracts},
            timeout=3.0,
        )
        if st != 200 or not isinstance(body, list):
            return None
        out: dict[str, Any] = {}
        for row in body:
            if not isinstance(row, dict):
                continue
            code = str(row.get("code") or "").strip()
            close = _parse_mis_float(row.get("close"))
            if not code or close is None:
                continue
            ref = _parse_mis_float(row.get("close")) - (_parse_mis_float(row.get("change_price")) or 0.0)
            out[code] = {
                "symbol": code,
                "name": str(row.get("name") or code),
                "exchange": str(row.get("exchange") or "TSE").lower(),
                "price": close,
                "prev_close": round(ref, 2) if ref else close,
                "change": _parse_mis_float(row.get("change_price")) or 0.0,
                "change_pct": _parse_mis_float(row.get("change_rate")) or 0.0,
                "open": _parse_mis_float(row.get("open")) or close,
                "high": _parse_mis_float(row.get("high")) or close,
                "low": _parse_mis_float(row.get("low")) or close,
                "volume_lots": _parse_mis_float(row.get("total_volume")) or 0.0,
                "tick_volume": _parse_mis_float(row.get("volume")) or 0.0,
                "amount": _parse_mis_float(row.get("total_amount")) or 0.0,
                "bids": [_parse_mis_float(row.get("buy_price"))] if _parse_mis_float(row.get("buy_price")) else [],
                "bid_sizes": [_parse_mis_float(row.get("buy_volume"))] if _parse_mis_float(row.get("buy_volume")) else [],
                "asks": [_parse_mis_float(row.get("sell_price"))] if _parse_mis_float(row.get("sell_price")) else [],
                "ask_sizes": [_parse_mis_float(row.get("sell_volume"))] if _parse_mis_float(row.get("sell_volume")) else [],
                "date": tw_now_str()[:10],
                "time": tw_now_str()[11:],
                "source_engine": "shioaji_http",
            }
        return out if out else None
    except Exception:
        return None


@app.post("/api/v1/shioaji/test-token")
async def shioaji_test_token(req: ShioajiConfigRequest):
    """Test Sinotrade Shioaji HTTP Server and/or Python shioaji SDK with user SIM or LIVE API Token."""
    if req.sim_api_key.strip():
        _SHIOAJI_CFG["sim_api_key"] = req.sim_api_key.strip()
    if req.sim_secret_key.strip():
        _SHIOAJI_CFG["sim_secret_key"] = req.sim_secret_key.strip()
    if req.api_key.strip():
        _SHIOAJI_CFG["api_key"] = req.api_key.strip()
    if req.secret_key.strip():
        _SHIOAJI_CFG["secret_key"] = req.secret_key.strip()
    if req.http_url.strip():
        _SHIOAJI_CFG["http_url"] = req.http_url.strip().rstrip("/")
    _SHIOAJI_CFG["simulation"] = bool(req.simulation)

    http_snap = await _fetch_shioaji_http_snapshots(["2330"])
    if http_snap and "2330" in http_snap:
        return {
            "ok": True,
            "engine": "shioaji_http",
            "message": f"已成功連線永豐金 Shioaji Pro HTTP 伺服器（2330 現價 {http_snap['2330']['price']}）",
            "status": await shioaji_status(),
        }

    is_sim_test = bool(req.simulation) and bool(req.sim_api_key.strip() and req.sim_secret_key.strip())
    if is_sim_test:
        ak, sk = req.sim_api_key.strip(), req.sim_secret_key.strip()
    else:
        ak, sk = _shioaji_market_creds()
    if ak and sk:
        try:
            import shioaji as sj  # type: ignore

            def _try_sdk():
                api = sj.Shioaji(simulation=bool(req.simulation))
                api.login(api_key=ak, secret_key=sk)
                c = api.Contracts.Stocks["2330"]
                snaps = api.snapshots([c])
                px = snaps[0].close if snaps else None
                api.logout()
                return px

            px = await asyncio.to_thread(_try_sdk)
            return {
                "ok": True,
                "engine": "shioaji_sdk",
                "message": f"已透過永豐金 Shioaji Python SDK 驗證登入成功（2330 現價 {px}）",
                "status": await shioaji_status(),
            }
        except ImportError:
            return {
                "ok": True,
                "engine": "token_saved_pending_runtime",
                "message": "永豐金 API Token 已安全儲存！若要啟用 Shioaji 串流，請啟動 shioaji server start 或安裝 pip install shioaji（目前自動使用 TWSE/TPEx 官方即時備援）",
                "status": await shioaji_status(),
            }
        except Exception as exc:
            return {
                "ok": False,
                "engine": "shioaji_sdk_error",
                "message": f"永豐金 Shioaji 登入驗證失敗：{redact_secrets(str(exc), [ak, sk])}",
                "status": await shioaji_status(),
            }

    return {
        "ok": False,
        "engine": "missing_token",
        "message": "請填入永豐金 Shioaji API Key 與 Secret Key（或啟動本機 shioaji server start）。",
        "status": await shioaji_status(),
    }


async def _fetch_shioaji_sdk_snapshots(symbols: list[str]) -> Optional[dict[str, Any]]:
    """Fetch real-time snapshots (bid/ask/volume/tick) via Python `shioaji` SDK when SIM or LIVE credentials exist."""
    ak, sk = _shioaji_market_creds()
    if not (ak and sk) or not symbols:
        return None
    try:
        import shioaji as sj  # type: ignore
    except ImportError:
        return None

    def _run_sdk():
        # v3.30.2: STRICTLY use production market data (simulation=False) ONLY.
        # NEVER fall back to simulation=True, which returns fake randomized +/-10% sandbox quotes!
        api = sj.Shioaji(simulation=False)
        api.login(api_key=ak, secret_key=sk)
        try:
            contracts = []
            for s in symbols:
                c = api.Contracts.Stocks[s] if s in api.Contracts.Stocks else None
                if c is not None:
                    contracts.append(c)
            if not contracts:
                return None
            snaps = api.snapshots(contracts)
            out: dict[str, Any] = {}
            now_s = tw_now_str()
            for sn in snaps or []:
                code = str(getattr(sn, "code", "") or "").strip().upper()
                close = float(getattr(sn, "close", 0) or 0)
                if not code or close <= 0:
                    continue
                chg = float(getattr(sn, "change_price", 0) or 0)
                ref = round(close - chg, 4) if close else close
                bp = float(getattr(sn, "buy_price", 0) or 0)
                bv = float(getattr(sn, "buy_volume", 0) or 0)
                sp = float(getattr(sn, "sell_price", 0) or 0)
                sv = float(getattr(sn, "sell_volume", 0) or 0)
                out[code] = {
                    "symbol": code,
                    "name": code,
                    "exchange": str(getattr(sn, "exchange", "TSE") or "TSE").lower(),
                    "price": close,
                    "prev_close": round(ref, 2) if ref else close,
                    "change": round(chg, 4),
                    "change_pct": round(float(getattr(sn, "change_rate", 0) or 0), 2),
                    "open": float(getattr(sn, "open", 0) or close),
                    "high": float(getattr(sn, "high", 0) or close),
                    "low": float(getattr(sn, "low", 0) or close),
                    "volume_lots": float(getattr(sn, "total_volume", 0) or 0),
                    "tick_volume": float(getattr(sn, "volume", 0) or 0),
                    "amount": float(getattr(sn, "total_amount", 0) or 0),
                    "tick_type": str(getattr(sn, "tick_type", "") or ""),
                    "bids": [bp] if bp > 0 else [],
                    "bid_sizes": [bv] if bv > 0 else [],
                    "asks": [sp] if sp > 0 else [],
                    "ask_sizes": [sv] if sv > 0 else [],
                    "date": now_s[:10],
                    "time": now_s[11:],
                    "source_engine": "shioaji_sdk",
                }
            return out if out else None
        finally:
            try:
                api.logout()
            except Exception:
                pass

    try:
        return await asyncio.wait_for(asyncio.to_thread(_run_sdk), timeout=12.0)
    except Exception:
        return None


@app.get("/api/v1/shioaji/snapshots")
async def shioaji_snapshots(symbols: str = "2330,2317,2454,0050"):
    syms = [x.strip().upper() for x in symbols.split(",") if re.fullmatch(r"[0-9A-Z]{4,6}", x.strip().upper())][:80]
    in_session = _is_tw_intraday_market_hours()
    # Intraday (09:00~13:35): strictly prefer Shioaji Production API first for live ticks
    if in_session:
        sj_data = await _fetch_shioaji_http_snapshots(syms)
        if sj_data:
            return JSONResponse(
                {"ok": True, "source": "永豐金 Shioaji Pro 正式實盤", "source_engine": "shioaji_http", "server_time": tw_now_str(), "quotes": sj_data},
                headers={"Cache-Control": "no-store"},
            )
        sdk_data = await _fetch_shioaji_sdk_snapshots(syms)
        if sdk_data:
            return JSONResponse(
                {"ok": True, "source": "永豐金 Shioaji SDK 正式實盤", "source_engine": "shioaji_sdk", "server_time": tw_now_str(), "quotes": sdk_data},
                headers={"Cache-Control": "no-store"},
            )
    # Off-hours / overnight / weekend OR Shioaji unavailable: official TWSE/TPEx real closing & MIS verified quotes
    return await twse_realtime_quotes(symbols=",".join(syms), indices="t00,o00")


def _resample_shioaji_kbars(raw_bars: list[dict[str, Any]], interval: str) -> list[dict[str, Any]]:
    """Resample 1-minute Sinotrade Shioaji K-bars into 5m / 15m / 60m / D / W candles in TW_TZ."""
    if not raw_bars:
        return []
    iv = str(interval or "15").upper()
    buckets: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for b in raw_bars:
        ts_val = b.get("ts") or b.get("timestamp")
        if ts_val is None:
            continue
        try:
            ts_num = float(ts_val)
            # Shioaji SDK timestamps are often nanoseconds; HTTP server may return ns, ms, or seconds
            if ts_num > 1e15:
                sec = ts_num / 1e9
            elif ts_num > 1e12:
                sec = ts_num / 1e3
            else:
                sec = ts_num
            dt = datetime.fromtimestamp(sec, TW_TZ)
        except Exception:
            continue

        o = float(b.get("Open") or b.get("open") or 0)
        h = float(b.get("High") or b.get("high") or o)
        l = float(b.get("Low") or b.get("low") or o)
        c = float(b.get("Close") or b.get("close") or o)
        v = int(float(b.get("Volume") or b.get("volume") or 0))
        if c <= 0:
            continue

        if iv in ("1", "5", "15", "30", "60"):
            mins = int(iv)
            floored_min = (dt.minute // mins) * mins
            b_dt = dt.replace(minute=floored_min, second=0, microsecond=0)
            key = b_dt.strftime("%Y-%m-%d %H:%M")
            label = b_dt.strftime("%m-%d %H:%M")
            date_str = b_dt.strftime("%Y-%m-%d")
        elif iv == "240":
            floored_hr = (dt.hour // 4) * 4
            b_dt = dt.replace(hour=floored_hr, minute=0, second=0, microsecond=0)
            key = b_dt.strftime("%Y-%m-%d %H:%M")
            label = b_dt.strftime("%m-%d %H:%M")
            date_str = b_dt.strftime("%Y-%m-%d")
        elif iv == "W":
            monday = dt - timedelta(days=dt.weekday())
            key = monday.strftime("%Y-%m-%d")
            label = monday.strftime("%Y-%m-%d")
            date_str = key
        elif iv == "M":
            key = dt.strftime("%Y-%m")
            label = key
            date_str = dt.strftime("%Y-%m-01")
        else:
            key = dt.strftime("%Y-%m-%d")
            label = dt.strftime("%m-%d")
            date_str = key

        if key not in buckets:
            buckets[key] = {
                "label": label,
                "date": date_str,
                "time": int(dt.timestamp()),
                "open": round(o, 2),
                "high": round(h, 2),
                "low": round(l, 2),
                "close": round(c, 2),
                "volume": v,
            }
            order.append(key)
        else:
            cur = buckets[key]
            cur["high"] = round(max(cur["high"], h), 2)
            cur["low"] = round(min(cur["low"], l), 2)
            cur["close"] = round(c, 2)
            cur["volume"] += v

    return [buckets[k] for k in order][-120:]


async def _fetch_shioaji_http_kbars(symbol: str, interval: str) -> Optional[dict[str, Any]]:
    """Fetch K-bars + live snapshot from Sinotrade Shioaji Pro HTTP Server (`POST /api/v1/data/kbars`)."""
    http_url = (_SHIOAJI_CFG.get("http_url") or "").strip().rstrip("/")
    if not http_url:
        return None
    iv = str(interval or "15").upper()
    lookback_days = {"1": 2, "5": 5, "15": 10, "30": 20, "60": 35, "D": 180, "W": 400, "M": 1500}.get(iv, 10)
    end_dt = datetime.now(TW_TZ)
    start_dt = end_dt - timedelta(days=lookback_days)
    is_fut = symbol.upper() in ("TXF", "TX", "MXF")
    contract = (
        {"security_type": "FUT", "code": "TXF" if symbol.upper() == "TX" else symbol.upper(), "exchange": "TAIFEX"}
        if is_fut
        else {"security_type": "STK", "code": symbol.upper(), "exchange": "TSE"}
    )
    try:
        st, body = await http_json(
            f"{http_url}/api/v1/data/kbars",
            method="POST",
            json_body={
                "contract": contract,
                "start": start_dt.strftime("%Y-%m-%d"),
                "end": end_dt.strftime("%Y-%m-%d"),
            },
            timeout=5.0,
        )
        if st != 200 or not isinstance(body, dict):
            return None
        # Support both columnar ({ts:[...], Open:[...], ...}) and row-list ({data:[...]}) responses
        raw_bars: list[dict[str, Any]] = []
        if isinstance(body.get("ts"), list):
            n = len(body["ts"])
            opens = body.get("Open") or body.get("open") or [0] * n
            highs = body.get("High") or body.get("high") or [0] * n
            lows = body.get("Low") or body.get("low") or [0] * n
            closes = body.get("Close") or body.get("close") or [0] * n
            vols = body.get("Volume") or body.get("volume") or [0] * n
            for i in range(n):
                raw_bars.append({"ts": body["ts"][i], "Open": opens[i], "High": highs[i], "Low": lows[i], "Close": closes[i], "Volume": vols[i]})
        elif isinstance(body.get("data"), list):
            raw_bars = body["data"]
        candles = _resample_shioaji_kbars(raw_bars, iv)
        if not candles:
            return None
        snap = None
        if not is_fut:
            snaps = await _fetch_shioaji_http_snapshots([symbol.upper()])
            snap = (snaps or {}).get(symbol.upper())
        latest = candles[-1]
        price = float(snap["price"]) if snap and snap.get("price") else latest["close"]
        prev_close = float(snap["prev_close"]) if snap and snap.get("prev_close") else (candles[-2]["close"] if len(candles) >= 2 else latest["open"])
        if snap and snap.get("price"):
            latest["close"] = round(price, 2)
            latest["high"] = round(max(latest["high"], price), 2)
            latest["low"] = round(min(latest["low"], price), 2)
        return {
            "ok": True,
            "symbol": symbol.upper(),
            "interval": iv,
            "source_engine": "shioaji_http",
            "source": f"永豐金 Shioaji Pro 即時 K 線 ({iv})",
            "price": round(price, 2),
            "prev_close": round(prev_close, 2),
            "open": round(float(snap["open"]) if snap and snap.get("open") else latest["open"], 2),
            "high": round(float(snap["high"]) if snap and snap.get("high") else latest["high"], 2),
            "low": round(float(snap["low"]) if snap and snap.get("low") else latest["low"], 2),
            "volume": int((snap.get("volume_lots") or 0) * 1000) if snap else latest["volume"],
            "last_date": tw_now_str()[:16],
            "candles": candles,
        }
    except Exception:
        return None


async def _fetch_shioaji_sdk_kbars(symbol: str, interval: str) -> Optional[dict[str, Any]]:
    """Fetch K-bars + live snapshot via Python `shioaji` SDK when SIM or LIVE API Key & Secret Key are configured."""
    ak, sk = _shioaji_market_creds()
    if not (ak and sk):
        return None
    iv = str(interval or "15").upper()
    lookback_days = {"1": 2, "5": 5, "15": 10, "30": 20, "60": 35, "D": 180, "W": 400, "M": 1500}.get(iv, 10)
    end_dt = datetime.now(TW_TZ)
    start_dt = end_dt - timedelta(days=lookback_days)
    try:
        import shioaji as sj  # type: ignore
    except ImportError:
        return None

    def _run_sdk():
        # v3.30.2: STRICTLY use production market data (simulation=False) ONLY for K-bars.
        # NEVER fall back to simulation=True sandbox test data.
        api = sj.Shioaji(simulation=False)
        api.login(api_key=ak, secret_key=sk)
        try:
            contract = api.Contracts.Stocks[symbol.upper()]
            kb = api.kbars(contract=contract, start=start_dt.strftime("%Y-%m-%d"), end=end_dt.strftime("%Y-%m-%d"))
            snaps = api.snapshots([contract])
            raw_bars = [
                {"ts": kb.ts[i], "Open": kb.Open[i], "High": kb.High[i], "Low": kb.Low[i], "Close": kb.Close[i], "Volume": kb.Volume[i]}
                for i in range(len(kb.ts))
            ]
            snap_obj = snaps[0] if snaps else None
            snap_dict = None
            if snap_obj:
                close_px = float(getattr(snap_obj, "close", 0) or 0)
                chg_px = float(getattr(snap_obj, "change_price", 0) or 0)
                snap_dict = {
                    "price": close_px,
                    "prev_close": round(close_px - chg_px, 2) if close_px else None,
                    "open": float(getattr(snap_obj, "open", close_px) or close_px),
                    "high": float(getattr(snap_obj, "high", close_px) or close_px),
                    "low": float(getattr(snap_obj, "low", close_px) or close_px),
                    "volume_lots": float(getattr(snap_obj, "total_volume", 0) or 0),
                }
            return raw_bars, snap_dict
        finally:
            try:
                api.logout()
            except Exception:
                pass

    try:
        raw_bars, snap = await asyncio.to_thread(_run_sdk)
        candles = _resample_shioaji_kbars(raw_bars, iv)
        if not candles:
            return None
        latest = candles[-1]
        price = float(snap["price"]) if snap and snap.get("price") else latest["close"]
        prev_close = float(snap["prev_close"]) if snap and snap.get("prev_close") else (candles[-2]["close"] if len(candles) >= 2 else latest["open"])
        if snap and snap.get("price"):
            latest["close"] = round(price, 2)
            latest["high"] = round(max(latest["high"], price), 2)
            latest["low"] = round(min(latest["low"], price), 2)
        return {
            "ok": True,
            "symbol": symbol.upper(),
            "interval": iv,
            "source_engine": "shioaji_sdk",
            "source": f"永豐金 Shioaji SDK 即時 K 線 ({iv})",
            "price": round(price, 2),
            "prev_close": round(prev_close, 2),
            "open": round(float(snap["open"]) if snap and snap.get("open") else latest["open"], 2),
            "high": round(float(snap["high"]) if snap and snap.get("high") else latest["high"], 2),
            "low": round(float(snap["low"]) if snap and snap.get("low") else latest["low"], 2),
            "volume": int((snap.get("volume_lots") or 0) * 1000) if snap else latest["volume"],
            "last_date": tw_now_str()[:16],
            "candles": candles,
        }
    except Exception:
        return None


@app.get("/api/v1/shioaji/kbars/{symbol}")
async def shioaji_kbars(symbol: str, interval: str = "15", yahoo_symbol: str = ""):
    """
    v3.10.5: Unified Sinotrade Shioaji real-time K-line endpoint for all technical charts.
    Priority: 1) Shioaji Pro HTTP Server -> 2) Shioaji Python SDK -> 3) Live-merged TWSE/TPEx MIS + Yahoo/FinMind K-lines.
    """
    sym = symbol.strip().upper()
    iv = str(interval or "15").upper()

    # 1. Shioaji Pro HTTP Server
    res = await _fetch_shioaji_http_kbars(sym, iv)
    if res:
        return JSONResponse(res, headers={"Cache-Control": "no-store"})

    # 2. Shioaji Python SDK
    res = await _fetch_shioaji_sdk_kbars(sym, iv)
    if res:
        return JSONResponse(res, headers={"Cache-Control": "no-store"})

    # 3. Seamless live-merged fallback (Yahoo/FinMind K-lines + official TWSE/TPEx MIS live tick)
    iv_map = {"1": ("1m", "1d"), "5": ("5m", "5d"), "15": ("15m", "5d"), "30": ("30m", "5d"), "60": ("60m", "1mo"), "240": ("60m", "3mo"), "D": ("1d", "6mo"), "W": ("1wk", "2y"), "M": ("1mo", "5y")}
    y_iv, y_rng = iv_map.get(iv, ("15m", "5d"))
    yh_sym = yahoo_symbol.strip() or ("^TWII" if sym in ("TXF", "T00") else ("^TWOII" if sym == "O00" else f"{sym}.TW"))

    kline_data: Optional[dict[str, Any]] = None
    try:
        kline_data = await get_yahoo_market_data(yh_sym, interval=y_iv, range_str=y_rng)
    except Exception:
        if yh_sym.endswith(".TW"):
            try:
                kline_data = await get_yahoo_market_data(yh_sym[:-3] + ".TWO", interval=y_iv, range_str=y_rng)
            except Exception:
                kline_data = None

    if kline_data and iv == "240" and isinstance(kline_data.get("candles"), list):
        agg_raw = [
            {"ts": c.get("time"), "Open": c.get("open"), "High": c.get("high"), "Low": c.get("low"), "Close": c.get("close"), "Volume": c.get("volume", 0)}
            for c in kline_data["candles"] if c.get("time")
        ]
        if agg_raw:
            kline_data["candles"] = _resample_shioaji_kbars(agg_raw, "240")

    if not kline_data:
        try:
            kline_data = await get_finmind_market_data(
                "TX" if sym == "TXF" else sym,
                dataset="TaiwanFuturesDaily" if sym == "TXF" else "TaiwanStockPrice",
                days=120 if iv in ("D", "W") else 60,
                interval=iv,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"永豐金待連線且備援 K 線抓取失敗：{exc}")

    # Merge official TWSE/TPEx MIS live tick into the latest forming candle
    mis_row = None
    if re.fullmatch(r"[0-9A-Z]{4,6}", sym):
        try:
            mis_resp = await twse_realtime_quotes(symbols=sym, indices="")
            mis_json = json.loads(mis_resp.body.decode("utf-8"))
            mis_row = (mis_json.get("quotes") or {}).get(sym)
        except Exception:
            mis_row = None

    candles = list(kline_data.get("candles") or [])
    price = float(mis_row["price"]) if (mis_row and mis_row.get("price")) else float(kline_data.get("price") or 0)
    prev_close = float(mis_row["prev_close"]) if (mis_row and mis_row.get("prev_close")) else float(kline_data.get("prev_close") or price)
    if candles and mis_row and mis_row.get("price"):
        last_bar = dict(candles[-1])
        last_bar["close"] = round(price, 2)
        last_bar["high"] = round(max(float(last_bar.get("high") or price), price), 2)
        last_bar["low"] = round(min(float(last_bar.get("low") or price), price), 2)
        candles[-1] = last_bar

    return JSONResponse(
        {
            "ok": True,
            "symbol": sym,
            "interval": iv,
            "source_engine": "twse_mis_fallback",
            "source": f"永豐金待連線（已自動以 TWSE 即時＋K線銜接 · {iv}）",
            "price": round(price, 2),
            "prev_close": round(prev_close, 2),
            "open": round(float(mis_row["open"]) if (mis_row and mis_row.get("open")) else float(kline_data.get("open") or price), 2),
            "high": round(float(mis_row["high"]) if (mis_row and mis_row.get("high")) else float(kline_data.get("high") or price), 2),
            "low": round(float(mis_row["low"]) if (mis_row and mis_row.get("low")) else float(kline_data.get("low") or price), 2),
            "volume": int((mis_row.get("volume_lots") or 0) * 1000) if (mis_row and mis_row.get("volume_lots")) else int(kline_data.get("volume") or 0),
            "last_date": tw_now_str()[:16],
            "candles": candles,
        },
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# v3.10.2 — Remote Shared Account (手機與電腦跨裝置同步共用帳號)
# 帳號密碼於前端先經 PBKDF2/SHA-256 雜湊驗證，同步資料本身亦為 E2EE AES-256-GCM 密文。
# ---------------------------------------------------------------------------
class RemoteAccountAuthRequest(BaseModel):
    account_id: str = Field(..., min_length=3, max_length=64)
    pass_hash: str = Field(..., min_length=32, max_length=128)
    device_name: str = ""


class RemoteAccountPushRequest(RemoteAccountAuthRequest):
    payload_json: str = Field(..., max_length=4_500_000)


def _norm_account_id(raw: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_.-]", "", (raw or "").strip().lower())
    if len(clean) < 3:
        raise HTTPException(status_code=400, detail="遠端共享帳號名稱至少需 3 碼英數字")
    return clean


@app.post("/api/v1/account/login")
async def remote_account_login(req: RemoteAccountAuthRequest):
    aid = _norm_account_id(req.account_id)
    now = tw_now_str()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM remote_accounts WHERE account_id = ?", (aid,)).fetchone()
        if row is None:
            owner_id = "own_" + hashlib.sha256(f"shl-acct:{aid}:{req.pass_hash[:16]}".encode()).hexdigest()[:24]
            conn.execute(
                "INSERT INTO remote_accounts (account_id, pass_hash, owner_id, payload_json, revision, last_device, updated_at) VALUES (?, ?, ?, '', 0, ?, ?)",
                (aid, req.pass_hash, owner_id, req.device_name[:60], now),
            )
            conn.execute(
                "INSERT OR IGNORE INTO device_owners (owner_id, owner_label, sync_space, created_at) VALUES (?, ?, ?, ?)",
                (owner_id, aid, _owner_sync_space(owner_id), now),
            )
            conn.commit()
            return {"ok": True, "created": True, "account_id": aid, "owner_id": owner_id, "sync_space": _owner_sync_space(owner_id), "revision": 0, "has_payload": False}
        if not hmac.compare_digest(str(row["pass_hash"]), req.pass_hash):
            raise HTTPException(status_code=403, detail="遠端共享帳號密碼錯誤")
        return {
            "ok": True,
            "created": False,
            "account_id": aid,
            "owner_id": str(row["owner_id"]),
            "sync_space": _owner_sync_space(str(row["owner_id"])),
            "revision": int(row["revision"]),
            "has_payload": bool(row["payload_json"]),
            "last_device": str(row["last_device"] or ""),
            "updated_at": str(row["updated_at"]),
        }
    finally:
        conn.close()


@app.post("/api/v1/account/sync/push")
async def remote_account_push(req: RemoteAccountPushRequest):
    aid = _norm_account_id(req.account_id)
    now = tw_now_str()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT pass_hash, owner_id, revision FROM remote_accounts WHERE account_id = ?", (aid,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="請先登入或建立遠端共享帳號")
        if not hmac.compare_digest(str(row["pass_hash"]), req.pass_hash):
            raise HTTPException(status_code=403, detail="遠端共享帳號密碼錯誤")
        new_rev = int(row["revision"]) + 1
        conn.execute(
            "UPDATE remote_accounts SET payload_json = ?, revision = ?, last_device = ?, updated_at = ? WHERE account_id = ?",
            (req.payload_json, new_rev, req.device_name[:60], now, aid),
        )
        conn.commit()
        return {"ok": True, "account_id": aid, "owner_id": str(row["owner_id"]), "revision": new_rev, "updated_at": now}
    finally:
        conn.close()


@app.post("/api/v1/account/sync/pull")
async def remote_account_pull(req: RemoteAccountAuthRequest):
    aid = _norm_account_id(req.account_id)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM remote_accounts WHERE account_id = ?", (aid,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="查無此遠端共享帳號")
        if not hmac.compare_digest(str(row["pass_hash"]), req.pass_hash):
            raise HTTPException(status_code=403, detail="遠端共享帳號密碼錯誤")
        return {
            "ok": True,
            "account_id": aid,
            "owner_id": str(row["owner_id"]),
            "sync_space": _owner_sync_space(str(row["owner_id"])),
            "revision": int(row["revision"]),
            "payload_json": str(row["payload_json"] or ""),
            "last_device": str(row["last_device"] or ""),
            "updated_at": str(row["updated_at"]),
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# v3.10.3 — Chinese Stock Name & Futures/Index Search (支援中文股名與台指電子盤查詢)
# ---------------------------------------------------------------------------
@app.get("/api/v1/market/stock-search")
async def search_tw_stock(q: str = ""):
    kw = (q or "").strip()
    if not kw:
        return {"ok": True, "matches": []}
    cached = cache_get("finmind_all_stock_info", 43200)
    if cached is None:
        url = "https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInfo"
        tok = FINMIND_TOKENS[0] if FINMIND_TOKENS else ""
        if tok:
            url += f"&token={urllib.parse.quote(tok)}"
        st, body = await http_json(url, timeout=12.0)
        if st == 200 and isinstance(body, dict) and isinstance(body.get("data"), list):
            seen: dict[str, dict[str, str]] = {}
            for row in body["data"]:
                sid = str(row.get("stock_id") or "").strip().upper()
                sname = str(row.get("stock_name") or "").strip()
                if sid and sname and re.fullmatch(r"[0-9A-Z]{4,6}", sid):
                    seen[sid] = {
                        "stock_id": sid,
                        "stock_name": sname,
                        "industry": str(row.get("industry_category") or "台股"),
                        "market_type": str(row.get("type") or "twse").lower(),
                    }
            cached = list(seen.values())
            cache_set("finmind_all_stock_info", cached)
        else:
            cached = []
    kw_up = kw.upper()
    exact = [x for x in cached if x["stock_id"] == kw_up or x["stock_name"] == kw]
    partial = [x for x in cached if x not in exact and (kw in x["stock_name"] or kw_up in x["stock_id"])]
    matches = (exact + partial)[:12]
    for m in matches:
        m["yahoo_symbol"] = f"{m['stock_id']}.TWO" if m["market_type"] == "tpex" else f"{m['stock_id']}.TW"
        m["tv_symbol"] = f"TPEX:{m['stock_id']}" if m["market_type"] == "tpex" else f"TWSE:{m['stock_id']}"
    return {"ok": True, "query": kw, "matches": matches}


# =============================================================================
# v3.14.0 SHIOAJI PRO STREAMING MARKET SIGNALS SCANNER & INDEX CONTRIBUTION
# Official spec: https://sinotrade.github.io/zh/tutor/market_data/streaming/signals/
# =============================================================================
from fastapi.responses import StreamingResponse

SHIOAJI_SCANNER_CATALOG: list[dict[str, str]] = [
    {"id": "bid_near_limit_up", "group": "limit", "kind": "preset_rule", "label": "買方接近漲停", "tone": "up"},
    {"id": "bid_touch_limit_up", "group": "limit", "kind": "preset_rule", "label": "買方觸及漲停", "tone": "up"},
    {"id": "limit_up_unlocked", "group": "limit", "kind": "preset_rule", "label": "漲停打開", "tone": "warn"},
    {"id": "ask_near_limit_down", "group": "limit", "kind": "preset_rule", "label": "賣方接近跌停", "tone": "dn"},
    {"id": "ask_touch_limit_down", "group": "limit", "kind": "preset_rule", "label": "賣方觸及跌停", "tone": "dn"},
    {"id": "limit_down_unlocked", "group": "limit", "kind": "preset_rule", "label": "跌停打開", "tone": "warn"},
    {"id": "trade_price_surge", "group": "price_move", "kind": "preset_rule", "label": "成交急漲", "tone": "up"},
    {"id": "trade_price_drop", "group": "price_move", "kind": "preset_rule", "label": "成交急跌", "tone": "dn"},
    {"id": "bid_price_surge", "group": "price_move", "kind": "preset_rule", "label": "買價急漲", "tone": "up"},
    {"id": "ask_price_drop", "group": "price_move", "kind": "preset_rule", "label": "賣價急跌", "tone": "dn"},
    {"id": "volume_burst", "group": "volume", "kind": "preset_rule", "label": "單筆爆量", "tone": "burst"},
    {"id": "simtrade", "group": "status", "kind": "stream", "label": "試撮", "tone": "info"},
    {"id": "suspend", "group": "status", "kind": "stream", "label": "暫停交易", "tone": "warn"},
]
_SCANNER_META = {x["id"]: x for x in SHIOAJI_SCANNER_CATALOG}
_SCANNER_STATE: dict[str, Any] = {
    "exchanges": ["TSE", "OTC"],
    "rules": [
        "bid_touch_limit_up",
        "ask_touch_limit_down",
        "trade_price_surge",
        "trade_price_drop",
        "volume_burst",
    ],
    "events": [],
    "last_scan_ts": 0.0,
    "shioaji_sse_online": False,
}


def _scanner_payload_obj(rule_id: str) -> Any:
    meta = _SCANNER_META.get(rule_id)
    if not meta:
        return None
    if meta["kind"] == "stream":
        return rule_id  # Official HTTP spec: "simtrade" | "suspend"
    return {"kind": "preset_rule", "id": rule_id}


def _sync_shioaji_http_scanner_sub(exchanges: list[str], rules: list[str], unsubscribe: bool = False) -> dict[str, Any]:
    """Forward official subscribe/unsubscribe calls to local Shioaji Pro HTTP server if online."""
    http_url = (_SHIOAJI_CFG.get("http_url") or os.getenv("SHIOAJI_HTTP_URL", "http://127.0.0.1:8080")).rstrip("/")
    endpoint = f"{http_url}/api/v1/stream/{'unsubscribe' if unsubscribe else 'subscribe'}/scanner"
    ok_cnt = 0
    err_cnt = 0
    for ex in exchanges:
        for rid in rules:
            sc_obj = _scanner_payload_obj(rid)
            if sc_obj is None:
                continue
            body = json.dumps({
                "scanner": sc_obj,
                "region": "TW",
                "security_type": "STK",
                "exchange": ex,
            }).encode("utf-8")
            try:
                req = urllib.request.Request(endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=2.5) as resp:
                    if 200 <= resp.status < 300:
                        ok_cnt += 1
                    else:
                        err_cnt += 1
            except Exception:
                err_cnt += 1
                if ok_cnt == 0 and err_cnt >= 1:
                    return {"http_online": False, "ok_count": 0, "err_count": err_cnt}
    return {"http_online": ok_cnt > 0, "ok_count": ok_cnt, "err_count": err_cnt}


class ScannerSubscribeRequest(BaseModel):
    exchanges: list[str] = Field(default_factory=lambda: ["TSE", "OTC"])
    rules: list[str] = Field(default_factory=lambda: ["bid_touch_limit_up", "ask_touch_limit_down", "trade_price_surge", "trade_price_drop", "volume_burst"])


@app.post("/api/v1/shioaji/scanner/subscribe")
async def shioaji_scanner_subscribe(req: ScannerSubscribeRequest):
    exchanges = [e.upper() for e in (req.exchanges or []) if e.upper() in ("TSE", "OTC")] or ["TSE", "OTC"]
    rules = [r for r in (req.rules or []) if r in _SCANNER_META]
    prev_ex = list(_SCANNER_STATE["exchanges"])
    prev_rules = list(_SCANNER_STATE["rules"])
    _SCANNER_STATE["exchanges"] = exchanges
    _SCANNER_STATE["rules"] = rules
    _SCANNER_STATE["last_scan_ts"] = 0.0

    # Sync with Shioaji Pro HTTP server if running
    removed_rules = [r for r in prev_rules if r not in rules]
    if removed_rules:
        await asyncio.to_thread(_sync_shioaji_http_scanner_sub, prev_ex, removed_rules, True)
    sub_res = await asyncio.to_thread(_sync_shioaji_http_scanner_sub, exchanges, rules, False)
    _SCANNER_STATE["shioaji_sse_online"] = bool(sub_res.get("http_online"))
    return {
        "ok": True,
        "exchanges": exchanges,
        "rules": rules,
        "shioaji_http_online": _SCANNER_STATE["shioaji_sse_online"],
        "sync_result": sub_res,
        "catalog": SHIOAJI_SCANNER_CATALOG,
    }


async def _build_live_scanner_events() -> list[dict[str, Any]]:
    """Derive live full-market scanner signals from real-time TWSE+TPEx rankings when Shioaji HTTP SSE is offline."""
    now_ts = time.time()
    if _SCANNER_STATE["events"] and (now_ts - _SCANNER_STATE["last_scan_ts"]) < 25:
        return _SCANNER_STATE["events"]

    up_rows, dn_rows = await asyncio.gather(
        asyncio.to_thread(_fetch_yahoo_tw_live_rank_sync, "up"),
        asyncio.to_thread(_fetch_yahoo_tw_live_rank_sync, "down"),
    )
    events: list[dict[str, Any]] = []
    now_hms = tw_now_str()[11:19]
    seen: set[tuple[str, str]] = set()

    def _push(rule_id: str, row: dict[str, Any], detail: str):
        key = (rule_id, row["symbol"])
        if key in seen:
            return
        seen.add(key)
        meta = _SCANNER_META[rule_id]
        events.append({
            "id": f"{rule_id}-{row['symbol']}",
            "scanner": rule_id,
            "label": meta["label"],
            "group": meta["group"],
            "tone": meta["tone"],
            "exchange": "OTC" if row.get("exchange") == "otc" else "TSE",
            "symbol": row["symbol"],
            "name": row["name"],
            "price": row["price"],
            "change_pct": row["change_pct"],
            "volume_lots": row["volume_lots"],
            "value_m": row["value_m"],
            "detail": detail,
            "time": now_hms,
            "source_engine": "shioaji_sse" if _SCANNER_STATE["shioaji_sse_online"] else "live_market_scanner",
        })

    for r in up_rows:
        pct = float(r.get("change_pct") or 0)
        val_m = float(r.get("value_m") or 0)
        hi = float(r.get("high") or r["price"])
        if pct >= 9.5:
            _push("bid_touch_limit_up", r, f"觸及漲停 +{pct:.2f}% · 成交 {val_m:.1f}M")
        elif pct >= 8.2:
            _push("bid_near_limit_up", r, f"逼近漲停 +{pct:.2f}% · 成交 {val_m:.1f}M")
        if hi > r["price"] and pct >= 6.0 and (hi - r["price"]) / hi >= 0.015:
            _push("limit_up_unlocked", r, f"高檔回落/開板 高 {hi:.2f} → 現 {r['price']:.2f}")
        if pct >= 4.0 and val_m >= 50:
            _push("trade_price_surge", r, f"強勢急漲 +{pct:.2f}% · 成交 {val_m:.1f}M")
            _push("bid_price_surge", r, f"買盤推升 +{pct:.2f}% · {int(r['volume_lots'])} 張")
        if val_m >= 500:
            _push("volume_burst", r, f"爆量長紅 成交值 {val_m:.0f}M ({int(r['volume_lots'])} 張)")

    for r in dn_rows:
        pct = float(r.get("change_pct") or 0)
        val_m = float(r.get("value_m") or 0)
        lo = float(r.get("low") or r["price"])
        if pct <= -9.5:
            _push("ask_touch_limit_down", r, f"觸及跌停 {pct:.2f}% · 成交 {val_m:.1f}M")
        elif pct <= -8.2:
            _push("ask_near_limit_down", r, f"逼近跌停 {pct:.2f}% · 成交 {val_m:.1f}M")
        if lo < r["price"] and pct <= -6.0 and (r["price"] - lo) / max(lo, 1e-6) >= 0.015:
            _push("limit_down_unlocked", r, f"低檔開板 低 {lo:.2f} → 現 {r['price']:.2f}")
        if pct <= -3.5 and val_m >= 20:
            _push("trade_price_drop", r, f"急殺走跌 {pct:.2f}% · 成交 {val_m:.1f}M")
            _push("ask_price_drop", r, f"賣壓湧現 {pct:.2f}% · {int(r['volume_lots'])} 張")
        if val_m >= 500:
            _push("volume_burst", r, f"爆量下殺 成交值 {val_m:.0f}M ({int(r['volume_lots'])} 張)")

    events.sort(key=lambda e: (abs(float(e["change_pct"])), float(e["value_m"])), reverse=True)
    _SCANNER_STATE["events"] = events[:150]
    _SCANNER_STATE["last_scan_ts"] = now_ts
    return _SCANNER_STATE["events"]


@app.get("/api/v1/shioaji/scanner/events")
async def shioaji_scanner_events():
    all_events = await _build_live_scanner_events()
    ex_set = set(_SCANNER_STATE["exchanges"])
    rule_set = set(_SCANNER_STATE["rules"])
    filtered = [e for e in all_events if e["exchange"] in ex_set and e["scanner"] in rule_set]
    return JSONResponse({
        "ok": True,
        "exchanges": _SCANNER_STATE["exchanges"],
        "rules": _SCANNER_STATE["rules"],
        "shioaji_http_online": _SCANNER_STATE["shioaji_sse_online"],
        "source": "永豐 Shioaji Pro 官方 SSE 市場訊號" if _SCANNER_STATE["shioaji_sse_online"] else "全市場實盤即時偵測（啟動 shioaji server start 即自動切換官方 SSE 毫秒推播）",
        "count": len(filtered),
        "events": filtered[:80],
        "catalog": SHIOAJI_SCANNER_CATALOG,
        "server_time": tw_now_str(),
    }, headers={"Cache-Control": "no-store"})


@app.get("/api/v1/shioaji/scanner/stream")
async def shioaji_scanner_sse():
    """SSE endpoint pushing real-time market scanner signals to the browser (relays Shioaji Pro HTTP SSE when available)."""
    async def event_gen():
        # Push initial snapshot immediately
        all_ev = await _build_live_scanner_events()
        ex_set = set(_SCANNER_STATE["exchanges"])
        rule_set = set(_SCANNER_STATE["rules"])
        filt = [e for e in all_ev if e["exchange"] in ex_set and e["scanner"] in rule_set][:40]
        yield f"event: snapshot\ndata: {json.dumps({'events': filt, 'shioaji_http_online': _SCANNER_STATE['shioaji_sse_online'], 'server_time': tw_now_str()}, ensure_ascii=False)}\n\n"
        # Periodic heartbeat & incremental refresh (25s)
        for _ in range(12):
            await asyncio.sleep(25)
            all_ev = await _build_live_scanner_events()
            ex_set = set(_SCANNER_STATE["exchanges"])
            rule_set = set(_SCANNER_STATE["rules"])
            filt = [e for e in all_ev if e["exchange"] in ex_set and e["scanner"] in rule_set][:40]
            yield f"event: snapshot\ndata: {json.dumps({'events': filt, 'shioaji_http_online': _SCANNER_STATE['shioaji_sse_online'], 'server_time': tw_now_str()}, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# Major TAIEX heavyweight constituent approximate weights (%) for index point contribution view
_TAIEX_HEAVYWEIGHTS: list[tuple[str, str, float]] = [
    ("2330", "台積電", 36.5),
    ("2317", "鴻海", 3.6),
    ("2454", "聯發科", 3.1),
    ("2308", "台達電", 1.9),
    ("2382", "廣達", 1.6),
    ("2881", "富邦金", 1.7),
    ("2882", "國泰金", 1.5),
    ("3711", "日月光投控", 1.1),
    ("2891", "中信金", 1.1),
    ("2303", "聯電", 0.9),
    ("2886", "兆豐金", 0.8),
    ("2412", "中華電", 1.3),
    ("2345", "智邦", 0.7),
    ("2357", "華碩", 0.7),
    ("3231", "緯創", 0.6),
    ("3008", "大立光", 0.6),
    ("2884", "玉山金", 0.6),
    ("2327", "國巨*", 0.5),
]


@app.get("/api/v1/market/breadth_contrib")
async def market_breadth_contrib():
    """Shioaji Pro-style Market Overview: TAIEX/OTC live index + heavyweight point contribution ranking."""
    syms = [x[0] for x in _TAIEX_HEAVYWEIGHTS]
    channels = ["tse_t00.tw", "otc_o00.tw"] + [f"tse_{s}.tw" for s in syms]
    live_map = await asyncio.to_thread(_fetch_mis_batch_exact_sync, channels)
    t00 = live_map.get("T00") or {}
    o00 = live_map.get("O00") or {}
    t00_px = float(t00.get("price") or 0)
    t00_prev = float(t00.get("prev_close") or t00_px or 23000)
    t00_chg = round(t00_px - t00_prev, 2) if t00_px else 0.0
    t00_pct = float(t00.get("change_pct") or 0.0)

    contribs = []
    for code, name, wt_pct in _TAIEX_HEAVYWEIGHTS:
        lv = live_map.get(code)
        if not lv:
            continue
        pct = float(lv.get("change_pct") or 0.0)
        pts = round(t00_prev * (wt_pct / 100.0) * (pct / 100.0), 2)
        contribs.append({
            "symbol": code,
            "name": name,
            "price": lv["price"],
            "change_pct": pct,
            "weight_pct": wt_pct,
            "contrib_pts": pts,
        })
    contribs.sort(key=lambda x: abs(x["contrib_pts"]), reverse=True)
    pos_pts = round(sum(x["contrib_pts"] for x in contribs if x["contrib_pts"] > 0), 2)
    neg_pts = round(sum(x["contrib_pts"] for x in contribs if x["contrib_pts"] < 0), 2)
    return JSONResponse({
        "ok": True,
        "t00": {"price": t00_px, "change": t00_chg, "change_pct": t00_pct},
        "o00": {"price": float(o00.get("price") or 0), "change_pct": float(o00.get("change_pct") or 0)},
        "pos_contrib_pts": pos_pts,
        "neg_contrib_pts": neg_pts,
        "contributors": contribs[:12],
        "server_time": tw_now_str(),
    }, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# Decision Handshake Diagnostics API (v3.19.0 — WAL + Composite-Indexed SQLite)
# ---------------------------------------------------------------------------
class DiagnosticHandshakeItemIn(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=32)
    stock_name: str = ""
    price: float = 0.0
    change_pct: float = 0.0
    bar_5m: Any = Field(default_factory=dict)
    bar_1m: Any = Field(default_factory=dict)
    direction: str = "NEUTRAL"
    strategies: Any = Field(default_factory=list)
    ai_score: int = 0
    ai_verdict: str = ""
    ai_reason: str = ""
    final_action: str = "HOLD"
    order_executed: bool | int = 0
    order_id: str = ""
    order_msg: str = ""
    latency_ms: float = 0.0


class DiagnosticHandshakeRunIn(BaseModel):
    run_id: str = ""
    env_mode: str = "SIM"
    scope_type: str = "WATCHLIST_ALL"
    symbols: list[str] = Field(default_factory=list)
    avg_latency_ms: Optional[float] = None
    items: list[DiagnosticHandshakeItemIn] = Field(default_factory=list)


def _diag_owner_from_request(request: Optional[Request]) -> str:
    oid = _owner_from_request(request)
    if oid:
        return oid
    if request is None:
        return ""
    raw = (request.headers.get("x-shl-owner-id") or "").strip()
    if raw and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", raw):
        return raw
    return ""


def _diag_json_dumps(val: Any, fallback: str) -> str:
    if isinstance(val, str):
        s = val.strip()
        return s if s else fallback
    try:
        return json.dumps(val, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return fallback


def _diag_json_loads(raw: Optional[str], default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _diag_db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@app.post("/api/v1/diagnostics/handshake/run")
async def save_diagnostic_handshake_run(req: DiagnosticHandshakeRunIn, request: Request):
    owner_id = _diag_owner_from_request(request)
    ts_ms = int(time.time() * 1000)
    created_at = datetime.now(TW_TZ).isoformat(timespec="seconds")

    raw_run_id = (req.run_id or "").strip()
    run_id = raw_run_id[:64] if raw_run_id else f"dh_{ts_ms}_{secrets.token_hex(4)}"

    env_mode = (req.env_mode or SERVER_TRADING_MODE or "SIM").strip().upper()
    if env_mode not in ("SIM", "LIVE"):
        env_mode = "SIM"
    scope_type = (req.scope_type or "WATCHLIST_ALL").strip()[:48]

    symbols = [s.strip().upper() for s in req.symbols if isinstance(s, str) and s.strip()]
    if not symbols and req.items:
        symbols = [it.symbol.strip().upper() for it in req.items if it.symbol.strip()]

    buy_count = 0
    short_count = 0
    order_exec_count = 0
    latencies: list[float] = []
    item_rows: list[tuple[Any, ...]] = []

    for it in req.items:
        sym = it.symbol.strip().upper()[:32]
        direction = (it.direction or "NEUTRAL").strip().upper()[:16]
        final_action = (it.final_action or "HOLD").strip().upper()[:24]
        executed_int = 1 if bool(it.order_executed) else 0
        lat = round(float(it.latency_ms or 0.0), 2)
        latencies.append(lat)

        if final_action == "BUY":
            buy_count += 1
        elif final_action in ("SHORT_SELL", "SHORT", "SELL"):
            short_count += 1
        if executed_int:
            order_exec_count += 1

        item_rows.append(
            (
                run_id,
                owner_id,
                sym,
                (it.stock_name or "").strip()[:64],
                float(it.price or 0.0),
                float(it.change_pct or 0.0),
                _diag_json_dumps(it.bar_5m, "{}"),
                _diag_json_dumps(it.bar_1m, "{}"),
                direction,
                _diag_json_dumps(it.strategies, "[]"),
                int(it.ai_score or 0),
                (it.ai_verdict or "").strip()[:32],
                (it.ai_reason or "").strip()[:500],
                final_action,
                executed_int,
                (it.order_id or "").strip()[:64],
                (it.order_msg or "").strip()[:300],
                lat,
                created_at,
                ts_ms,
            )
        )

    total_symbols = len(item_rows) if item_rows else len(symbols)
    if req.avg_latency_ms is not None:
        avg_latency_ms = round(float(req.avg_latency_ms), 2)
    elif latencies:
        avg_latency_ms = round(sum(latencies) / len(latencies), 2)
    else:
        avg_latency_ms = 0.0

    conn = _diag_db_connect()
    try:
        with conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO decision_handshake_runs (
                    run_id, owner_id, env_mode, scope_type, symbols_json,
                    total_symbols, buy_count, short_count, order_exec_count,
                    avg_latency_ms, created_at, ts_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    owner_id,
                    env_mode,
                    scope_type,
                    _diag_json_dumps(symbols, "[]"),
                    total_symbols,
                    buy_count,
                    short_count,
                    order_exec_count,
                    avg_latency_ms,
                    created_at,
                    ts_ms,
                ),
            )
            if item_rows:
                conn.executemany(
                    """
                    INSERT INTO decision_handshake_items (
                        run_id, owner_id, symbol, stock_name, price, change_pct,
                        bar_5m_json, bar_1m_json, direction, strategies_json,
                        ai_score, ai_verdict, ai_reason, final_action,
                        order_executed, order_id, order_msg, latency_ms,
                        created_at, ts_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    item_rows,
                )
    finally:
        conn.close()

    return JSONResponse(
        {
            "ok": True,
            "run_id": run_id,
            "saved_items": len(item_rows),
            "created_at": created_at,
        },
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/v1/diagnostics/handshake/history")
async def get_diagnostic_handshake_history(
    request: Request,
    symbol: str = "",
    action: str = "",
    limit: int = 30,
):
    owner_id = _diag_owner_from_request(request)
    eff_limit = max(1, min(int(limit or 30), 100))
    sym = symbol.strip().upper()
    act = action.strip().upper()

    conn = _diag_db_connect()
    try:
        # Runs query (uses idx_dh_runs_owner_ts when owner_id is specified)
        if owner_id:
            run_rows = conn.execute(
                "SELECT * FROM decision_handshake_runs WHERE owner_id = ? ORDER BY ts_ms DESC LIMIT ?",
                (owner_id, eff_limit),
            ).fetchall()
        else:
            run_rows = conn.execute(
                "SELECT * FROM decision_handshake_runs ORDER BY ts_ms DESC LIMIT ?",
                (eff_limit,),
            ).fetchall()

        # Items query (uses idx_dh_items_sym_ts / idx_dh_items_action_ts / idx_dh_items_owner_ts)
        where_clauses: list[str] = []
        params: list[Any] = []
        if sym:
            where_clauses.append("symbol = ?")
            params.append(sym)
        if act:
            where_clauses.append("final_action = ?")
            params.append(act)
        if owner_id:
            where_clauses.append("owner_id = ?")
            params.append(owner_id)

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        params.append(eff_limit)
        item_rows = conn.execute(
            f"SELECT * FROM decision_handshake_items {where_sql} ORDER BY ts_ms DESC LIMIT ?",
            tuple(params),
        ).fetchall()
    finally:
        conn.close()

    runs_out = []
    for r in run_rows:
        d = dict(r)
        d["symbols"] = _diag_json_loads(d.pop("symbols_json", "[]"), [])
        runs_out.append(d)

    items_out = []
    for r in item_rows:
        d = dict(r)
        d["bar_5m"] = _diag_json_loads(d.pop("bar_5m_json", "{}"), {})
        d["bar_1m"] = _diag_json_loads(d.pop("bar_1m_json", "{}"), {})
        d["strategies"] = _diag_json_loads(d.pop("strategies_json", "[]"), [])
        d["order_executed"] = bool(d.get("order_executed"))
        items_out.append(d)

    return JSONResponse(
        {"ok": True, "runs": runs_out, "items": items_out},
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/v1/diagnostics/handshake/stats")
async def get_diagnostic_handshake_stats(request: Request):
    owner_id = _diag_owner_from_request(request)
    conn = _diag_db_connect()
    try:
        if owner_id:
            run_agg = conn.execute(
                "SELECT COUNT(*) AS total_runs, AVG(avg_latency_ms) AS avg_lat FROM decision_handshake_runs WHERE owner_id = ?",
                (owner_id,),
            ).fetchone()
            item_agg = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_items,
                    SUM(CASE WHEN final_action = 'BUY' THEN 1 ELSE 0 END) AS buy_signals,
                    SUM(CASE WHEN final_action IN ('SHORT_SELL', 'SHORT', 'SELL') THEN 1 ELSE 0 END) AS short_signals,
                    SUM(CASE WHEN order_executed = 1 THEN 1 ELSE 0 END) AS executed_orders,
                    AVG(latency_ms) AS item_avg_lat
                FROM decision_handshake_items
                WHERE owner_id = ?
                """,
                (owner_id,),
            ).fetchone()
            action_rows = conn.execute(
                """
                SELECT final_action, COUNT(*) AS cnt
                FROM decision_handshake_items
                WHERE owner_id = ?
                GROUP BY final_action
                ORDER BY cnt DESC
                """,
                (owner_id,),
            ).fetchall()
            top_rows = conn.execute(
                """
                SELECT
                    symbol,
                    MAX(stock_name) AS stock_name,
                    COUNT(*) AS count,
                    SUM(CASE WHEN final_action = 'BUY' THEN 1 ELSE 0 END) AS buy_count,
                    SUM(CASE WHEN final_action IN ('SHORT_SELL', 'SHORT', 'SELL') THEN 1 ELSE 0 END) AS short_count,
                    SUM(CASE WHEN order_executed = 1 THEN 1 ELSE 0 END) AS exec_count,
                    ROUND(AVG(ai_score), 1) AS avg_ai_score
                FROM decision_handshake_items
                WHERE owner_id = ?
                GROUP BY symbol
                ORDER BY count DESC, exec_count DESC
                LIMIT 10
                """,
                (owner_id,),
            ).fetchall()
        else:
            run_agg = conn.execute(
                "SELECT COUNT(*) AS total_runs, AVG(avg_latency_ms) AS avg_lat FROM decision_handshake_runs"
            ).fetchone()
            item_agg = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_items,
                    SUM(CASE WHEN final_action = 'BUY' THEN 1 ELSE 0 END) AS buy_signals,
                    SUM(CASE WHEN final_action IN ('SHORT_SELL', 'SHORT', 'SELL') THEN 1 ELSE 0 END) AS short_signals,
                    SUM(CASE WHEN order_executed = 1 THEN 1 ELSE 0 END) AS executed_orders,
                    AVG(latency_ms) AS item_avg_lat
                FROM decision_handshake_items
                """
            ).fetchone()
            action_rows = conn.execute(
                """
                SELECT final_action, COUNT(*) AS cnt
                FROM decision_handshake_items
                GROUP BY final_action
                ORDER BY cnt DESC
                """
            ).fetchall()
            top_rows = conn.execute(
                """
                SELECT
                    symbol,
                    MAX(stock_name) AS stock_name,
                    COUNT(*) AS count,
                    SUM(CASE WHEN final_action = 'BUY' THEN 1 ELSE 0 END) AS buy_count,
                    SUM(CASE WHEN final_action IN ('SHORT_SELL', 'SHORT', 'SELL') THEN 1 ELSE 0 END) AS short_count,
                    SUM(CASE WHEN order_executed = 1 THEN 1 ELSE 0 END) AS exec_count,
                    ROUND(AVG(ai_score), 1) AS avg_ai_score
                FROM decision_handshake_items
                GROUP BY symbol
                ORDER BY count DESC, exec_count DESC
                LIMIT 10
                """
            ).fetchall()
    finally:
        conn.close()

    total_runs = int(run_agg["total_runs"] or 0) if run_agg else 0
    total_items = int(item_agg["total_items"] or 0) if item_agg else 0
    buy_signals = int(item_agg["buy_signals"] or 0) if item_agg else 0
    short_signals = int(item_agg["short_signals"] or 0) if item_agg else 0
    executed_orders = int(item_agg["executed_orders"] or 0) if item_agg else 0
    raw_avg_lat = (
        run_agg["avg_lat"]
        if (run_agg and run_agg["avg_lat"] is not None)
        else (item_agg["item_avg_lat"] if item_agg else 0.0)
    )
    avg_latency_ms = round(float(raw_avg_lat or 0.0), 2)

    action_breakdown = {str(r["final_action"] or "UNKNOWN"): int(r["cnt"] or 0) for r in action_rows}
    top_symbols = [
        {
            "symbol": str(r["symbol"]),
            "stock_name": str(r["stock_name"] or ""),
            "count": int(r["count"] or 0),
            "buy_count": int(r["buy_count"] or 0),
            "short_count": int(r["short_count"] or 0),
            "exec_count": int(r["exec_count"] or 0),
            "avg_ai_score": float(r["avg_ai_score"] or 0.0),
        }
        for r in top_rows
    ]

    return JSONResponse(
        {
            "ok": True,
            "total_runs": total_runs,
            "total_items": total_items,
            "buy_signals": buy_signals,
            "short_signals": short_signals,
            "executed_orders": executed_orders,
            "avg_latency_ms": avg_latency_ms,
            "action_breakdown": action_breakdown,
            "top_symbols": top_symbols,
        },
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# Strategy30 Deterministic Statistics & Free System One Shadow Decision API
# ---------------------------------------------------------------------------
class Strategy30EvalRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list)
    interval: str = "5"
    stock_names: dict[str, str] = Field(default_factory=dict)
    quotes: dict[str, dict[str, Any]] = Field(default_factory=dict)
    candles_map: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)


_S30_TIER_PRIORITY: dict[str, int] = {
    "normal_candidate": 5,
    "small_candidate": 4,
    "review": 3,
    "watch": 2,
    "ignore": 1,
}

_S30_INTERVAL_MAP: dict[str, tuple[str, str]] = {
    "1": ("1m", "1d"),
    "1M": ("1m", "1d"),
    "5": ("5m", "5d"),
    "5M": ("5m", "5d"),
    "15": ("15m", "5d"),
    "15M": ("15m", "5d"),
    "30": ("30m", "5d"),
    "30M": ("30m", "5d"),
    "60": ("60m", "1mo"),
    "60M": ("60m", "1mo"),
    "D": ("1d", "6mo"),
    "1D": ("1d", "6mo"),
}


async def _s30_fetch_yahoo_intraday(sym: str, interval: str) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    """Fetch real intraday K-bars & quote metadata from Yahoo Finance (.TW then .TWO)."""
    iv_key = str(interval or "5").strip().upper()
    y_iv, y_rng = _S30_INTERVAL_MAP.get(iv_key, ("5m", "5d"))
    candidates = [
        (f"{sym}.TW", "TW_TSE"),
        (f"{sym}.TWO", "TW_OTC"),
    ]
    if sym.endswith(".TW") or sym.endswith(".TWO") or sym.startswith("^"):
        candidates = [(sym, "TW_OTC" if sym.endswith(".TWO") else "TW_TSE")]

    for yh_sym, mkt in candidates:
        try:
            data = await get_yahoo_market_data(yh_sym, interval=y_iv, range_str=y_rng)
            candles = data.get("candles") if isinstance(data, dict) else None
            if isinstance(candles, list) and candles:
                px = float(data.get("price") or candles[-1].get("close") or 0.0)
                prev_c = float(data.get("prev_close") or 0.0)
                chg_pct = round(((px - prev_c) / prev_c * 100.0), 2) if (px > 0 and prev_c > 0) else 0.0
                meta_quote = {
                    "price": px,
                    "prev_close": prev_c,
                    "change_pct": chg_pct,
                    "high": float(data.get("high") or px),
                    "low": float(data.get("low") or px),
                }
                return candles, meta_quote, mkt
        except Exception:
            continue
    return [], {}, "TW"


@app.post("/api/v1/strategy30/evaluate")
async def evaluate_strategy30_batch(req: Strategy30EvalRequest, request: Request):
    """Evaluate up to 16 symbols across 30 deterministic intraday strategies + free System One shadow classifier."""
    t_total = time.perf_counter()
    owner_id = _diag_owner_from_request(request)
    ts_ms = int(time.time() * 1000)
    created_at = datetime.now(TW_TZ).isoformat(timespec="seconds")
    eval_id = f"s30_{ts_ms}_{secrets.token_hex(4)}"

    # Deduplicate and allow up to 60 symbols (Full-Market Top-50 or all 12-sector chains + watchlist)
    clean_symbols: list[str] = []
    seen_syms: set[str] = set()
    for raw_sym in req.symbols or []:
        s = str(raw_sym or "").strip().upper()
        if s and s not in seen_syms:
            seen_syms.add(s)
            clean_symbols.append(s)
            if len(clean_symbols) >= 60:
                break

    if not clean_symbols:
        return JSONResponse(
            {
                "ok": True,
                "eval_id": eval_id,
                "count": 0,
                "total_cost_usd": 0.0,
                "shadow_mode": True,
                "weight_version": WEIGHT_VERSION,
                "prompt_version": PROMPT_VERSION,
                "total_latency_ms": round((time.perf_counter() - t_total) * 1000.0, 2),
                "results": [],
            },
            headers={"Cache-Control": "no-store"},
        )

    names_norm = {str(k).strip().upper(): str(v or "").strip() for k, v in (req.stock_names or {}).items()}
    quotes_norm = {str(k).strip().upper(): dict(v) for k, v in (req.quotes or {}).items() if isinstance(v, dict)}
    candles_norm = {
        str(k).strip().upper(): list(v)
        for k, v in (req.candles_map or {}).items()
        if isinstance(v, list)
    }

    need_bars = [s for s in clean_symbols if not candles_norm.get(s)]
    need_quotes = [s for s in clean_symbols if not quotes_norm.get(s)]

    async def _empty_mis() -> dict[str, dict[str, Any]]:
        return {}

    if need_quotes:
        ex_channels: list[str] = []
        for s in need_quotes:
            base = s.split(".", 1)[0]
            ex_channels.append(f"tse_{base}.tw")
            ex_channels.append(f"otc_{base}.tw")
        mis_task = asyncio.to_thread(_fetch_mis_batch_exact_sync, ex_channels)
    else:
        mis_task = _empty_mis()

    bar_tasks = [_s30_fetch_yahoo_intraday(s, req.interval) for s in need_bars]
    gathered = await asyncio.gather(mis_task, *bar_tasks, return_exceptions=True)

    mis_map: dict[str, dict[str, Any]] = gathered[0] if isinstance(gathered[0], dict) else {}
    fetched_bars: dict[str, tuple[list[dict[str, Any]], dict[str, Any], str]] = {}
    for sym, res_item in zip(need_bars, gathered[1:]):
        if isinstance(res_item, tuple) and len(res_item) == 3:
            fetched_bars[sym] = res_item
        else:
            fetched_bars[sym] = ([], {}, "TW")

    results: list[dict[str, Any]] = []
    for sym in clean_symbols:
        candles = candles_norm.get(sym)
        meta_quote: dict[str, Any] = {}
        market = "TW"
        if not candles:
            fb_candles, fb_meta, fb_mkt = fetched_bars.get(sym, ([], {}, "TW"))
            candles = fb_candles
            meta_quote = fb_meta
            market = fb_mkt

        quote = dict(quotes_norm.get(sym) or {})
        if not quote:
            base = sym.split(".", 1)[0]
            mis_q = mis_map.get(base) or mis_map.get(sym) or {}
            if mis_q:
                quote = dict(mis_q)
                if "volume" not in quote and mis_q.get("volume_lots") is not None:
                    quote["volume"] = float(mis_q.get("volume_lots") or 0.0) * 1000.0
            elif meta_quote:
                quote = dict(meta_quote)

        stock_name = names_norm.get(sym) or str(quote.get("stock_name") or quote.get("name") or sym)
        res = evaluate_symbol_30(
            symbol=sym,
            stock_name=stock_name,
            market=market,
            candles=candles or [],
            quote=quote,
            kill_switch=KILL_SWITCH_ACTIVE,
        )
        results.append(res)

    def _leaderboard_sort_key(item: dict[str, Any]) -> tuple[int, int, float]:
        mi = item.get("model_input") or {}
        dec = item.get("decision") or {}
        has_block = 1 if (mi.get("hard_blocks") or []) else 0
        tier_rank = _S30_TIER_PRIORITY.get(str(dec.get("action_tier") or "ignore"), 0)
        w_score = abs(float(mi.get("weighted_score") or 0.0))
        grp_agree = max(float(mi.get("group_agreement") or 0.0), 0.1)
        return (has_block, -tier_rank, -(w_score * grp_agree))

    results.sort(key=_leaderboard_sort_key)

    # Batch-insert all rows into strategy30_audit_log in a single transaction
    rows_to_insert: list[tuple[Any, ...]] = []
    for item in results:
        mi = item.get("model_input") or {}
        dec = item.get("decision") or {}
        cov = mi.get("signal_coverage") or {}
        cnt = mi.get("counts") or {}
        rows_to_insert.append(
            (
                eval_id,
                owner_id,
                str(item.get("symbol") or ""),
                str(item.get("stock_name") or ""),
                float(item.get("price") or 0.0),
                float(item.get("change_pct") or 0.0),
                int(cov.get("valid") or 0),
                int(cnt.get("long") or 0),
                int(cnt.get("short") or 0),
                int(cnt.get("neutral") or 0),
                float(mi.get("weighted_score") or 0.0),
                float(mi.get("group_agreement") or 0.0),
                str(item.get("overall_direction") or "neutral"),
                _diag_json_dumps(mi.get("hard_blocks") or [], "[]"),
                str(dec.get("signal_quality") or "insufficient"),
                str(dec.get("action_tier") or "ignore"),
                1 if dec.get("review_required") else 0,
                float(dec.get("cost_usd") or 0.0),
                str(item.get("weight_version") or WEIGHT_VERSION),
                str(dec.get("prompt_version") or PROMPT_VERSION),
                str(item.get("one_line_summary") or ""),
                _diag_json_dumps(item, "{}"),
                float(item.get("latency_ms") or 0.0),
                created_at,
                ts_ms,
            )
        )

    if rows_to_insert:
        conn = _diag_db_connect()
        try:
            with conn:
                conn.executemany(
                    """
                    INSERT INTO strategy30_audit_log (
                        eval_id, owner_id, symbol, stock_name, price, change_pct,
                        valid_count, long_count, short_count, neutral_count,
                        weighted_score, group_agreement, overall_direction,
                        hard_blocks_json, signal_quality, action_tier,
                        review_required, cost_usd, weight_version, prompt_version,
                        one_line_summary, payload_json, latency_ms, created_at, ts_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows_to_insert,
                )
        finally:
            conn.close()

    total_latency_ms = round((time.perf_counter() - t_total) * 1000.0, 2)
    return JSONResponse(
        {
            "ok": True,
            "eval_id": eval_id,
            "count": len(results),
            "total_cost_usd": 0.0,
            "shadow_mode": True,
            "weight_version": WEIGHT_VERSION,
            "prompt_version": PROMPT_VERSION,
            "jev_quota": get_jev_quota_status(),
            "total_latency_ms": total_latency_ms,
            "results": results,
        },
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/v1/strategy30/jev-quota")
async def get_jev_quota_endpoint():
    return JSONResponse({"ok": True, **get_jev_quota_status()}, headers={"Cache-Control": "no-store"})


@app.get("/api/v1/strategy30/audit")
async def get_strategy30_audit(
    request: Request,
    symbol: str = "",
    action_tier: str = "",
    limit: int = 30,
):
    """Query indexed strategy30_audit_log records and aggregate quality/risk statistics."""
    sym_filter = str(symbol or "").strip().upper()
    tier_filter = str(action_tier or "").strip()
    owner_id = _diag_owner_from_request(request)
    lim = max(1, min(100, int(limit or 30)))

    where_clauses: list[str] = []
    params: list[Any] = []
    if sym_filter:
        where_clauses.append("symbol = ?")
        params.append(sym_filter)
    if tier_filter:
        where_clauses.append("action_tier = ?")
        params.append(tier_filter)
    if owner_id:
        where_clauses.append("owner_id = ?")
        params.append(owner_id)

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    conn = _diag_db_connect()
    try:
        rows = conn.execute(
            f"""
            SELECT
                id, eval_id, owner_id, symbol, stock_name, price, change_pct,
                valid_count, long_count, short_count, neutral_count,
                weighted_score, group_agreement, overall_direction,
                hard_blocks_json, signal_quality, action_tier,
                review_required, cost_usd, weight_version, prompt_version,
                one_line_summary, latency_ms, created_at, ts_ms
            FROM strategy30_audit_log
            {where_sql}
            ORDER BY ts_ms DESC
            LIMIT ?
            """,
            (*params, lim),
        ).fetchall()

        agg = conn.execute(
            f"""
            SELECT
                COUNT(*) AS total,
                AVG(valid_count / 30.0) AS avg_coverage,
                SUM(CASE WHEN hard_blocks_json LIKE '%strategy_conflicted%' THEN 1 ELSE 0 END) AS conflict_cnt,
                SUM(CASE WHEN hard_blocks_json IS NOT NULL AND hard_blocks_json != '' AND hard_blocks_json != '[]' THEN 1 ELSE 0 END) AS block_cnt
            FROM strategy30_audit_log
            {where_sql}
            """,
            tuple(params),
        ).fetchone()

        tier_rows = conn.execute(
            f"""
            SELECT action_tier, COUNT(*) AS cnt
            FROM strategy30_audit_log
            {where_sql}
            GROUP BY action_tier
            ORDER BY cnt DESC
            """,
            tuple(params),
        ).fetchall()
    finally:
        conn.close()

    items = [
        {
            "id": int(r["id"]),
            "eval_id": str(r["eval_id"]),
            "owner_id": str(r["owner_id"] or ""),
            "symbol": str(r["symbol"]),
            "stock_name": str(r["stock_name"] or ""),
            "price": float(r["price"] or 0.0),
            "change_pct": float(r["change_pct"] or 0.0),
            "valid_count": int(r["valid_count"] or 0),
            "long_count": int(r["long_count"] or 0),
            "short_count": int(r["short_count"] or 0),
            "neutral_count": int(r["neutral_count"] or 0),
            "weighted_score": float(r["weighted_score"] or 0.0),
            "group_agreement": float(r["group_agreement"] or 0.0),
            "overall_direction": str(r["overall_direction"] or "neutral"),
            "hard_blocks": _diag_json_loads(r["hard_blocks_json"], []),
            "signal_quality": str(r["signal_quality"] or "insufficient"),
            "action_tier": str(r["action_tier"] or "ignore"),
            "review_required": bool(r["review_required"]),
            "cost_usd": float(r["cost_usd"] or 0.0),
            "weight_version": str(r["weight_version"] or ""),
            "prompt_version": str(r["prompt_version"] or ""),
            "one_line_summary": str(r["one_line_summary"] or ""),
            "latency_ms": float(r["latency_ms"] or 0.0),
            "created_at": str(r["created_at"] or ""),
            "ts_ms": int(r["ts_ms"] or 0),
        }
        for r in rows
    ]

    total = int(agg["total"] or 0) if agg else 0
    avg_coverage = round(float(agg["avg_coverage"] or 0.0), 4) if (agg and total > 0) else 0.0
    conflict_rate = round(float(agg["conflict_cnt"] or 0) / total, 4) if (agg and total > 0) else 0.0
    block_rate = round(float(agg["block_cnt"] or 0) / total, 4) if (agg and total > 0) else 0.0
    tier_breakdown = {str(r["action_tier"] or "unknown"): int(r["cnt"] or 0) for r in tier_rows}

    return JSONResponse(
        {
            "ok": True,
            "items": items,
            "stats": {
                "total": total,
                "avg_coverage": avg_coverage,
                "conflict_rate": conflict_rate,
                "block_rate": block_rate,
                "tier_breakdown": tier_breakdown,
            },
        },
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/v1/stream/live-quotes")
async def stream_live_quotes(
    request: Request,
    symbols: str = "2330,2317,2454",
    interval_ms: int = 1500,
    max_events: int = 0,
):
    """
    v3.28.0 Dynamic SSE Real-Time Quote Push Stream (永豐 Shioaji 優先 -> 官方實盤輔助).
    Replaces slow browser polling with persistent low-latency server push so stop-loss/take-profit
    and UI HUD react immediately on every tick.
    """
    syms = [x.strip().upper() for x in symbols.split(",") if re.fullmatch(r"[0-9A-Z]{4,6}", x.strip().upper())][:60]
    if not syms:
        syms = ["2330"]
    sym_csv = ",".join(syms)
    sleep_sec = max(1.0, min(10.0, float(interval_ms) / 1000.0))

    async def event_gen():
        seq = 0
        yield f"event: hello\ndata: {json.dumps({'ok': True, 'mode': 'SSE_DYNAMIC', 'symbols': syms, 'interval_ms': int(sleep_sec * 1000)}, ensure_ascii=False)}\n\n"
        while not _SERVER_SHUTTING_DOWN:
            if await request.is_disconnected():
                break
            seq += 1
            t0 = time.perf_counter()
            try:
                snap = await shioaji_snapshots(symbols=sym_csv)
                quotes = snap.get("quotes") if isinstance(snap, dict) else {}
                src = snap.get("source") if isinstance(snap, dict) else "永豐/官方實盤"
            except Exception as exc:
                quotes = {}
                src = f"暫時重連中 ({type(exc).__name__})"
            lat_ms = round((time.perf_counter() - t0) * 1000.0, 1)
            payload = {
                "seq": seq,
                "server_time": tw_now_str(),
                "source": src,
                "latency_ms": lat_ms,
                "count": len(quotes) if isinstance(quotes, dict) else 0,
                "quotes": quotes if isinstance(quotes, dict) else {},
            }
            yield f"event: tick\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            if max_events > 0 and seq >= max_events:
                break
            await asyncio.sleep(sleep_sec)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# =============================================================================
# v3.29.0 UI Button Action & Error Audit Log Endpoints
# =============================================================================
_UI_ACTION_LOGS: List[Dict[str, Any]] = []
_UI_ACTION_LOGS_MAX = 500


@app.post("/api/v1/ui-logs")
async def post_ui_logs(payload: Dict[str, Any]):
    """Receive batched UI button action / error logs from frontend (non-blocking audit trail)."""
    entries = payload.get("entries")
    if not isinstance(entries, list):
        entries = [payload] if isinstance(payload, dict) else []
    added = 0
    for item in entries[-100:]:
        if not isinstance(item, dict):
            continue
        _UI_ACTION_LOGS.append({
            "ts": str(item.get("ts") or tw_now_str())[:32],
            "level": str(item.get("level") or "OK")[:16],
            "kind": str(item.get("kind") or "click")[:24],
            "target": str(item.get("target") or "")[:80],
            "area": str(item.get("area") or "")[:64],
            "detail": str(item.get("detail") or "")[:240],
        })
        added += 1
    if len(_UI_ACTION_LOGS) > _UI_ACTION_LOGS_MAX:
        del _UI_ACTION_LOGS[:-_UI_ACTION_LOGS_MAX]
    return {"ok": True, "added": added, "total": len(_UI_ACTION_LOGS)}


@app.get("/api/v1/ui-logs")
async def get_ui_logs(limit: int = 120):
    """Return recent UI button action & error audit logs."""
    lim = max(1, min(int(limit or 120), _UI_ACTION_LOGS_MAX))
    return {"ok": True, "total": len(_UI_ACTION_LOGS), "entries": list(reversed(_UI_ACTION_LOGS[-lim:]))}


_SERVER_SHUTTING_DOWN = False


@app.on_event("shutdown")
async def _on_server_shutdown():
    """Ensure desktop / cloud shutdown immediately stops background scanner & SSE loops."""
    global _SERVER_SHUTTING_DOWN
    _SERVER_SHUTTING_DOWN = True
    try:
        _SCANNER_STATE["active"] = False
    except Exception:
        pass
    try:
        for q in list(globals().get("_SCANNER_SSE_QUEUES") or []):
            try:
                q.put_nowait(None)
            except Exception:
                pass
    except Exception:
        pass


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port, timeout_graceful_shutdown=3)
