"""
V22 live scanner — asyncio background task.

Wakes up every minute:
  - Always: checks open positions for TP/SL/trail hits, closes any that hit
  - Aligned with each 4H candle close (+ 5min buffer): full V22 detection
    across the watchlist

ccxt is synchronous, so all I/O-bound calls are wrapped in asyncio.to_thread.
The task is mounted onto the FastAPI app via the lifespan handler (main.py).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Any

from .config import (
    WATCHLIST,
    USE_S5,
    USE_BTC_FILTER_S5,
    TRAIL_ATR_MULT,
    TRAIL_TIGHTEN_R,
    TRAIL_ATR_TIGHT,
)
from .db import (
    insert_open_signal,
    close_signal,
    touch_signal,
    list_open_signals,
    write_scanner_state,
)
from .exchange import fetch_ohlcv
from .indicators import add_indicators
from .notify import notify_closed_signal, notify_new_signal
from .regime import get_btc_regime, get_daily_trend
from .s3 import check_s3_signal
from .s5 import check_s5_signal_at

log = logging.getLogger("v22.scanner")

# ── Run state ────────────────────────────────────────────────────────────────
_task: asyncio.Task | None = None
_running = False


# ────────────────────────────────────────────────────────────────────────────
# Scan: detect new signals (S3 + S5) across the watchlist
# ────────────────────────────────────────────────────────────────────────────

def _scan_symbol_sync(symbol: str, btc_regime: str) -> list[dict[str, Any]]:
    """Synchronous: run S3 + S5 detection for one symbol. Returns 0+ signals."""
    signals: list[dict[str, Any]] = []

    # ── S3 (pullback) ───────────────────────────────────────────────────────
    try:
        trend = get_daily_trend(symbol)
        if trend != "flat":
            s3 = check_s3_signal(symbol, trend=trend, btc_regime=btc_regime)
            if s3:
                signals.append(s3)
    except Exception as e:
        log.warning(f"[v22] S3 scan {symbol} failed: {e}")

    # ── S5 (break-retest-go) ────────────────────────────────────────────────
    if USE_S5:
        try:
            df = fetch_ohlcv(symbol, "4h", limit=400)
            if not df.empty and len(df) >= 50:
                df = add_indicators(df)
                if not df.empty:
                    last_idx = len(df) - 1
                    daily_trend_for_s5 = "flat"
                    if USE_BTC_FILTER_S5:
                        # S5 uses BTC regime as the daily trend proxy for filtering
                        daily_trend_for_s5 = (
                            "bull"
                            if btc_regime == "bull"
                            else "bear"
                            if btc_regime == "bear"
                            else "flat"
                        )
                    s5 = check_s5_signal_at(df, last_idx, daily_trend=daily_trend_for_s5)
                    if s5:
                        last_ts = df.iloc[last_idx]["timestamp"]
                        s5["symbol"] = symbol
                        s5["entry_time"] = (
                            last_ts.isoformat() if hasattr(last_ts, "isoformat") else str(last_ts)
                        )
                        signals.append(s5)
        except Exception as e:
            log.warning(f"[v22] S5 scan {symbol} failed: {e}")

    # Attach entry_time to S3 signals (it doesn't include one)
    now_iso = datetime.now(timezone.utc).isoformat()
    for s in signals:
        s.setdefault("entry_time", now_iso)

    return signals


async def run_full_scan() -> int:
    """Run S3+S5 detection across the entire watchlist. Returns # signals fired."""
    started = datetime.now(timezone.utc)
    log.info("[v22] starting full scan")

    try:
        btc_regime = await asyncio.to_thread(get_btc_regime)
    except Exception as e:
        log.warning(f"[v22] get_btc_regime failed, defaulting to 'bull': {e}")
        btc_regime = "bull"

    fired = 0
    for symbol in WATCHLIST:
        try:
            signals = await asyncio.to_thread(_scan_symbol_sync, symbol, btc_regime)
        except Exception as e:
            log.warning(f"[v22] scan failed for {symbol}: {e}")
            continue
        for sig in signals:
            inserted = await asyncio.to_thread(insert_open_signal, sig)
            if inserted:
                fired += 1
                # Fire-and-forget Telegram notification so HTTP latency doesn't
                # block the scan. The notifier handles eligibility + tier gate
                # internally and silently no-ops if TELEGRAM_BOT_TOKEN is unset.
                try:
                    asyncio.create_task(notify_new_signal({**sig, **inserted}))
                except Exception as e:
                    log.warning(f"[v22] could not schedule notify task: {e}")
        # Small inter-symbol pause so we don't hammer Binance even with rate limiting
        await asyncio.sleep(0.05)

    finished = datetime.now(timezone.utc)
    write_scanner_state(
        last_scan_at=finished,
        last_signal_at=finished if fired > 0 else None,
    )
    elapsed = (finished - started).total_seconds()
    log.info(f"[v22] full scan done in {elapsed:.1f}s — fired {fired} new signal(s)")
    return fired


