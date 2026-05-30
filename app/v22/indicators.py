"""
Indicator helpers — pandas_ta wrappers used by S3 and S5 detection.

These match the indicator stack used by the audited V22 backtest exactly:
EMA(21,55,200), RSI(14), ATR(14), Bollinger(20,2), simple swing-pivot S/R.
"""

from __future__ import annotations

import pandas as pd
import pandas_ta as ta

from .config import (
    EMA_FAST, EMA_SLOW, EMA_TREND,
    RSI_PERIOD, ATR_PERIOD,
)


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add EMA / RSI / ATR / BB columns. Returns dropna-trimmed copy."""
    df = df.copy()
    df["ema_fast"] = ta.ema(df["close"], length=EMA_FAST)
    df["ema_slow"] = ta.ema(df["close"], length=EMA_SLOW)
    df["ema_trend"] = ta.ema(df["close"], length=EMA_TREND)
    df["rsi"] = ta.rsi(df["close"], length=RSI_PERIOD)
    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=ATR_PERIOD)
    # Bollinger bands used by S5
    bb = ta.bbands(df["close"], length=20, std=2.0)
    if bb is not None and not bb.empty:
        # pandas_ta returns BBL_20_2.0 / BBM_20_2.0 / BBU_20_2.0
        bbl = next((c for c in bb.columns if c.startswith("BBL_")), None)
        bbm = next((c for c in bb.columns if c.startswith("BBM_")), None)
        bbu = next((c for c in bb.columns if c.startswith("BBU_")), None)
        if bbl and bbm and bbu:
            df["bb_low"] = bb[bbl]
            df["bb_mid"] = bb[bbm]
            df["bb_upper"] = bb[bbu]
    return df.dropna()


def find_sr_levels(df: pd.DataFrame, lookback: int = 80) -> list[float]:
    """Simple swing-pivot S/R: a candle whose high/low exceeds its 2 neighbours."""
    recent = df.tail(lookback)
    levels: list[float] = []
    if len(recent) < 5:
        return levels
    highs = recent["high"].to_numpy()
    lows = recent["low"].to_numpy()
    for i in range(2, len(recent) - 2):
        h = highs[i]
        l = lows[i]
        if h > highs[i - 1] and h > highs[i - 2] and h > highs[i + 1] and h > highs[i + 2]:
            levels.append(float(h))
        if l < lows[i - 1] and l < lows[i - 2] and l < lows[i + 1] and l < lows[i + 2]:
            levels.append(float(l))
    return sorted(set(round(x, 6) for x in levels))


def nearest_levels(
    price: float,
    levels: list[float],
    direction: str,
) -> tuple[float | None, float | None, float | None]:
    """Return (nearest_below_or_above, first_tp, second_tp) given direction."""
    if direction == "long":
        below = [lvl for lvl in levels if lvl < price]
        above = [lvl for lvl in levels if lvl > price]
        return (
            max(below) if below else None,
            min(above) if above else None,
            above[1] if len(above) > 1 else None,
        )
    above = [lvl for lvl in levels if lvl > price]
    below = [lvl for lvl in levels if lvl < price]
    return (
        min(above) if above else None,
        max(below) if below else None,
        below[-2] if len(below) > 1 else None,
    )
