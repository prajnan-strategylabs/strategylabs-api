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


def resolve_symbol(symbol: str) -> str:
    """Resolve a loosely-formatted symbol ('btc', 'BTCUSDT', 'BTC/USDT') to a ccxt market symbol."""
    try:
        symbol_upper = symbol.upper().strip()
        if not _exchange.markets:
            try:
                _exchange.load_markets()
            except Exception as e:
                log.warning(f"Failed to load markets during resolve_symbol: {e}")

        if _exchange.markets:
            if symbol_upper in _exchange.markets:
                return symbol_upper
            # 1. Try match without punctuation
            norm_target = symbol_upper.replace("/", "").replace("-", "").replace("_", "").replace(" ", "")
            for m_sym in _exchange.markets:
                m_norm = m_sym.upper().replace("/", "").replace("-", "").replace("_", "").replace(" ", "")
                if norm_target == m_norm:
                    return m_sym
            # 2. Try common quote currency suffixes
            for quote in ["/USDT", "/USDC", "/BUSD", "/BTC"]:
                test_sym = f"{symbol_upper}{quote}"
                if test_sym in _exchange.markets:
                    return test_sym
    except Exception as e:
        log.warning(f"Error normalising symbol '{symbol}': {e}")
    return symbol


def fetch_ohlcv(
    symbol: str,
    timeframe: str = "4h",
    limit: int = 400,
    force: bool = False,
) -> pd.DataFrame:
    """Fetch OHLCV bars from Binance. Cached per (symbol, timeframe)."""
    symbol = resolve_symbol(symbol)

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


_TF_MS = {"1d": 86_400_000, "4h": 14_400_000, "1h": 3_600_000}
_RANGE_CACHE_TTL_S = 4 * 3600
_range_cache: dict[tuple[str, str, int, int], _CacheEntry] = {}


def fetch_ohlcv_range(
    symbol: str,
    timeframe: str,
    since_ms: int,
    until_ms: int,
    max_bars: int = 25_000,
) -> pd.DataFrame:
    """
    Fetch OHLCV bars covering [since_ms, until_ms] by paginating Binance's
    per-request limit. Used by the backtest engine, where the window can span
    years. Cached per (symbol, timeframe, since-day, until-day).
    """
    symbol = resolve_symbol(symbol)
    tf_ms = _TF_MS.get(timeframe)
    if tf_ms is None:
        raise ValueError(f"Unsupported timeframe for range fetch: {timeframe}")

    key = (symbol, timeframe, since_ms // 86_400_000, until_ms // 86_400_000)
    entry = _range_cache.get(key)
    if entry and (time.time() - entry.fetched_at) < _RANGE_CACHE_TTL_S:
        return entry.df

    all_rows: list[list] = []
    cursor = since_ms
    while cursor < until_ms and len(all_rows) < max_bars:
        try:
            raw = _exchange.fetch_ohlcv(symbol, timeframe, since=cursor, limit=1000)
        except Exception as e:
            log.warning(f"fetch_ohlcv_range({symbol}, {timeframe}) page failed: {e}")
            break
        if not raw:
            break
        all_rows.extend(raw)
        last_ts = raw[-1][0]
        if last_ts <= cursor:  # no forward progress — bail out
            break
        cursor = last_ts + tf_ms

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
    df = df[df["timestamp"] <= until_ms]
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.astype(
        {"open": float, "high": float, "low": float, "close": float, "volume": float}
    )
    _range_cache[key] = _CacheEntry(df=df, fetched_at=time.time())
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
