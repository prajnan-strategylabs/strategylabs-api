"""
V22 strategy parameters.

Mirrors crypto-bot/swingbot/strategy_3/config.py — keep these in sync if the
backtest config changes. Live signals here should match the audited CSV.

Telegram / risk-tracker fields are NOT ported — the live scanner persists
signals to Supabase rather than alerting Telegram, and risk tracking is
done off the live signal log (no compounding for V22 by design).
"""

# ── Account (matches backtest config) ────────────────────────────────────────
ACCOUNT_SIZE = 5000
RISK_PER_TRADE_PCT = 1.0  # 1% of starting balance per trade

# ── Signal quality ───────────────────────────────────────────────────────────
MIN_RR = 2.0

# ── Indicators ───────────────────────────────────────────────────────────────
EMA_FAST = 21
EMA_SLOW = 55
EMA_TREND = 200
RSI_PERIOD = 14
ATR_PERIOD = 14
ATR_SL_MULTIPLIER = 1.0

# ── S3 pullback entry ────────────────────────────────────────────────────────
RSI_PULLBACK_MIN = 35
RSI_PULLBACK_MAX = 60
SR_LOOKBACK = 80
EMA_SPREAD_MIN = 3.0
PB_CANDLES_MIN_LONG = 1
PB_CANDLES_MIN_SHORT = 1
PB_CANDLES_MAX = 8
RSI_DIR_MAX = 5.0

# ── Quality filters ──────────────────────────────────────────────────────────
ATR_QUIET_THRESHOLD = 2.5
WEAK_ENTRY_DIST_PCT = 1.5
WEAK_ENTRY_BODY_PCT = 65.0
ADX_VIOLENT_THRESHOLD = 60
CI_VIOLENT_THRESHOLD = 45
VIOLENT_SIZE_MULT = 0.5

# ── Strategy toggles (V22 = S3 + S5) ─────────────────────────────────────────
USE_S5 = True
USE_BTC_FILTER_S5 = True

# ── Regime filter ────────────────────────────────────────────────────────────
SKIP_TRANSITIONAL_REGIME = True

# ── Scanner cadence ──────────────────────────────────────────────────────────
SCAN_INTERVAL_SECONDS = 60 * 60 * 4  # one 4H candle close
EXIT_CHECK_INTERVAL_SECONDS = 60     # 1-min exit polling for open positions

# ── Watchlist (top ~47 USDT pairs) — mirrors crypto-bot ──────────────────────
WATCHLIST = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "ADA/USDT", "AVAX/USDT",
    "LINK/USDT", "DOT/USDT", "ATOM/USDT", "BNB/USDT", "MATIC/USDT", "UNI/USDT",
    "NEAR/USDT", "TRX/USDT", "FIL/USDT", "ALGO/USDT", "HBAR/USDT",
    "OP/USDT", "APT/USDT",
    "AAVE/USDT", "ALT/USDT", "BCH/USDT", "CHZ/USDT", "COS/USDT", "DASH/USDT",
    "DOGE/USDT", "ENA/USDT", "FET/USDT", "FIDA/USDT", "GMT/USDT", "ICP/USDT",
    "INJ/USDT", "LTC/USDT", "LUNC/USDT", "ME/USDT", "ONDO/USDT",
    "PENGU/USDT", "PEPE/USDT", "RENDER/USDT", "SUI/USDT", "TAO/USDT", "TON/USDT",
    "TRUMP/USDT", "UTK/USDT", "VIRTUAL/USDT", "WLD/USDT", "ZEC/USDT",
]

# ── Trailing stop (used by the exit tracker) ─────────────────────────────────
TRAIL_ATR_MULT = 1.5
TRAIL_TIGHTEN_R = 3.0      # tighten once price moves 3R in our favour
TRAIL_ATR_TIGHT = 1.0