# ────────────────────────────────────────────────────────────────────────────
# Exit check: scan open positions for TP/SL/trail hits
# ────────────────────────────────────────────────────────────────────────────

def _check_exit_sync(signal: dict[str, Any]) -> dict[str, Any] | None:
    """Synchronous: returns an exit dict if this open position hit something."""
    symbol = signal["symbol"]
    direction = signal["direction"]
    entry = float(signal["entry"])
    sl = float(signal["stop_loss"])
    tp1 = float(signal["tp1"])
    tp2 = float(signal.get("tp2") or 0.0) if signal.get("tp2") else None

    # Use the 1h timeframe — granular enough to catch intraday exits
    df = fetch_ohlcv(symbol, "1h", limit=50)
    if df.empty:
        return None
    last = df.iloc[-1]
    high = float(last["high"])
    low = float(last["low"])
    close = float(last["close"])
    ts_raw = last["timestamp"]
    last_ts: datetime = ts_raw.to_pydatetime() if hasattr(ts_raw, "to_pydatetime") else datetime.now(timezone.utc)

    is_long = direction == "long"
    risk_dist = abs(entry - sl)
    if risk_dist <= 0:
        return None

    # Hard SL hit?
    if (is_long and low <= sl) or (not is_long and high >= sl):
        ret_pct = ((sl - entry) / entry) * (1 if is_long else -1) * 100
        # Position size from signal (or fall back to constant risk model)
        risk_usd = float(signal.get("risk_usd") or 50.0)
        return {
            "exit_price": sl,
            "exit_time": last_ts,
            "exit_reason": "stop_loss",
            "outcome": "loss",
            "pnl": -risk_usd,
            "ret_pct": round(ret_pct, 2),
        }

    # TP2 hit?
    if tp2 is not None and (
        (is_long and high >= tp2) or (not is_long and low <= tp2)
    ):
        ret_pct = ((tp2 - entry) / entry) * (1 if is_long else -1) * 100
        rr_realized = abs(tp2 - entry) / risk_dist
        risk_usd = float(signal.get("risk_usd") or 50.0)
        return {
            "exit_price": tp2,
            "exit_time": last_ts,
            "exit_reason": "tp2",
            "outcome": "win",
            "pnl": round(risk_usd * rr_realized, 2),
            "ret_pct": round(ret_pct, 2),
        }

    # TP1 hit? — close the position (V22 trails after TP1 in the live engine,
    # but for simplicity in v1 we close at TP1; refine later with trail logic).
    if (is_long and high >= tp1) or (not is_long and low <= tp1):
        ret_pct = ((tp1 - entry) / entry) * (1 if is_long else -1) * 100
        rr_realized = abs(tp1 - entry) / risk_dist
        risk_usd = float(signal.get("risk_usd") or 50.0)
        return {
            "exit_price": tp1,
            "exit_time": last_ts,
            "exit_reason": "tp1",
            "outcome": "win",
            "pnl": round(risk_usd * rr_realized, 2),
            "ret_pct": round(ret_pct, 2),
        }

    # Trail exit: once price has moved TRAIL_TIGHTEN_R × risk distance in our
    # favour, place a tight trail at TRAIL_ATR_TIGHT × ATR.
    moved = (close - entry) * (1 if is_long else -1)
    if moved >= risk_dist * TRAIL_TIGHTEN_R:
        # Tight trail — give back at most 1× ATR of unrealized gain
        df_ind = add_indicators(fetch_ohlcv(symbol, "4h", limit=400))
        if not df_ind.empty:
            atr = float(df_ind.iloc[-1]["atr"])
            trail = close - atr * TRAIL_ATR_TIGHT if is_long else close + atr * TRAIL_ATR_TIGHT
            if (is_long and low <= trail) or (not is_long and high >= trail):
                ret_pct = ((trail - entry) / entry) * (1 if is_long else -1) * 100
                rr_realized = abs(trail - entry) / risk_dist
                risk_usd = float(signal.get("risk_usd") or 50.0)
                return {
                    "exit_price": round(trail, 6),
                    "exit_time": last_ts,
                    "exit_reason": "trail_stop",
                    "outcome": "win" if ret_pct > 0 else "loss",
                    "pnl": round(risk_usd * rr_realized * (1 if ret_pct > 0 else -1), 2),
                    "ret_pct": round(ret_pct, 2),
                }

    return None


