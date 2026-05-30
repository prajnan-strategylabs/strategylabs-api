"""
V22 live signal engine.

Ported from crypto-bot/swingbot/strategy_3/ for in-process use by the
strategylabs-api scanner. Only the entry-detection + indicator code lives
here; the backtest harness (1500-line backtest.py) stays in crypto-bot.

Modules
-------
- config           : strategy parameters (S3 + S5 thresholds, watchlist)
- indicators       : EMA / RSI / ATR / BB / S-R helpers (pandas_ta wrappers)
- exchange         : ccxt Binance fetcher with TTL in-memory cache
- s3               : S3 pullback signal detection (live mode)
- s5               : S5 break-retest-go signal detection
- regime           : BTC macro regime + altcoin daily trend classifiers
- scanner          : asyncio background task (FastAPI lifespan)
"""
