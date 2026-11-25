"""
SHL TW — 30 組盤中策略確定性統計與免費 System One 影子決策引擎 (strategy30_engine.py)
參考架構：
- https://github.com/jarrodwatts/jev-trader (低延遲熱路徑、離散選項機率、乾跑/影子稽核)
- https://jevai.dev/zh-Hant/playground/ (System One 型別化有限選項分類、AnyJev 順序去偏精神)

安全與零收費保證：
1. 所有比例、加權分數、群組一致性與硬風控皆由確定性純程式計算，絕不由 LLM 計算。
2. 預設 100% 採用本機免費 System One 影子分類器（$0.00 費用、毫秒級延遲），絕不自動呼叫任何付費 API。
3. 任何模型或分類器絕不得繞過 hard_blocks 硬風控或決定槓桿。
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any

TW_TZ = timezone(timedelta(hours=8))

WEIGHT_VERSION = "w_v1.0.0"
PROMPT_VERSION = "s1_v1.0.0"

# Jev / System One 免費額度守衛（嚴格防超額收費：達每日免費上限或 429/402 即停用，待 UTC 00:00 / 台北 08:00 自動重置）
_JEV_QUOTA_STATE: dict[str, Any] = {
    "utc_day": "",
    "used": 0,
    "daily_limit": 6,
    "exhausted": False,
    "reason": "",
}


def _sync_jev_quota_day() -> None:
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _JEV_QUOTA_STATE["utc_day"] != today_utc:
        _JEV_QUOTA_STATE["utc_day"] = today_utc
        _JEV_QUOTA_STATE["used"] = 0
        _JEV_QUOTA_STATE["exhausted"] = False
        _JEV_QUOTA_STATE["reason"] = ""


def get_jev_quota_status() -> dict[str, Any]:
    _sync_jev_quota_day()
    now_utc = datetime.now(timezone.utc)
    next_reset_utc = (now_utc + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    next_reset_tw = next_reset_utc.astimezone(TW_TZ)
    exhausted = bool(_JEV_QUOTA_STATE["exhausted"] or _JEV_QUOTA_STATE["used"] >= _JEV_QUOTA_STATE["daily_limit"])
    return {
        "external_jev_allowed": not exhausted,
        "exhausted": exhausted,
        "used": int(_JEV_QUOTA_STATE["used"]),
        "daily_limit": int(_JEV_QUOTA_STATE["daily_limit"]),
        "remaining": max(0, int(_JEV_QUOTA_STATE["daily_limit"]) - int(_JEV_QUOTA_STATE["used"])),
        "reset_at_utc": next_reset_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reset_at_tw": next_reset_tw.strftime("%Y-%m-%d %H:%M TST"),
        "reason": _JEV_QUOTA_STATE["reason"] or ("已達每日免費上限，自動改用本機 $0 免費引擎，待額度更新後再啟用" if exhausted else "免費本機引擎優先（$0）"),
    }


def can_use_external_jev() -> bool:
    return get_jev_quota_status()["external_jev_allowed"]


def record_jev_quota_exhausted(reason: str = "Jev 免費額度已用盡") -> dict[str, Any]:
    _sync_jev_quota_day()
    _JEV_QUOTA_STATE["exhausted"] = True
    _JEV_QUOTA_STATE["used"] = max(_JEV_QUOTA_STATE["used"], _JEV_QUOTA_STATE["daily_limit"])
    _JEV_QUOTA_STATE["reason"] = str(reason or "Jev 免費額度已用盡，待 UTC 00:00 更新後自動恢復")
    return get_jev_quota_status()

# 30 組版本化盤中策略目錄（5 獨立群組 × 6 策略 = 30 組；權重固定於盤外版本化，盤中唯讀）
STRATEGY_CATALOG: list[dict[str, Any]] = [
    # 1. trend (趨勢 6 組)
    {"id": "trend_ema5_12", "group": "trend", "weight": 1.15, "name": "EMA5/12 趨勢交叉"},
    {"id": "trend_ema8_21", "group": "trend", "weight": 1.10, "name": "EMA8/21 中短趨勢"},
    {"id": "trend_ma20_slope", "group": "trend", "weight": 1.00, "name": "MA20 斜率方向"},
    {"id": "trend_vwap_hold", "group": "trend", "weight": 1.25, "name": "VWAP 均價站穩"},
    {"id": "trend_adx_di", "group": "trend", "weight": 1.05, "name": "DI+/- 趨勢強度"},
    {"id": "trend_supertrend", "group": "trend", "weight": 1.15, "name": "ATR SuperTrend"},
    # 2. breakout (突破 6 組)
    {"id": "breakout_orb_5m", "group": "breakout", "weight": 1.20, "name": "開盤區間突破 ORB"},
    {"id": "breakout_donchian_10", "group": "breakout", "weight": 1.10, "name": "Donchian 10 突破"},
    {"id": "breakout_bb_sqz", "group": "breakout", "weight": 1.05, "name": "布林壓縮突破"},
    {"id": "breakout_smc_bos", "group": "breakout", "weight": 1.25, "name": "SMC 結構 BOS"},
    {"id": "breakout_prev_hl", "group": "breakout", "weight": 1.00, "name": "近5根高低突破"},
    {"id": "breakout_keltner", "group": "breakout", "weight": 1.05, "name": "Keltner 通道突破"},
    # 3. mean_reversion (均值回歸 6 組)
    {"id": "mr_rsi14_ext", "group": "mean_reversion", "weight": 0.95, "name": "RSI14 極值回歸"},
    {"id": "mr_bb_band", "group": "mean_reversion", "weight": 0.95, "name": "布林上下軌回歸"},
    {"id": "mr_vwap_zscore", "group": "mean_reversion", "weight": 1.05, "name": "VWAP Z-Score 回歸"},
    {"id": "mr_kd_cross", "group": "mean_reversion", "weight": 0.90, "name": "KD(9,3,3) 轉折"},
    {"id": "mr_cci_revert", "group": "mean_reversion", "weight": 0.90, "name": "CCI14 極端回歸"},
    {"id": "mr_williams_r", "group": "mean_reversion", "weight": 0.90, "name": "Williams %R 回歸"},
    # 4. volume_price (量價 6 組)
    {"id": "vp_vol_surge_dir", "group": "volume_price", "weight": 1.25, "name": "爆量方向確認"},
    {"id": "vp_obv_trend", "group": "volume_price", "weight": 1.05, "name": "OBV 能量潮斜率"},
    {"id": "vp_clv_delta", "group": "volume_price", "weight": 1.15, "name": "CLV 區間 Delta"},
    {"id": "vp_mfi14", "group": "volume_price", "weight": 1.00, "name": "MFI14 資金流向"},
    {"id": "vp_vwap_vol_confirm", "group": "volume_price", "weight": 1.15, "name": "VWAP 帶量推升"},
    {"id": "vp_bar_body_vol", "group": "volume_price", "weight": 1.00, "name": "實體K棒量價比"},
    # 5. event (事件／微結構 6 組)
    {"id": "evt_fvg_retest", "group": "event", "weight": 1.15, "name": "FVG 缺口回測"},
    {"id": "evt_orderbook_imb", "group": "event", "weight": 1.20, "name": "五檔委買賣失衡"},
    {"id": "evt_gap_momentum", "group": "event", "weight": 1.00, "name": "開盤跳空延續"},
    {"id": "evt_intraday_hl_test", "group": "event", "weight": 1.05, "name": "當日高低點測試"},
    {"id": "evt_spread_tight", "group": "event", "weight": 0.85, "name": "內外盤價差動能"},
    {"id": "evt_vol_spike_break", "group": "event", "weight": 1.10, "name": "單棒急量突圍"},
]

STRATEGY_WEIGHT_MAP = {s["id"]: float(s["weight"]) for s in STRATEGY_CATALOG}


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(v)))


def _ema_series(vals: list[float], period: int) -> list[float]:
    if not vals:
        return []
    k = 2.0 / (period + 1.0)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1.0 - k))
    return out


def _rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < 3:
        return 50.0
    n = min(period, len(closes) - 1)
    gains = 0.0
    losses = 0.0
    for i in range(len(closes) - n, len(closes)):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            gains += diff
        else:
            losses += -diff
    if losses <= 1e-9:
        return 100.0 if gains > 0 else 50.0
    rs = (gains / n) / (losses / n)
    return 100.0 - (100.0 / (1.0 + rs))


def _atr(candles: list[dict[str, Any]], period: int = 14) -> float:
    if len(candles) < 2:
        px = float(candles[-1].get("close") or 0) if candles else 0.0
        return max(px * 0.008, 0.01)
    trs = []
    start = max(1, len(candles) - period)
    for i in range(start, len(candles)):
        h = float(candles[i].get("high") or candles[i].get("close") or 0)
        l = float(candles[i].get("low") or candles[i].get("close") or 0)
        pc = float(candles[i - 1].get("close") or l)
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    px = float(candles[-1].get("close") or 0)
    return max(sum(trs) / max(len(trs), 1), px * 0.004, 0.01)


def _make_signal(
    *,
    symbol: str,
    market: str,
    as_of: str,
    invalid_after: str,
    strategy_id: str,
    strategy_group: str,
    direction: str,
    strength: float,
    confidence: float,
    entry_price: float,
    stop_price: float,
    reason_code: list[str],
    is_valid: bool = True,
) -> dict[str, Any]:
    if direction not in ("long", "short", "neutral", "no_signal"):
        direction = "no_signal"
        is_valid = False
    return {
        "symbol": symbol,
        "market": market,
        "as_of": as_of,
        "strategy_id": strategy_id,
        "strategy_group": strategy_group,
        "direction": direction,
        "strength": round(_clamp(strength), 4),
        "confidence": round(_clamp(confidence), 4),
        "horizon": "intraday",
        "entry_price": round(float(entry_price), 2),
        "stop_price": round(float(stop_price), 2),
        "invalid_after": invalid_after,
        "reason_code": reason_code,
        "is_valid": bool(is_valid),
    }


def _evaluate_30_signals(
    symbol: str,
    market: str,
    candles: list[dict[str, Any]],
    quote: dict[str, Any],
    as_of_dt: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Compute all 30 standardized strategy signals deterministically in < 2ms."""
    as_of = as_of_dt.isoformat(timespec="seconds")
    invalid_after = (as_of_dt + timedelta(minutes=25)).isoformat(timespec="seconds")

    n = len(candles)
    closes = [float(c.get("close") or 0) for c in candles if float(c.get("close") or 0) > 0]
    highs = [float(c.get("high") or c.get("close") or 0) for c in candles]
    lows = [float(c.get("low") or c.get("close") or 0) for c in candles]
    opens = [float(c.get("open") or c.get("close") or 0) for c in candles]
    vols = [max(0.0, float(c.get("volume") or 0)) for c in candles]

    px = float(quote.get("price") or (closes[-1] if closes else 0.0))
    prev_close = float(quote.get("prev_close") or (closes[0] if closes else px) or px)
    chg_pct = float(quote.get("change_pct") or (((px - prev_close) / prev_close * 100.0) if prev_close > 0 else 0.0))

    if n < 6 or px <= 0 or len(closes) < 6:
        # Data insufficient -> emit 30 invalid no_signal records so coverage gate catches it
        sigs = [
            _make_signal(
                symbol=symbol, market=market, as_of=as_of, invalid_after=invalid_after,
                strategy_id=s["id"], strategy_group=s["group"], direction="no_signal",
                strength=0.0, confidence=0.0, entry_price=px, stop_price=px,
                reason_code=["insufficient_bars"], is_valid=False,
            )
            for s in STRATEGY_CATALOG
        ]
        return sigs, {"px": px, "chg_pct": chg_pct, "atr": 0.0, "vwap": px, "vol_ratio": 0.0, "bars": n}

    atr_val = _atr(candles, 14)
    ema5 = _ema_series(closes, 5)
    ema8 = _ema_series(closes, 8)
    ema12 = _ema_series(closes, 12)
    ema21 = _ema_series(closes, 21)
    ma20 = sum(closes[-min(20, len(closes)):]) / min(20, len(closes))
    ma20_prev = sum(closes[-min(21, len(closes)):-1]) / max(1, min(20, len(closes) - 1))

    # VWAP
    pv_sum = 0.0
    v_sum = 0.0
    for c in candles[-min(40, n):]:
        tp = (float(c.get("high") or c.get("close") or px) + float(c.get("low") or c.get("close") or px) + float(c.get("close") or px)) / 3.0
        v = max(1.0, float(c.get("volume") or 0))
        pv_sum += tp * v
        v_sum += v
    vwap = pv_sum / v_sum if v_sum > 0 else px
    vwap_diff = (px - vwap) / max(vwap, 1e-6)

    # Bollinger Bands (20, 2)
    win20 = closes[-min(20, len(closes)):]
    mean20 = sum(win20) / len(win20)
    std20 = math.sqrt(sum((x - mean20) ** 2 for x in win20) / max(1, len(win20)))
    bb_up = mean20 + 2.0 * std20
    bb_lo = mean20 - 2.0 * std20
    bb_width = (bb_up - bb_lo) / max(mean20, 1e-6)

    rsi14 = _rsi(closes, 14)
    hi5 = max(highs[-6:-1]) if n >= 6 else max(highs)
    lo5 = min(lows[-6:-1]) if n >= 6 else min(lows)
    hi10 = max(highs[-min(11, n):-1]) if n >= 3 else max(highs)
    lo10 = min(lows[-min(11, n):-1]) if n >= 3 else min(lows)
    day_hi = max(highs)
    day_lo = min(lows)

    vol_avg5 = sum(vols[-6:-1]) / 5.0 if n >= 6 else (sum(vols) / max(1, len(vols)))
    cur_vol = vols[-1]
    vol_ratio = (cur_vol / vol_avg5) if vol_avg5 > 0 else 1.0

    # OBV slope
    obv = [0.0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            obv.append(obv[-1] + vols[i])
        elif closes[i] < closes[i - 1]:
            obv.append(obv[-1] - vols[i])
        else:
            obv.append(obv[-1])
    obv_delta = obv[-1] - obv[-min(6, len(obv))]

    # CLV Delta
    clv_sum = 0.0
    for i in range(max(0, n - 10), n):
        rng = max(highs[i] - lows[i], 1e-6)
        clv = ((closes[i] - lows[i]) - (highs[i] - closes[i])) / rng
        clv_sum += clv * max(1.0, vols[i])

    # Stochastic K/D (9)
    h9 = max(highs[-min(9, n):])
    l9 = min(lows[-min(9, n):])
    rsv = ((px - l9) / max(h9 - l9, 1e-6)) * 100.0
    prev_h9 = max(highs[-min(10, n):-1]) if n >= 2 else h9
    prev_l9 = min(lows[-min(10, n):-1]) if n >= 2 else l9
    prev_rsv = ((closes[-2] - prev_l9) / max(prev_h9 - prev_l9, 1e-6)) * 100.0 if len(closes) >= 2 else 50.0
    will_r = -100.0 * (h9 - px) / max(h9 - l9, 1e-6)

    # CCI14
    tps = [(highs[i] + lows[i] + closes[i]) / 3.0 for i in range(max(0, n - 14), n)]
    tp_ma = sum(tps) / len(tps)
    md = sum(abs(t - tp_ma) for t in tps) / max(1, len(tps))
    cci = (tps[-1] - tp_ma) / max(0.015 * md, 1e-6)

    # MFI14 approximation
    pos_mf = 0.0
    neg_mf = 0.0
    for i in range(max(1, n - 14), n):
        tp_c = (highs[i] + lows[i] + closes[i]) / 3.0
        tp_p = (highs[i - 1] + lows[i - 1] + closes[i - 1]) / 3.0
        mf = tp_c * max(1.0, vols[i])
        if tp_c >= tp_p:
            pos_mf += mf
        else:
            neg_mf += mf
    mfi = 100.0 - (100.0 / (1.0 + pos_mf / max(neg_mf, 1e-6)))

    # Orderbook imbalance from quote if available
    bid_v = float(quote.get("bid_vol") or quote.get("bid_size") or 0)
    ask_v = float(quote.get("ask_vol") or quote.get("ask_size") or 0)
    ob_imb = ((bid_v - ask_v) / (bid_v + ask_v)) if (bid_v + ask_v) > 0 else (clv_sum / max(sum(vols[-10:]) or 1.0, 1.0))

    def _emit(sid: str, grp: str, d: str, st: float, cf: float, rc: list[str], valid: bool = True) -> dict[str, Any]:
        stop_px = (px - atr_val * 1.2) if d == "long" else ((px + atr_val * 1.2) if d == "short" else px)
        return _make_signal(
            symbol=symbol, market=market, as_of=as_of, invalid_after=invalid_after,
            strategy_id=sid, strategy_group=grp, direction=d,
            strength=st, confidence=cf, entry_price=px, stop_price=stop_px,
            reason_code=rc, is_valid=valid,
        )

    sigs: list[dict[str, Any]] = []

    # ---- Group 1: trend (6) ----
    e5_12 = (ema5[-1] - ema12[-1]) / max(px, 1e-6)
    sigs.append(_emit("trend_ema5_12", "trend",
                      "long" if e5_12 > 0.0005 else ("short" if e5_12 < -0.0005 else "neutral"),
                      min(1.0, abs(e5_12) * 180 + 0.35), 0.78,
                      ["ema5_above_ema12"] if e5_12 > 0 else ["ema5_below_ema12"]))

    e8_21 = (ema8[-1] - ema21[-1]) / max(px, 1e-6)
    sigs.append(_emit("trend_ema8_21", "trend",
                      "long" if e8_21 > 0.0006 else ("short" if e8_21 < -0.0006 else "neutral"),
                      min(1.0, abs(e8_21) * 150 + 0.32), 0.76,
                      ["ema8_above_ema21"] if e8_21 > 0 else ["ema8_below_ema21"]))

    ma_slope = (ma20 - ma20_prev) / max(px, 1e-6)
    sigs.append(_emit("trend_ma20_slope", "trend",
                      "long" if (px > ma20 and ma_slope > 0) else ("short" if (px < ma20 and ma_slope < 0) else "neutral"),
                      min(1.0, abs(ma_slope) * 400 + 0.35), 0.72,
                      ["ma20_rising"] if ma_slope > 0 else ["ma20_falling"]))

    sigs.append(_emit("trend_vwap_hold", "trend",
                      "long" if vwap_diff > 0.001 else ("short" if vwap_diff < -0.001 else "neutral"),
                      min(1.0, abs(vwap_diff) * 120 + 0.4), 0.82,
                      ["above_vwap"] if vwap_diff > 0 else ["below_vwap"]))

    # DI+/- proxy
    up_mv = sum(max(0.0, highs[i] - highs[i - 1]) for i in range(max(1, n - 10), n))
    dn_mv = sum(max(0.0, lows[i - 1] - lows[i]) for i in range(max(1, n - 10), n))
    di_diff = (up_mv - dn_mv) / max(up_mv + dn_mv, 1e-6)
    sigs.append(_emit("trend_adx_di", "trend",
                      "long" if di_diff > 0.15 else ("short" if di_diff < -0.15 else "neutral"),
                      min(1.0, abs(di_diff)), 0.74,
                      ["di_plus_dom"] if di_diff > 0 else ["di_minus_dom"]))

    st_mid = (highs[-1] + lows[-1]) / 2.0
    st_bull = px > (ma20 - atr_val * 0.5) and ema5[-1] >= ema12[-1]
    st_bear = px < (ma20 + atr_val * 0.5) and ema5[-1] < ema12[-1]
    sigs.append(_emit("trend_supertrend", "trend",
                      "long" if st_bull else ("short" if st_bear else "neutral"),
                      0.72 if (st_bull or st_bear) else 0.3, 0.77,
                      ["supertrend_bull"] if st_bull else ["supertrend_bear"]))

    # ---- Group 2: breakout (6) ----
    orb_hi = max(highs[:min(3, n)])
    orb_lo = min(lows[:min(3, n)])
    sigs.append(_emit("breakout_orb_5m", "breakout",
                      "long" if px > orb_hi else ("short" if px < orb_lo else "neutral"),
                      0.80 if (px > orb_hi or px < orb_lo) else 0.25, 0.80,
                      ["opening_range_break_up"] if px > orb_hi else (["opening_range_break_dn"] if px < orb_lo else ["inside_orb"])))

    sigs.append(_emit("breakout_donchian_10", "breakout",
                      "long" if px >= hi10 else ("short" if px <= lo10 else "neutral"),
                      0.78 if (px >= hi10 or px <= lo10) else 0.25, 0.76,
                      ["donchian_high_break"] if px >= hi10 else (["donchian_low_break"] if px <= lo10 else ["inside_channel"])))

    sigs.append(_emit("breakout_bb_sqz", "breakout",
                      "long" if px > bb_up else ("short" if px < bb_lo else "neutral"),
                      0.75 if (px > bb_up or px < bb_lo) else 0.25, 0.72,
                      ["bb_upper_break"] if px > bb_up else (["bb_lower_break"] if px < bb_lo else ["bb_squeeze"])))

    bos_up = px > hi5 and closes[-1] > opens[-1]
    bos_dn = px < lo5 and closes[-1] < opens[-1]
    sigs.append(_emit("breakout_smc_bos", "breakout",
                      "long" if bos_up else ("short" if bos_dn else "neutral"),
                      0.82 if (bos_up or bos_dn) else 0.30, 0.81,
                      ["smc_bos_bull"] if bos_up else (["smc_bos_bear"] if bos_dn else ["no_bos"])))

    sigs.append(_emit("breakout_prev_hl", "breakout",
                      "long" if px > highs[-2] else ("short" if px < lows[-2] else "neutral"),
                      0.68 if (px > highs[-2] or px < lows[-2]) else 0.25, 0.70,
                      ["prev_bar_high_break"] if px > highs[-2] else (["prev_bar_low_break"] if px < lows[-2] else ["inside_prev_bar"])))

    kc_up = ema21[-1] + 1.5 * atr_val
    kc_lo = ema21[-1] - 1.5 * atr_val
    sigs.append(_emit("breakout_keltner", "breakout",
                      "long" if px > kc_up else ("short" if px < kc_lo else "neutral"),
                      0.74 if (px > kc_up or px < kc_lo) else 0.25, 0.73,
                      ["keltner_up_break"] if px > kc_up else (["keltner_dn_break"] if px < kc_lo else ["inside_keltner"])))

    # ---- Group 3: mean_reversion (6) ----
    sigs.append(_emit("mr_rsi14_ext", "mean_reversion",
                      "long" if rsi14 < 32 else ("short" if rsi14 > 68 else "neutral"),
                      min(1.0, abs(rsi14 - 50) / 35.0), 0.70,
                      ["rsi_oversold"] if rsi14 < 32 else (["rsi_overbought"] if rsi14 > 68 else ["rsi_mid"])))

    sigs.append(_emit("mr_bb_band", "mean_reversion",
                      "long" if px <= bb_lo * 1.002 else ("short" if px >= bb_up * 0.998 else "neutral"),
                      0.68 if (px <= bb_lo * 1.002 or px >= bb_up * 0.998) else 0.25, 0.68,
                      ["bb_oversold_bounce"] if px <= bb_lo * 1.002 else (["bb_overbought_fade"] if px >= bb_up * 0.998 else ["bb_neutral"])))

    vwap_z = (px - vwap) / max(std20, atr_val * 0.5, 1e-6)
    sigs.append(_emit("mr_vwap_zscore", "mean_reversion",
                      "long" if vwap_z < -1.8 else ("short" if vwap_z > 1.8 else "neutral"),
                      min(1.0, abs(vwap_z) / 3.0), 0.72,
                      ["vwap_z_low"] if vwap_z < -1.8 else (["vwap_z_high"] if vwap_z > 1.8 else ["vwap_z_mid"])))

    sigs.append(_emit("mr_kd_cross", "mean_reversion",
                      "long" if (rsv > prev_rsv and rsv < 35) else ("short" if (rsv < prev_rsv and rsv > 65) else "neutral"),
                      0.66 if (rsv < 35 or rsv > 65) else 0.25, 0.68,
                      ["kd_golden_low"] if rsv < 35 else (["kd_death_high"] if rsv > 65 else ["kd_mid"])))

    sigs.append(_emit("mr_cci_revert", "mean_reversion",
                      "long" if cci < -110 else ("short" if cci > 110 else "neutral"),
                      min(1.0, abs(cci) / 200.0), 0.67,
                      ["cci_oversold"] if cci < -110 else (["cci_overbought"] if cci > 110 else ["cci_mid"])))

    sigs.append(_emit("mr_williams_r", "mean_reversion",
                      "long" if will_r < -82 else ("short" if will_r > -18 else "neutral"),
                      0.65 if (will_r < -82 or will_r > -18) else 0.25, 0.66,
                      ["willr_oversold"] if will_r < -82 else (["willr_overbought"] if will_r > -18 else ["willr_mid"])))

    # ---- Group 4: volume_price (6) ----
    bull_bar = closes[-1] > opens[-1]
    bear_bar = closes[-1] < opens[-1]
    sigs.append(_emit("vp_vol_surge_dir", "volume_price",
                      "long" if (vol_ratio >= 1.25 and bull_bar) else ("short" if (vol_ratio >= 1.25 and bear_bar) else "neutral"),
                      min(1.0, vol_ratio / 2.5) if vol_ratio >= 1.25 else 0.25, 0.83,
                      ["volume_expansion_bull"] if (vol_ratio >= 1.25 and bull_bar) else (["volume_expansion_bear"] if (vol_ratio >= 1.25 and bear_bar) else ["normal_volume"])))

    sigs.append(_emit("vp_obv_trend", "volume_price",
                      "long" if obv_delta > 0 else ("short" if obv_delta < 0 else "neutral"),
                      0.70 if obv_delta != 0 else 0.25, 0.74,
                      ["obv_rising"] if obv_delta > 0 else ["obv_falling"]))

    sigs.append(_emit("vp_clv_delta", "volume_price",
                      "long" if clv_sum > 0 else ("short" if clv_sum < 0 else "neutral"),
                      0.72 if clv_sum != 0 else 0.25, 0.76,
                      ["clv_buy_pressure"] if clv_sum > 0 else ["clv_sell_pressure"]))

    sigs.append(_emit("vp_mfi14", "volume_price",
                      "long" if mfi >= 58 else ("short" if mfi <= 42 else "neutral"),
                      min(1.0, abs(mfi - 50) / 40.0 + 0.3), 0.73,
                      ["mfi_inflow"] if mfi >= 58 else (["mfi_outflow"] if mfi <= 42 else ["mfi_neutral"])))

    sigs.append(_emit("vp_vwap_vol_confirm", "volume_price",
                      "long" if (px > vwap and vol_ratio >= 1.1) else ("short" if (px < vwap and vol_ratio >= 1.1) else "neutral"),
                      0.76 if vol_ratio >= 1.1 else 0.30, 0.78,
                      ["vwap_vol_bull"] if px > vwap else ["vwap_vol_bear"]))

    body_ratio = abs(closes[-1] - opens[-1]) / max(highs[-1] - lows[-1], 1e-6)
    sigs.append(_emit("vp_bar_body_vol", "volume_price",
                      "long" if (body_ratio >= 0.55 and bull_bar) else ("short" if (body_ratio >= 0.55 and bear_bar) else "neutral"),
                      min(1.0, body_ratio), 0.71,
                      ["strong_bull_body"] if (body_ratio >= 0.55 and bull_bar) else (["strong_bear_body"] if (body_ratio >= 0.55 and bear_bar) else ["indecision_bar"])))

    # ---- Group 5: event / microstructure (6) ----
    fvg_bull = n >= 4 and lows[-1] > highs[-3]
    fvg_bear = n >= 4 and highs[-1] < lows[-3]
    sigs.append(_emit("evt_fvg_retest", "event",
                      "long" if fvg_bull else ("short" if fvg_bear else "neutral"),
                      0.78 if (fvg_bull or fvg_bear) else 0.25, 0.76,
                      ["fvg_bull_gap"] if fvg_bull else (["fvg_bear_gap"] if fvg_bear else ["no_fvg"])))

    sigs.append(_emit("evt_orderbook_imb", "event",
                      "long" if ob_imb > 0.18 else ("short" if ob_imb < -0.18 else "neutral"),
                      min(1.0, abs(ob_imb) + 0.25), 0.79,
                      ["orderbook_bid_heavy"] if ob_imb > 0.18 else (["orderbook_ask_heavy"] if ob_imb < -0.18 else ["orderbook_balanced"])))

    gap_pct = ((opens[0] - prev_close) / prev_close * 100.0) if prev_close > 0 else 0.0
    sigs.append(_emit("evt_gap_momentum", "event",
                      "long" if (gap_pct >= 0.8 and px >= opens[0]) else ("short" if (gap_pct <= -0.8 and px <= opens[0]) else "neutral"),
                      min(1.0, abs(gap_pct) / 4.0 + 0.3), 0.70,
                      ["gap_up_hold"] if gap_pct >= 0.8 else (["gap_down_hold"] if gap_pct <= -0.8 else ["flat_open"])))

    near_hi = day_hi > 0 and ((day_hi - px) / day_hi) <= 0.004
    near_lo = day_lo > 0 and ((px - day_lo) / day_lo) <= 0.004
    sigs.append(_emit("evt_intraday_hl_test", "event",
                      "long" if near_hi else ("short" if near_lo else "neutral"),
                      0.75 if (near_hi or near_lo) else 0.25, 0.75,
                      ["testing_day_high"] if near_hi else (["testing_day_low"] if near_lo else ["mid_range"])))

    sigs.append(_emit("evt_spread_tight", "event",
                      "long" if chg_pct >= 1.2 else ("short" if chg_pct <= -1.2 else "neutral"),
                      min(1.0, abs(chg_pct) / 5.0 + 0.25), 0.68,
                      ["intraday_momentum_up"] if chg_pct >= 1.2 else (["intraday_momentum_dn"] if chg_pct <= -1.2 else ["mild_move"])))

    sigs.append(_emit("evt_vol_spike_break", "event",
                      "long" if (vol_ratio >= 1.6 and px >= hi5) else ("short" if (vol_ratio >= 1.6 and px <= lo5) else "neutral"),
                      0.85 if (vol_ratio >= 1.6 and (px >= hi5 or px <= lo5)) else 0.25, 0.82,
                      ["spike_break_up"] if (vol_ratio >= 1.6 and px >= hi5) else (["spike_break_dn"] if (vol_ratio >= 1.6 and px <= lo5) else ["no_spike_break"])))

    meta_ctx = {
        "px": px,
        "chg_pct": round(chg_pct, 2),
        "atr": round(atr_val, 2),
        "vwap": round(vwap, 2),
        "vol_ratio": round(vol_ratio, 2),
        "bb_width": round(bb_width, 4),
        "bars": n,
    }
    return sigs, meta_ctx


def _free_local_system_one_classifier(model_input: dict[str, Any]) -> dict[str, Any]:
    """
    Zero-cost ($0.00) System One / AnyJev-style bounded discrete classifier (Shadow Mode).
    Takes ONLY the deterministic summary JSON; never bypasses hard_blocks or sets leverage.
    Returns typed Choice + calibrated-style probability distributions for:
      - signal_quality: clear | mixed | insufficient | blocked
      - action_tier: ignore | watch | review | small_candidate | normal_candidate
      - review_required: bool + prob
    """
    hard_blocks = list(model_input.get("hard_blocks") or [])
    allowed = list(model_input.get("allowed_actions") or ["ignore", "watch"])
    cov = model_input.get("signal_coverage") or {}
    valid_cnt = int(cov.get("valid") or 0)
    ws = float(model_input.get("weighted_score") or 0.0)
    abs_ws = abs(ws)
    grp_agree = float(model_input.get("group_agreement") or 0.0)
    aligned_groups = int(model_input.get("aligned_groups") or 0)

    # 1. signal_quality probabilities
    if any(b in ("stale_data", "liquidity_blocked", "volatility_blocked") for b in hard_blocks):
        q_probs = {"clear": 0.02, "mixed": 0.05, "insufficient": 0.08, "blocked": 0.85}
        sig_quality = "blocked"
    elif "data_insufficient" in hard_blocks or valid_cnt < 24:
        q_probs = {"clear": 0.03, "mixed": 0.12, "insufficient": 0.80, "blocked": 0.05}
        sig_quality = "insufficient"
    elif "strategy_conflicted" in hard_blocks or aligned_groups < 2:
        q_probs = {"clear": 0.12, "mixed": 0.78, "insufficient": 0.05, "blocked": 0.05}
        sig_quality = "mixed"
    else:
        p_clear = _clamp(0.45 + abs_ws * 0.45 + grp_agree * 0.25, 0.50, 0.96)
        p_mixed = round(max(0.02, 1.0 - p_clear - 0.02), 4)
        q_probs = {"clear": round(p_clear, 4), "mixed": p_mixed, "insufficient": 0.01, "blocked": 0.01}
        sig_quality = "clear"

    # 2. action_tier strictly constrained to allowed_actions (NEVER bypass hard_blocks)
    tier_scores = {
        "ignore": 0.10,
        "watch": 0.20,
        "review": 0.20,
        "small_candidate": 0.05,
        "normal_candidate": 0.02,
    }
    if sig_quality == "blocked":
        tier_scores = {"ignore": 0.82, "watch": 0.15, "review": 0.03, "small_candidate": 0.0, "normal_candidate": 0.0}
    elif sig_quality == "insufficient":
        tier_scores = {"ignore": 0.48, "watch": 0.44, "review": 0.08, "small_candidate": 0.0, "normal_candidate": 0.0}
    elif sig_quality == "mixed":
        tier_scores = {"ignore": 0.12, "watch": 0.34, "review": 0.50, "small_candidate": 0.04, "normal_candidate": 0.0}
    else:
        if abs_ws >= 0.48 and grp_agree >= 0.75 and aligned_groups >= 3:
            tier_scores = {"ignore": 0.02, "watch": 0.08, "review": 0.15, "small_candidate": 0.30, "normal_candidate": 0.45}
        elif abs_ws >= 0.30 and grp_agree >= 0.60 and aligned_groups >= 2:
            tier_scores = {"ignore": 0.04, "watch": 0.16, "review": 0.24, "small_candidate": 0.46, "normal_candidate": 0.10}
        else:
            tier_scores = {"ignore": 0.12, "watch": 0.46, "review": 0.32, "small_candidate": 0.08, "normal_candidate": 0.02}

    # Mask disallowed actions to 0 and re-normalize
    filtered = {k: (v if k in allowed else 0.0) for k, v in tier_scores.items()}
    total_p = sum(filtered.values()) or 1.0
    action_probs = {k: round(v / total_p, 4) for k, v in filtered.items()}
    chosen_action = max(action_probs.items(), key=lambda kv: kv[1])[0]

    review_prob = round(_clamp(action_probs.get("review", 0.0) + (0.35 if sig_quality == "mixed" else 0.0)), 4)
    return {
        "engine": "FREE_LOCAL_SYSTEM_ONE_SHADOW",
        "prompt_version": PROMPT_VERSION,
        "shadow_mode": True,
        "cost_usd": 0.0,
        "signal_quality": sig_quality,
        "signal_quality_probs": q_probs,
        "action_tier": chosen_action,
        "action_probs": action_probs,
        "review_required": bool(chosen_action == "review" or review_prob >= 0.45),
        "review_prob": review_prob,
    }


def evaluate_symbol_30(
    *,
    symbol: str,
    stock_name: str,
    market: str,
    candles: list[dict[str, Any]],
    quote: dict[str, Any],
    kill_switch: bool = False,
    min_turnover_twd: float = 1_000_000.0,
) -> dict[str, Any]:
    """Full 30-strategy deterministic statistics + hard risk gates + free System One shadow decision."""
    t0 = time.perf_counter()
    now_dt = datetime.now(TW_TZ)
    as_of = now_dt.isoformat(timespec="seconds")

    signals, ctx = _evaluate_30_signals(symbol, market, candles, quote, now_dt)

    # 4.1 Basic Counts (only valid & non-no_signal)
    valid_sigs = [s for s in signals if s["is_valid"] and s["direction"] != "no_signal"]
    L = sum(1 for s in valid_sigs if s["direction"] == "long")
    S = sum(1 for s in valid_sigs if s["direction"] == "short")
    N = sum(1 for s in valid_sigs if s["direction"] == "neutral")
    V = L + S + N
    net_vote = L - S
    long_short_ratio = round(L / max(L + S, 1), 4)

    # 4.2 Deterministic Weighted Score (-1 ~ +1)
    num = 0.0
    den = 0.0
    for s in valid_sigs:
        w = STRATEGY_WEIGHT_MAP.get(s["strategy_id"], 1.0)
        sw = w * float(s["strength"]) * float(s["confidence"])
        d_val = 1.0 if s["direction"] == "long" else (-1.0 if s["direction"] == "short" else 0.0)
        num += d_val * sw
        den += sw
    weighted_score = round((num / den) if den > 1e-9 else 0.0, 4)

    # 4.3 Group Consistency (5 independent groups)
    group_summary: dict[str, dict[str, Any]] = {}
    grp_dirs: list[str] = []
    for grp in ("trend", "breakout", "mean_reversion", "volume_price", "event"):
        gs = [s for s in valid_sigs if s["strategy_group"] == grp]
        g_num = 0.0
        g_den = 0.0
        for s in gs:
            w = STRATEGY_WEIGHT_MAP.get(s["strategy_id"], 1.0)
            sw = w * float(s["strength"]) * float(s["confidence"])
            d_val = 1.0 if s["direction"] == "long" else (-1.0 if s["direction"] == "short" else 0.0)
            g_num += d_val * sw
            g_den += sw
        g_score = round((g_num / g_den) if g_den > 1e-9 else 0.0, 4)
        g_dir = "long" if g_score > 0.12 else ("short" if g_score < -0.12 else "neutral")
        group_summary[grp] = {"direction": g_dir, "score": g_score, "valid": len(gs)}
        if g_dir != "neutral":
            grp_dirs.append(g_dir)

    overall_dir = "long" if weighted_score > 0.08 else ("short" if weighted_score < -0.08 else "neutral")
    aligned_groups = sum(1 for gd in grp_dirs if gd == overall_dir and overall_dir != "neutral")
    effective_groups = max(1, len(grp_dirs))
    group_agreement = round((aligned_groups / effective_groups) if overall_dir != "neutral" else 0.0, 4)

    # 4.4 Hard Risk & Quality Gates
    hard_blocks: list[str] = []
    if V < 24:
        hard_blocks.append("data_insufficient")

    ls_total = L + S
    if ls_total >= 4 and (L / ls_total) >= 0.30 and (S / ls_total) >= 0.30:
        hard_blocks.append("strategy_conflicted")
    elif group_summary["trend"]["direction"] != "neutral" and group_summary["volume_price"]["direction"] != "neutral":
        if group_summary["trend"]["direction"] != group_summary["volume_price"]["direction"]:
            if "strategy_conflicted" not in hard_blocks:
                hard_blocks.append("strategy_conflicted")

    px = float(ctx["px"])
    total_vol = float(quote.get("volume") or sum(float(c.get("volume") or 0) for c in candles[-20:]))
    est_turnover = total_vol * px
    liquidity_ok = bool(px > 0 and (est_turnover >= min_turnover_twd or total_vol >= 200))
    if not liquidity_ok:
        hard_blocks.append("liquidity_blocked")

    chg_pct = abs(float(ctx["chg_pct"]))
    volatility_blocked = bool(kill_switch or chg_pct >= 9.7)
    if volatility_blocked:
        hard_blocks.append("volatility_blocked")

    # Determine allowed_actions strictly from deterministic hard risk gates
    if any(b in ("stale_data", "liquidity_blocked", "volatility_blocked") for b in hard_blocks):
        allowed_actions = ["ignore", "watch"]
    elif "data_insufficient" in hard_blocks:
        allowed_actions = ["ignore", "watch"]
    elif "strategy_conflicted" in hard_blocks or aligned_groups < 2:
        allowed_actions = ["ignore", "watch", "review"]
    else:
        allowed_actions = ["ignore", "watch", "review", "small_candidate", "normal_candidate"]

    model_input = {
        "symbol": symbol,
        "as_of": as_of,
        "session": "continuous",
        "signal_coverage": {"valid": V, "total": len(STRATEGY_CATALOG)},
        "counts": {"long": L, "short": S, "neutral": N},
        "long_short_ratio": long_short_ratio,
        "net_vote": net_vote,
        "weighted_score": weighted_score,
        "group_summary": group_summary,
        "group_agreement": group_agreement,
        "aligned_groups": aligned_groups,
        "market_state": {
            "trend": "up" if ctx["chg_pct"] > 0.3 else ("down" if ctx["chg_pct"] < -0.3 else "flat"),
            "volatility": "high" if chg_pct >= 3.5 else "normal",
        },
        "execution": {"liquidity_ok": liquidity_ok, "spread_ok": True},
        "hard_blocks": hard_blocks,
        "allowed_actions": allowed_actions,
    }

    decision = _free_local_system_one_classifier(model_input)
    latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)

    dir_zh = "偏多" if overall_dir == "long" else ("偏空(可現沖先賣)" if overall_dir == "short" else "盤整無共識")
    blk_zh = ("｜阻擋:" + ",".join(hard_blocks)) if hard_blocks else "｜風控通過"
    one_line_summary = (
        f"{symbol} {stock_name} @{px:.2f}({ctx['chg_pct']:+.2f}%) ｜ "
        f"覆蓋 {V}/30 · 多{L}/空{S}/中{N} (比 {long_short_ratio:.2f}) · 加權 {weighted_score:+.2f} · "
        f"群組一致 {int(group_agreement * 100)}%({aligned_groups}組) → {dir_zh} · "
        f"SystemOne={decision['signal_quality']}/{decision['action_tier']}{blk_zh}"
    )

    return {
        "symbol": symbol,
        "stock_name": stock_name,
        "market": market,
        "as_of": as_of,
        "price": round(px, 2),
        "change_pct": ctx["chg_pct"],
        "weight_version": WEIGHT_VERSION,
        "overall_direction": overall_dir,
        "model_input": model_input,
        "decision": decision,
        "one_line_summary": one_line_summary,
        "latency_ms": latency_ms,
        "signals_sample": signals[:10],
        "signals_total": len(signals),
    }