async def run_exit_checks() -> int:
    """Walk every open signal; close any that hit TP/SL/trail. Returns # closed."""
    open_rows = list_open_signals()
    if not open_rows:
        write_scanner_state(last_exit_check=datetime.now(timezone.utc))
        return 0

    closed = 0
    for sig in open_rows:
        try:
            exit_data = await asyncio.to_thread(_check_exit_sync, sig)
        except Exception as e:
            log.warning(f"[v22] exit check failed for #{sig.get('id')}: {e}")
            continue
        if exit_data:
            did_close = await asyncio.to_thread(
                close_signal,
                sig["id"],
                **exit_data,
            )
            if did_close:
                closed += 1
                try:
                    asyncio.create_task(notify_closed_signal({**sig, **exit_data, "status": "closed"}))
                except Exception as e:
                    log.warning(f"[v22] could not schedule close notify task: {e}")
        else:
            await asyncio.to_thread(touch_signal, sig["id"])

    write_scanner_state(last_exit_check=datetime.now(timezone.utc))
    if closed:
        log.info(f"[v22] closed {closed} position(s) this exit-check pass")
    return closed


# ────────────────────────────────────────────────────────────────────────────
# Main scheduling loop
# ────────────────────────────────────────────────────────────────────────────

def _next_4h_boundary(now: datetime) -> datetime:
    """Next 00:00 / 04:00 / 08:00 / 12:00 / 16:00 / 20:00 UTC + 5min buffer."""
    base = now.replace(minute=0, second=0, microsecond=0)
    hour = ((base.hour // 4) + 1) * 4
    if hour >= 24:
        base = base + timedelta(days=1)
        hour -= 24
    return base.replace(hour=hour) + timedelta(minutes=5)


async def _loop() -> None:
    log.info("[v22] scanner loop starting")
    next_full_scan = _next_4h_boundary(datetime.now(timezone.utc))
    log.info(f"[v22] next full scan at {next_full_scan.isoformat()}")

    # Kick off one immediate scan + exit-check on startup so the live table
    # has fresh data on first deploy.
    try:
        await run_full_scan()
    except Exception as e:
        log.exception(f"[v22] initial scan errored: {e}")

    while _running:
        try:
            now = datetime.now(timezone.utc)
            # Hit a full scan when we've crossed the next 4H boundary
            if now >= next_full_scan:
                await run_full_scan()
                next_full_scan = _next_4h_boundary(now)
                log.info(f"[v22] next full scan at {next_full_scan.isoformat()}")
            # Always run an exit check
            await run_exit_checks()
        except Exception as e:
            log.exception(f"[v22] loop iteration errored: {e}")
        # Sleep 60s between iterations
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            break

    log.info("[v22] scanner loop exited")


# ────────────────────────────────────────────────────────────────────────────
# Lifecycle (called from FastAPI lifespan)
# ────────────────────────────────────────────────────────────────────────────

async def start() -> None:
    global _task, _running
    if _task and not _task.done():
        return
    _running = True
    _task = asyncio.create_task(_loop(), name="v22-scanner")
    log.info("[v22] scanner task started")


async def stop() -> None:
    global _task, _running
    _running = False
    if _task:
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):
            pass
        _task = None
    log.info("[v22] scanner task stopped")
