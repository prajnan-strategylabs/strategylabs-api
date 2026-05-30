"""
S3 — Pullback signal detector (live mode).

Ported from crypto-bot/swingbot/strategy_3/signal_engine.py::check_signal.
Detects high-conviction pullback entries in strong daily trends, gated by
BTC macro regime and a chain of optimization filters discovered through
8-year walk-forward audit.
"""

from __future__ import annotations

import logging

import pandas as pd
import pandas_ta as ta

from .config import (
    ACCOUNT_SIZE,
    RISK_PER_TRADE_PCT,
    MIN_RR,
    ATR_SL_MULTIPLIER,
    RSI_PULLBACK_MIN,
    RSI_PULLBACK_MAX,
    SR_LOOKBACK,
    EMA_SPREAD_MIN,
    PB_CANDLES_MIN_LONG,
    PB_CANDLES_MIN_SHORT,
    PB_CANDLES_MAX,
    RSI_DIR_MAX,
    ATR_QUIET_THRESHOLD,
    WEAK_ENTRY_DIST_PCT,
    WEAK_ENTRY_BODY_PCT,
    ADX_VIOLENT_THRESHOLD,
    CI_VIOLENT_THRESHOLD,
    VIOLENT_SIZE_MULT,
)
from .exchange import fetch_ohlcv
from .indicators import add_indicators, find_sr_levels

log = logging.getLogger(__name__)


