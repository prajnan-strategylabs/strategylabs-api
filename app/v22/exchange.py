"""
Binance OHLCV fetcher with TTL in-memory cache.

ccxt is synchronous; the scanner wraps these calls in asyncio.to_thread().
Fly machines have ephemeral disk, so we don't pickle to disk like the
backtest does — we keep recent bars in memory only.

The cache TTL is set per timeframe (1d → 4h, 4h → 30min, 1h → 5min).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import ccxt
import pandas as pd

log = logging.getLogger(__name__)

# ── Binance client (rate-limited) ────────────────────────────────────────────
_exchange = ccxt.binance({"enableRateLimit": True})

# ── In-memory OHLCV cache ────────────────────────────────────────────────────
_CACHE_TTL_S = {"1d": 4 * 3600, "4h": 30 * 60, "1h": 5 * 60}


@dataclass
class _CacheEntry:
    df: pd.DataFrame
    fetched_at: float


_cache: dict[tuple[str, str], _CacheEntry] = {}


def fetch_ohlcv(
    symbol: str,
    timeframe: str = "4h",
    limit: int = 400,
    force: bool = False,
) -> pd.DataFrame:
    """Fetch OHLCV bars from Binance. Cached per (symbol, timeframe)."""
    key = (symbol, timeframe)
    ttl = _CACHE_TTL_S.get(timeframe, 60)
    entry = _cache.get(key)
    if not force and entry and (time.time() - entry.fetched_at) < ttl:
        return entry.df

    try:
        raw = _exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    except Exception as e:
        log.warning(f"fetch_ohlcv({symbol}, {timeframe}) failed: {e}")
        return entry.df if entry else pd.DataFrame()

    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.astype(
        {"open": float, "high": float, "low": float, "close": float, "volume": float}
    )
    _cache[key] = _CacheEntry(df=df, fetched_at=time.time())
    return df


def latest_price(symbol: str) -> Optional[float]:
    """Cheap call — pulls the latest 1m candle from cache or Binance."""
    try:
        df = fetch_ohlcv(symbol, "1h", limit=2)
    except Exception:
        return None
    if df.empty:
        return None
    return float(df.iloc[-1]["close"])


def invalidate_cache(symbol: Optional[str] = None) -> None:
    """Clear the OHLCV cache (per-symbol or fully)."""
    if symbol is None:
        _cache.clear()
        return
    for k in list(_cache):
        if k[0] == symbol:
            _cache.pop(k, None)
