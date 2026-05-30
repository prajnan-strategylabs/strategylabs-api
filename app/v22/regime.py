"""
Regime classifiers.

- BTC macro regime  : bull / transition / bear  (gates which directions fire)
- Altcoin daily trend: bull / bear / flat (gates the strategy entirely)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd
import pandas_ta as ta

from .exchange import fetch_ohlcv

log = logging.getLogger(__name__)

# Cached for the day; reset on UTC date change.
_btc_regime_cache: dict[str, str] = {}


def get_btc_regime() -> str:
    """bull (BTC > EMA21) / transition (EMA55 < BTC ≤ EMA21) / bear (BTC ≤ EMA55)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if today in _btc_regime_cache:
        return _btc_regime_cache[today]

    df = fetch_ohlcv("BTC/USDT", timeframe="1d", limit=60)
    if df.empty or len(df) < 30:
        return "bull"  # default open; lets shorts and longs both fire

    df = df.copy()
    df["e21"] = ta.ema(df["close"], length=21)
    df["e55"] = ta.ema(df["close"], length=55)
    df = df.dropna()
    last = df.iloc[-1]
    price = float(last["close"])

    if price > last["e21"]:
        regime = "bull"
    elif price > last["e55"]:
        regime = "transition"
    else:
        regime = "bear"

    _btc_regime_cache[today] = regime
    log.info(
        f"BTC regime={regime}  price={price:.0f}  EMA21={last['e21']:.0f}  EMA55={last['e55']:.0f}"
    )
    # Prune yesterday's cache entries to keep this dict from growing forever
    for k in list(_btc_regime_cache):
        if k != today:
            _btc_regime_cache.pop(k, None)
    return regime


def get_daily_trend(symbol: str) -> str:
    """bull / bear / flat based on the coin's own daily EMA21 > 55 > 200 stack."""
    df = fetch_ohlcv(symbol, timeframe="1d", limit=250)
    if df.empty or len(df) < 210:
        return "flat"
    df = df.copy()
    df["e21"] = ta.ema(df["close"], length=21)
    df["e55"] = ta.ema(df["close"], length=55)
    df["e200"] = ta.ema(df["close"], length=200)
    df = df.dropna()
    if df.empty:
        return "flat"
    last = df.iloc[-1]
    price = float(last["close"])
    if price > last["e21"] > last["e55"] > last["e200"]:
        return "bull"
    if price < last["e21"] < last["e55"] < last["e200"]:
        return "bear"
    return "flat"