def check_s3_signal(
    symbol: str,
    trend: str,
    btc_regime: str,
) -> dict | None:
    """
    Situation-aware pullback entry. Returns a signal dict if all conditions
    align, else None.

    Args
    ----
    symbol      : Binance pair (e.g. "BTC/USDT")
    trend       : "bull" | "bear" | "flat"  (from regime.get_daily_trend)
    btc_regime  : "bull" | "transition" | "bear" (from regime.get_btc_regime)
    """
    # ── BTC regime gate ──────────────────────────────────────────────────────
    if trend == "bull" and btc_regime != "bull":
        return None
    if trend == "bear" and btc_regime == "bull":
        return None
    if trend == "flat":
        return None

    # ── 4H data ──────────────────────────────────────────────────────────────
    df = fetch_ohlcv(symbol, timeframe="4h", limit=400)
    if df.empty or len(df) < 220:
        return None

    df = add_indicators(df)
    if len(df) < 2:
        return None

    last = df.iloc[-1]
    price = float(last["close"])
    atr = float(last["atr"])
    rsi = float(last["rsi"])
    ema_fast = float(last["ema_fast"])
    ema_slow = float(last["ema_slow"])

    # ── 4H EMA alignment with daily trend ────────────────────────────────────
    if trend == "bull" and ema_fast <= ema_slow:
        return None
    if trend == "bear" and ema_fast >= ema_slow:
        return None

    # ── Trend strength gate (EMA21-EMA55 spread ≥ 3%) ────────────────────────
    ema_spread_pct = abs(ema_fast - ema_slow) / ema_slow * 100
    if ema_spread_pct < EMA_SPREAD_MIN:
        return None

    # ── Quiet-market skip (ATR < 2.5% of price) ──────────────────────────────
    atr_pct = atr / price * 100
    if atr_pct < ATR_QUIET_THRESHOLD:
        return None

    # ── Pullback depth ───────────────────────────────────────────────────────
    pb_candles = 0
    for j in range(len(df) - 2, max(len(df) - (PB_CANDLES_MAX + 5), 0), -1):
        row_j = df.iloc[j]
        if trend == "bull" and row_j["close"] < row_j["ema_fast"]:
            pb_candles += 1
        elif trend == "bear" and row_j["close"] > row_j["ema_fast"]:
            pb_candles += 1
        else:
            break

    if pb_candles > PB_CANDLES_MAX:
        return None
    pb_min = PB_CANDLES_MIN_LONG if trend == "bull" else PB_CANDLES_MIN_SHORT
    if pb_candles < pb_min:
        return None

    # ── RSI velocity guard ───────────────────────────────────────────────────
    rsi_3ago = float(df.iloc[-4]["rsi"]) if len(df) >= 4 else rsi
    if (rsi - rsi_3ago) > RSI_DIR_MAX:
        return None

    # ── EMA21 touch + conviction close ───────────────────────────────────────
    touched_long = last["low"] <= ema_fast and last["close"] > ema_fast
    touched_short = last["high"] >= ema_fast and last["close"] < ema_fast

    body = abs(last["close"] - last["open"])
    candle_rng = last["high"] - last["low"]
    conviction = candle_rng > 0 and body > candle_rng * 0.5
    body_pct = (body / candle_rng * 100) if candle_rng > 0 else 0

    rsi_reset = RSI_PULLBACK_MIN < rsi < RSI_PULLBACK_MAX

    direction = None
    if trend == "bull" and touched_long and conviction and rsi_reset:
        direction = "long"
    elif trend == "bear" and touched_short and conviction and rsi_reset:
        direction = "short"
    if not direction:
        return None

    # ── Weak-entry skip ──────────────────────────────────────────────────────
    ema21_dist_pct = abs(price - ema_fast) / ema_fast * 100
    if ema21_dist_pct > WEAK_ENTRY_DIST_PCT and body_pct < WEAK_ENTRY_BODY_PCT:
        return None

    # ── Stop loss ────────────────────────────────────────────────────────────
    if direction == "long":
        stop_loss = round(float(last["low"]) - atr * ATR_SL_MULTIPLIER, 6)
    else:
        stop_loss = round(float(last["high"]) + atr * ATR_SL_MULTIPLIER, 6)
    sl_distance = abs(price - stop_loss)
    if sl_distance == 0:
        return None

    # ── TP: first S/R level giving ≥ MIN_RR ──────────────────────────────────
    min_tp_dist = sl_distance * MIN_RR
    sr_levels = find_sr_levels(df, lookback=SR_LOOKBACK)
    if direction == "long":
        viable = [l for l in sr_levels if l >= price + min_tp_dist]
        tp1 = min(viable) if viable else round(price + sl_distance * MIN_RR, 6)
        above = [l for l in sr_levels if l > tp1]
        tp2 = min(above) if above else round(price + sl_distance * MIN_RR * 2, 6)
    else:
        viable = [l for l in sr_levels if l <= price - min_tp_dist]
        tp1 = max(viable) if viable else round(price - sl_distance * MIN_RR, 6)
        below = [l for l in sr_levels if l < tp1]
        tp2 = max(below) if below else round(price - sl_distance * MIN_RR * 2, 6)
    rr = abs(tp1 - price) / sl_distance
    if rr < MIN_RR:
        return None

    # ── Violent-trend half-size adjustment ───────────────────────────────────
    size_multiplier = 1.0
    try:
        daily_df = fetch_ohlcv(symbol, timeframe="1d", limit=120)
        if not daily_df.empty and len(daily_df) >= 40:
            d_sub = daily_df.tail(100).copy().reset_index(drop=True)
            adx_df = ta.adx(d_sub["high"], d_sub["low"], d_sub["close"], length=14)
            adx_col = next((c for c in adx_df.columns if c.startswith("ADX_")), None)
            adx_val = float(adx_df[adx_col].iloc[-1]) if adx_col else 0
            chop_raw = ta.chop(d_sub["high"], d_sub["low"], d_sub["close"], length=14)
            chop_val = (
                float(chop_raw.iloc[-1])
                if not isinstance(chop_raw, pd.DataFrame)
                else float(chop_raw.iloc[-1, 0])
            )
            if adx_val > ADX_VIOLENT_THRESHOLD and chop_val > CI_VIOLENT_THRESHOLD:
                size_multiplier = VIOLENT_SIZE_MULT
    except Exception:
        pass

    risk_amount = ACCOUNT_SIZE * (RISK_PER_TRADE_PCT / 100) * size_multiplier
    position_size = risk_amount / sl_distance

    return {
        "symbol": symbol,
        "direction": direction,
        "trend": trend,
        "btc_regime": btc_regime,
        "entry": round(price, 6),
        "stop_loss": stop_loss,
        "tp1": round(tp1, 6),
        "tp2": round(tp2, 6),
        "rr": round(rr, 2),
        "risk_usd": round(risk_amount, 2),
        "position_size": round(position_size, 4),
        "size_mult": size_multiplier,
        "rsi": round(rsi, 1),
        "atr": round(atr, 6),
        "atr_pct": round(atr_pct, 2),
        "pb_candles": pb_candles,
        "spread_pct": round(ema_spread_pct, 2),
        "strategy": "S3",
    }
