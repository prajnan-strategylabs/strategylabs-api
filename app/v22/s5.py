"""
S5 — Break-Retest-Go (BRG).

Ported verbatim from crypto-bot/swingbot/strategy_3/strategy_5_signal.py
with only the import paths adjusted.
"""

from __future__ import annotations

import pandas as pd

# ── S5 config (matches the audited backtest) ─────────────────────────────────
S5_BB_LENGTH = 20
S5_BB_STD = 2.0
S5_MIN_BREAK_BODY = 0.60     # breakout candle body ≥ 60% of range
S5_MIN_CONFIRM_BODY = 0.50   # confirmation candle body ≥ 50%
S5_RETEST_WINDOW = 6         # max 4H candles after breakout to find retest+confirm
S5_RETEST_PROX_ATR = 0.5     # retest must come within 0.5×ATR of broken band
S5_SL_BUFFER_ATR = 0.5       # stop is 0.5×ATR beyond retest extreme
S5_TP1_RR = 2.0
S5_TP2_RR = 4.0
S5_MIN_RR = 2.0
S5_MAX_ATR_PCT = 5.0


def check_s5_signal_at(
    four_h_df: pd.DataFrame,
    idx: int,
    daily_trend: str = "flat",
) -> dict | None:
    """
    Returns a signal if the 4H candle at `idx` is the confirmation candle of
    a valid break-retest-go pattern that started 1-6 candles earlier.

    `four_h_df` must already have indicator columns: rsi, atr, bb_low, bb_mid,
    bb_upper (added by indicators.add_indicators).
    """
    if idx < 40 or idx < S5_RETEST_WINDOW + 5:
        return None

    confirm = four_h_df.iloc[idx]
    confirm_price = float(confirm["close"])
    confirm_atr = float(confirm["atr"])
    if pd.isna(confirm_atr) or pd.isna(confirm.get("bb_low")) or pd.isna(confirm.get("bb_upper")):
        return None
    if confirm_price <= 0 or confirm_atr <= 0:
        return None

    # ── ATR sanity (skip CHAOS volatility) ───────────────────────────────────
    atr_pct = confirm_atr / confirm_price * 100
    if atr_pct > S5_MAX_ATR_PCT:
        return None

    # ── Confirmation candle body conviction ──────────────────────────────────
    rng = float(confirm["high"]) - float(confirm["low"])
    body = abs(float(confirm["close"]) - float(confirm["open"]))
    if rng <= 0 or body < rng * S5_MIN_CONFIRM_BODY:
        return None

    # Look backward 1..S5_RETEST_WINDOW candles for a valid BREAKOUT candle
    for back in range(1, S5_RETEST_WINDOW + 1):
        brk_idx = idx - back
        if brk_idx < 20:
            break

        brk = four_h_df.iloc[brk_idx]
        if pd.isna(brk.get("bb_low")) or pd.isna(brk.get("bb_upper")):
            continue
        brk_rng = float(brk["high"]) - float(brk["low"])
        brk_body = abs(float(brk["close"]) - float(brk["open"]))
        if brk_rng <= 0 or brk_body < brk_rng * S5_MIN_BREAK_BODY:
            continue

        long_break = brk["close"] > brk["bb_upper"] and brk["close"] > brk["open"]
        short_break = brk["close"] < brk["bb_low"] and brk["close"] < brk["open"]
        if not (long_break or short_break):
            continue

        direction = "long" if long_break else "short"
        broken_level = float(brk["bb_upper"] if long_break else brk["bb_low"])

        # Anti-counter-trend
        if direction == "long" and daily_trend == "bear":
            continue
        if direction == "short" and daily_trend == "bull":
            continue

        # Confirmation direction
        confirm_dir_ok = (confirm["close"] > confirm["open"]) if direction == "long" \
            else (confirm["close"] < confirm["open"])
        if not confirm_dir_ok:
            continue
        if direction == "long" and confirm["close"] <= broken_level:
            continue
        if direction == "short" and confirm["close"] >= broken_level:
            continue

        # Retest must come within proximity of the broken level
        retest_lo, retest_hi = brk_idx + 1, idx
        if retest_lo >= retest_hi:
            continue
        retest_slice = four_h_df.iloc[retest_lo:retest_hi]

        proximity = confirm_atr * S5_RETEST_PROX_ATR
        if direction == "long":
            min_low = float(retest_slice["low"].min())
            retest_ok = (min_low - broken_level) <= proximity
            retest_extreme = min_low
        else:
            max_high = float(retest_slice["high"].max())
            retest_ok = (broken_level - max_high) <= proximity
            retest_extreme = max_high
        if not retest_ok:
            continue

        # Build signal
        if direction == "long":
            sl = round(retest_extreme - confirm_atr * S5_SL_BUFFER_ATR, 6)
            sl_dist = confirm_price - sl
            if sl_dist <= 0:
                continue
            tp1 = round(confirm_price + sl_dist * S5_TP1_RR, 6)
            tp2 = round(confirm_price + sl_dist * S5_TP2_RR, 6)
        else:
            sl = round(retest_extreme + confirm_atr * S5_SL_BUFFER_ATR, 6)
            sl_dist = sl - confirm_price
            if sl_dist <= 0:
                continue
            tp1 = round(confirm_price - sl_dist * S5_TP1_RR, 6)
            tp2 = round(confirm_price - sl_dist * S5_TP2_RR, 6)

        rr = (abs(tp1 - confirm_price) / sl_dist) if sl_dist > 0 else 0
        if rr < S5_MIN_RR:
            continue

        bb_mid_val = brk.get("bb_mid", confirm.get("bb_mid", 0)) or 1
        spread_proxy = ((brk["bb_upper"] - brk["bb_low"]) / bb_mid_val * 100) if bb_mid_val else 0

        return {
            "direction":      direction,
            "entry":          round(confirm_price, 6),
            "stop_loss":      sl,
            "tp1":            tp1,
            "tp2":            tp2,
            "rr":             round(rr, 2),
            "rsi":            round(float(confirm["rsi"]) if not pd.isna(confirm["rsi"]) else 50.0, 1),
            "atr":            round(confirm_atr, 6),
            "ema_spread_pct": round(float(spread_proxy), 2),
            "atr_pct":        round(atr_pct, 2),
            "strategy":       "S5",
        }

    return None
