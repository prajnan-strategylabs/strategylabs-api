"""
User-strategy backtest engine ("rules_v2").

Design principles (these are load-bearing — the marketing promises them):
- Every reported number is computed from the actual trade sequence. Nothing is
  randomised except the Monte Carlo resampler, which is seeded and reported as such.
- No silent fallback: if the entry rule can't be compiled or never fires, the run
  is marked FAILED with an actionable message instead of inventing results.
- Costs are always on: taker fee + slippage per side, deducted from every trade.
- No look-ahead: signals are evaluated on bar close, fills happen at next bar open.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import re
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Optional
from uuid import UUID

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, status
from pydantic import BaseModel

from app.auth import CurrentUser
from app.db import get_db
from supabase import Client

log = logging.getLogger(__name__)

router = APIRouter(prefix="/backtests", tags=["backtests"])

# ── Engine constants ─────────────────────────────────────────────────────────
ENGINE_VERSION = "rules_v2"
START_EQUITY = 100.0            # equity is tracked in % terms; UI reads return = equity − 100
FEE_BPS_PER_SIDE = 10.0         # Binance spot taker 0.10%
SLIPPAGE_BPS_PER_SIDE = 5.0     # conservative fill assumption
RISK_PCT_PER_TRADE = 1.0        # risk-based sizing, same model as the v22 research engine
DEFAULT_SL_ATR_MULT = 1.5
DEFAULT_TP_R_MULT = 3.0
WARMUP_BARS = 250               # extra history so 200-period indicators are valid at start
MAX_BARS = 20_000               # 8y of 4H fits; beyond this we step down a timeframe
MONTE_CARLO_RUNS = 500
MONTE_CARLO_SEED = 42
EARLIEST_START = datetime(2017, 1, 1, tzinfo=timezone.utc)
TF_MINUTES = {"1d": 1440, "4h": 240, "1h": 60}


class BacktestRequest(BaseModel):
    strategy_id: UUID
    start_date: str  # "YYYY-MM-DD"
    end_date: str    # "YYYY-MM-DD"


class BacktestError(Exception):
    """Raised for honest, user-facing backtest failures."""


# ── Text → rules parsing helpers ─────────────────────────────────────────────

def _normalize_asset(spec: dict) -> str:
    asset_str = str(spec.get("asset", "BTC/USDT")).upper().strip().replace(" ", "")
    if "/" not in asset_str:
        for quote in ("USDT", "USDC", "BUSD"):
            if asset_str.endswith(quote) and len(asset_str) > len(quote):
                return f"{asset_str[:-len(quote)]}/{quote}"
        if asset_str.endswith("BTC") and len(asset_str) > 3:
            return f"{asset_str[:-3]}/BTC"
        return f"{asset_str}/USDT"
    return asset_str


def _normalize_timeframe(spec: dict) -> str:
    tf = str(spec.get("timeframe", "1d")).lower().strip()
    if tf in ("1d", "d", "daily", "1day"):
        return "1d"
    if tf in ("4h", "h4", "4hr", "4 hour", "4hour"):
        return "4h"
    if tf in ("1h", "h1", "hourly", "1 hour", "1hour"):
        return "1h"
    return "1d"


def _find_ma_refs(text: str) -> list[tuple[str, int]]:
    """Return (ma_type, length) pairs in order of appearance, e.g. 'EMA 9 crosses EMA(21)'."""
    return [(m.group(1).lower(), int(m.group(2)))
            for m in re.finditer(r"\b(ema|sma)\s*\(?\s*(\d+)\s*\)?", text, re.IGNORECASE)]


DAY_MINUTES = 1440
RETEST_WAIT_BARS = 15         # how long a breakout waits for its retest before expiring
RETEST_TOUCH_TOL = 1.002      # a pullback low within 0.2% of the level counts as a touch
MAX_BREAKOUT_LOOKBACK = 200   # channel lookback capped at the indicator warm-up budget


def _split_direction_sections(t: str) -> Optional[tuple[str, str]]:
    """Detect 'LONG: ... SHORT: ...' style dual-direction rules (either order)."""
    m_long = re.search(r"\blong\b[^:]{0,25}:", t)
    m_short = re.search(r"\bshort\b[^:]{0,25}:", t)
    if not (m_long and m_short) or m_long.start() == m_short.start():
        return None
    if m_long.start() < m_short.start():
        return t[m_long.end():m_short.start()], t[m_short.end():]
    return t[m_long.end():], t[m_short.end():m_long.start()]


def _parse_entry(entry_text: str, timeframe: str = "1d") -> dict:
    """
    Compile the entry rule text into one or two (LONG/SHORT) structured event
    triggers, each with its own AND-filters. Raises BacktestError when no
    testable trigger is found.
    """
    t = entry_text.lower()
    sections = _split_direction_sections(t)
    if sections:
        long_side = _parse_side(sections[0], timeframe, force_short=False)
        short_side = _parse_side(sections[1], timeframe, force_short=True)
        return {
            "type": "dual", "is_short": False,
            "sides": [long_side, short_side],
            "label": f"LONG: {_side_label(long_side)} | SHORT: {_side_label(short_side)}",
        }
    return _parse_side(t, timeframe, force_short=None)


def _side_label(side: dict) -> str:
    label = side["label"]
    if side.get("filters"):
        label += " (filters: " + ", ".join(f["label"] for f in side["filters"]) + ")"
    return label


def _parse_side(t: str, timeframe: str, force_short: Optional[bool]) -> dict:
    is_short = force_short if force_short is not None else bool(re.search(r"\b(short|go short|sell short)\b", t))
    tf_minutes = TF_MINUTES.get(timeframe, DAY_MINUTES)

    trigger: Optional[dict] = None
    used: set[str] = set()   # concepts consumed by the trigger, excluded from filters

    # ── Breakout above/below an N-day/bar high/low, optionally with retest ──
    m = re.search(r"break(?:s|out|ing|down)?[^.]*?(\d+)[\s-]*(day|week|bar|candle|period)s?\s*(high|low)", t) \
        or re.search(r"new\s*(\d+)[\s-]*(day|week|bar|candle|period)s?\s*(high|low)", t)
    if m:
        n, unit, band = int(m.group(1)), m.group(2), m.group(3)
        if unit == "day":
            lookback = max(2, n * DAY_MINUTES // tf_minutes)
        elif unit == "week":
            lookback = max(2, n * 7 * DAY_MINUTES // tf_minutes)
        else:
            lookback = max(2, n)
        lookback = min(lookback, MAX_BREAKOUT_LOOKBACK)
        retest = bool(re.search(r"\bretest", t))
        short_break = band == "low"
        trigger = {
            "type": "breakout", "lookback": lookback, "band": band, "retest": retest,
            "is_short": short_break or is_short,
            "label": f"breakout {'below' if band == 'low' else 'above'} the {n}-{unit} {band}"
                     + (f" + retest confirmation (touch within {RETEST_WAIT_BARS} bars, close back "
                        f"{'below' if band == 'low' else 'above'} the level)" if retest else ""),
        }
        used.add("breakout")

    # ── Price/close crossing a single MA ("close crosses above EMA 55") ──
    # Must outrank the two-MA crossover: a second MA in the sentence is usually
    # a regime filter ("...while price above EMA 200"), not the cross pair.
    if trigger is None:
        m = re.search(
            r"(?:close|price|candle)s?\s*cross(?:es|ing|ed)?\s*(above|over|below|under)\s*(?:the\s*)?(ema|sma)\s*\(?\s*(\d+)\s*\)?", t)
        if m:
            side_word = "above" if m.group(1) in ("above", "over") else "below"
            ma = f"{m.group(2)}_{int(m.group(3))}"
            trigger = {"type": "price_vs_ma", "ma": ma, "side": side_word, "is_short": is_short,
                       "label": f"close crossing {side_word} {ma.upper()}"}
            used.add("ma_trigger:" + ma)

    has_cross = bool(re.search(r"\bcross(es|ing|over|ed)?\b|golden cross|death cross", t))
    ma_refs = _find_ma_refs(t)

    # ── MACD / Stochastic crossovers ──
    if trigger is None and "macd" in t and (has_cross or "signal" in t):
        trigger = {"type": "macd_cross", "is_short": is_short, "label": "MACD line crossing its signal line"}
        used.add("macd")
    if trigger is None and "stoch" in t and has_cross:
        trigger = {"type": "stoch_cross", "is_short": is_short, "label": "Stochastic %K crossing %D"}
        used.add("stoch")

    # ── Moving-average crossover ("golden cross", "ema 9 crosses above ema 21") ──
    if trigger is None and has_cross and len(ma_refs) >= 2:
        fast = f"{ma_refs[0][0]}_{ma_refs[0][1]}"
        slow = f"{ma_refs[1][0]}_{ma_refs[1][1]}"
        trigger = {"type": "ma_cross", "fast": fast, "slow": slow, "is_short": is_short,
                   "label": f"{fast.upper()} crossing {'below' if is_short else 'above'} {slow.upper()}"}
        used.add("ma")
    if trigger is None and ("golden cross" in t or "death cross" in t):
        ma = "sma" if "sma" in t or "simple" in t else ("ema" if "ema" in t else "sma")
        short = "death cross" in t and "golden" not in t
        trigger = {"type": "ma_cross", "fast": f"{ma}_50", "slow": f"{ma}_200", "is_short": short or is_short,
                   "label": f"{ma.upper()} 50/200 {'death' if short else 'golden'} cross"}
        used.add("ma")

    # ── RSI threshold crossing ──
    if trigger is None:
        m = re.search(r"rsi[^<>]*?(?:(<|<=|below|dips below|under)\s*(\d+))", t)
        if m:
            trigger = {"type": "rsi_cross", "level": float(m.group(2)), "direction": "down", "is_short": is_short,
                       "label": f"RSI crossing below {float(m.group(2)):g}"}
            used.add("rsi")
        else:
            m = re.search(r"rsi[^<>]*?(?:(>|>=|above|rises above|over)\s*(\d+))", t)
            if m:
                trigger = {"type": "rsi_cross", "level": float(m.group(2)), "direction": "up", "is_short": is_short,
                           "label": f"RSI crossing above {float(m.group(2)):g}"}
                used.add("rsi")

    # ── Bollinger band touch ──
    if trigger is None and re.search(r"lower (bollinger )?band|bb[_ ]?low|bbl\b", t):
        trigger = {"type": "bb_touch", "band": "low", "is_short": is_short,
                   "label": "close touching the lower Bollinger band"}
        used.add("bb")
    if trigger is None and re.search(r"upper (bollinger )?band|bb[_ ]?upper|bbu\b", t):
        trigger = {"type": "bb_touch", "band": "upper", "is_short": is_short,
                   "label": "close touching the upper Bollinger band"}
        used.add("bb")

    # ── Price vs MA state transition (last resort, no 'cross' keyword) ──
    if trigger is None and ma_refs and re.search(r"\b(above|over)\b", t):
        ma = f"{ma_refs[0][0]}_{ma_refs[0][1]}"
        trigger = {"type": "price_vs_ma", "ma": ma, "side": "above", "is_short": is_short,
                   "label": f"close crossing above {ma.upper()}"}
        used.add("ma")
    if trigger is None and ma_refs and re.search(r"\b(below|under)\b", t):
        ma = f"{ma_refs[0][0]}_{ma_refs[0][1]}"
        trigger = {"type": "price_vs_ma", "ma": ma, "side": "below", "is_short": is_short,
                   "label": f"close crossing below {ma.upper()}"}
        used.add("ma")

    if trigger is None:
        raise BacktestError(
            "Couldn't compile the entry rule into a testable trigger. "
            "Try phrasing like 'buy on a breakout above the 20-day high', "
            "'buy when EMA 9 crosses above EMA 21', or 'buy when RSI drops below 30'."
        )

    trigger["filters"] = _parse_filters(t, trigger, used)
    return trigger


def _parse_filters(t: str, trigger: dict, used: set[str]) -> list[dict]:
    """State conditions AND-ed onto the trigger (evaluated on the signal bar)."""
    filters: list[dict] = []

    # Trend regime ("in an uptrend", "trend.daily == 'up'", "bullish trend")
    if re.search(r"\buptrend\b|\bbullish\b|trend\.?\w*\s*(?:is|==?)\s*'?up\b|trend\s+(?:is\s+)?up\b", t):
        filters.append({"type": "price_above_ma", "ma": "sma_200", "label": "uptrend (close above SMA 200)"})
    elif re.search(r"\bdowntrend\b|\bbearish\b|trend\.?\w*\s*(?:is|==?)\s*'?down\b|trend\s+(?:is\s+)?down\b", t):
        filters.append({"type": "price_below_ma", "ma": "sma_200", "label": "downtrend (close below SMA 200)"})

    # Explicit "price/close above|below EMA/SMA N"
    for m in re.finditer(
            r"(?:price|close(?:s)?|trading|it)\s*(?:is|stays|remains)?\s*(above|over|below|under)\s*(?:the\s*)?(ema|sma)\s*\(?\s*(\d+)\s*\)?", t):
        direction, ma_type, ln = m.group(1), m.group(2), int(m.group(3))
        col = f"{ma_type}_{ln}"
        if trigger.get("type") == "price_vs_ma" and trigger.get("ma") == col:
            continue  # already the trigger itself
        ftype = "price_above_ma" if direction in ("above", "over") else "price_below_ma"
        if not any(f.get("ma") == col and f["type"] == ftype for f in filters):
            filters.append({"type": ftype, "ma": col, "label": f"close {direction} {ma_type.upper()} {ln}"})

    # "above the 200-day moving average"
    m = re.search(r"(above|over|below|under)\s*(?:the\s*)?(\d+)[\s-]*day\s*(?:simple\s*|exponential\s*)?(?:ma\b|sma\b|ema\b|moving average)", t)
    if m:
        direction, ln = m.group(1), int(m.group(2))
        col = f"ema_{ln}" if "exponential" in t or re.search(rf"{ln}[\s-]*day\s*ema", t) else f"sma_{ln}"
        ftype = "price_above_ma" if direction in ("above", "over") else "price_below_ma"
        if not any(f.get("ma") == col and f["type"] == ftype for f in filters):
            filters.append({"type": ftype, "ma": col, "label": f"close {direction} the {ln}-day MA"})

    # RSI as a filter when it isn't the trigger
    if "rsi" not in used:
        m = re.search(r"rsi[^<>]*?(?:>|>=|above|over)\s*(\d+)", t)
        if m:
            filters.append({"type": "rsi_above", "level": float(m.group(1)), "label": f"RSI above {m.group(1)}"})
        m = re.search(r"rsi[^<>]*?(?:<|<=|below|under)\s*(\d+)", t)
        if m:
            filters.append({"type": "rsi_below", "level": float(m.group(1)), "label": f"RSI below {m.group(1)}"})

    # Volume confirmation
    if re.search(r"volume\s*(?:is\s*)?(?:above|over|greater|spike|surge|elevated|high(?:er)?)", t):
        filters.append({"type": "volume_above_avg", "label": "volume above its 20-bar average"})

    return filters


def _parse_exit(exit_text: str, entry: dict) -> dict:
    """Optional rule-based exits layered on top of the stop/target."""
    t = exit_text.lower()
    out: dict[str, Any] = {}

    m = re.search(r"rsi[^<>]*?(?:>|>=|above|rises above|over)\s*(\d+)", t)
    if m:
        out["rsi_above"] = float(m.group(1))
    m = re.search(r"rsi[^<>]*?(?:<|<=|below|dips below|under)\s*(\d+)", t)
    if m:
        out["rsi_below"] = float(m.group(1))

    ma_refs = _find_ma_refs(t)
    if ma_refs and re.search(r"\b(below|under|cross(es|ing)? below)\b", t):
        out["close_below_ma"] = f"{ma_refs[0][0]}_{ma_refs[0][1]}"
    elif ma_refs and re.search(r"\b(above|over|cross(es|ing)? above)\b", t):
        out["close_above_ma"] = f"{ma_refs[0][0]}_{ma_refs[0][1]}"

    if re.search(r"\b(opposite|reverse|cross(es|ing)? back)\b", t):
        out["opposite_cross"] = True  # applied at runtime for MA-cross / price-vs-MA triggers

    m = re.search(r"(?:after|within|max(?:imum)?)\s*(\d+)\s*(bar|bars|candle|candles|day|days)", t)
    if m:
        out["max_bars_held"] = int(m.group(1))

    return out


def _parse_stop(sl_text: str, rules_text: str, entry: Optional[dict] = None) -> dict:
    """
    Stop distance: 'n * ATR', 'n %', or 'the retest low' (breakout+retest only).
    Any mention of 'trailing' upgrades the ATR stop to a ratcheting trail.
    Falls back to a stated default.
    """
    combined = f"{sl_text} {rules_text}".lower()
    trailing = bool(re.search(r"\btrail", combined))
    trail_mult = DEFAULT_SL_ATR_MULT
    m = re.search(r"(\d+(?:\.\d+)?)\s*[*x]?\s*atr", combined)
    if m:
        trail_mult = float(m.group(1))

    stop: dict[str, Any]
    if entry and entry.get("type") == "breakout" and entry.get("retest") and re.search(r"retest\s*(?:bar|candle)?'?s?\s*low", combined):
        stop = {"mode": "retest_low", "mult": trail_mult, "label": "the retest bar's low"}
    else:
        found = False
        for source in (sl_text, rules_text):
            t = source.lower()
            m = re.search(r"(\d+(?:\.\d+)?)\s*[*x]?\s*atr", t)
            if m:
                stop = {"mode": "atr", "mult": float(m.group(1)), "label": f"{float(m.group(1)):g} × ATR(14)"}
                found = True
                break
            m = re.search(r"(\d+(?:\.\d+)?)\s*%", t)
            if m:
                stop = {"mode": "pct", "pct": float(m.group(1)), "label": f"{float(m.group(1)):g}% fixed"}
                found = True
                break
        if not found:
            stop = {"mode": "atr", "mult": DEFAULT_SL_ATR_MULT,
                    "label": f"{DEFAULT_SL_ATR_MULT:g} × ATR(14) (default — no stop rule found)"}

    stop["trailing"] = trailing
    stop["trail_mult"] = trail_mult
    if trailing:
        stop["label"] += f", trailing at {trail_mult:g} × ATR(14)"
    return stop


def _parse_target(tp_text: str) -> dict:
    """Target: 'nR' or 'n %'. Falls back to a stated default."""
    t = tp_text.lower()
    m = re.search(r"(\d+(?:\.\d+)?)\s*[*x]?\s*r\b", t)
    if m:
        return {"mode": "r", "mult": float(m.group(1)), "label": f"{float(m.group(1)):g}R"}
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", t)
    if m:
        return {"mode": "pct", "pct": float(m.group(1)), "label": f"{float(m.group(1)):g}% fixed"}
    return {"mode": "r", "mult": DEFAULT_TP_R_MULT,
            "label": f"{DEFAULT_TP_R_MULT:g}R (default — no target rule found)"}


# ── Indicators ───────────────────────────────────────────────────────────────

def _compute_indicators(df: pd.DataFrame, spec: dict, source_prompt: str) -> pd.DataFrame:
    import pandas_ta as ta

    df = df.copy()
    df["sma_50"] = ta.sma(df["close"], length=50)
    df["sma_200"] = ta.sma(df["close"], length=200)
    df["ema_21"] = ta.ema(df["close"], length=21)
    df["ema_50"] = ta.ema(df["close"], length=50)
    df["ema_200"] = ta.ema(df["close"], length=200)
    df["rsi"] = ta.rsi(df["close"], length=14)
    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)
    df["vol_sma20"] = ta.sma(df["volume"], length=20)

    bb = ta.bbands(df["close"], length=20, std=2.0)
    if bb is not None and not bb.empty:
        bbl = next((c for c in bb.columns if c.startswith("BBL_")), None)
        bbm = next((c for c in bb.columns if c.startswith("BBM_")), None)
        bbu = next((c for c in bb.columns if c.startswith("BBU_")), None)
        if bbl and bbm and bbu:
            df["bb_low"] = bb[bbl]
            df["bb_mid"] = bb[bbm]
            df["bb_upper"] = bb[bbu]

    macd = ta.macd(df["close"])
    if macd is not None and not macd.empty:
        for col in macd.columns:
            cl = col.lower()
            if cl.startswith("macd_"):
                df["macd_line"] = macd[col]
            elif cl.startswith("macds_"):
                df["macd_signal"] = macd[col]
    stoch = ta.stoch(df["high"], df["low"], df["close"])
    if stoch is not None and not stoch.empty:
        for col in stoch.columns:
            cl = col.lower()
            if cl.startswith("stochk_"):
                df["stoch_k"] = stoch[col]
            elif cl.startswith("stochd_"):
                df["stoch_d"] = stoch[col]

    # Any explicitly-referenced MA lengths not covered by the defaults
    rules_text = " ".join(str(spec.get(k, "") or "") for k in ("entry", "exit", "stop_loss", "target"))
    for ma_type, length in _find_ma_refs(f"{rules_text} {source_prompt}"):
        col = f"{ma_type}_{length}"
        if col not in df.columns:
            df[col] = ta.sma(df["close"], length=length) if ma_type == "sma" else ta.ema(df["close"], length=length)

    return df


def _ensure_rule_columns(df: pd.DataFrame, entry: dict, exits: dict) -> pd.DataFrame:
    """Compute any columns the compiled rules reference that the defaults missed."""
    import pandas_ta as ta

    needed: list[str] = []
    sides = entry["sides"] if entry["type"] == "dual" else [entry]
    for side in sides:
        if side["type"] == "ma_cross":
            needed += [side["fast"], side["slow"]]
        if side["type"] == "price_vs_ma":
            needed.append(side["ma"])
        for f in side.get("filters", []):
            if "ma" in f:
                needed.append(f["ma"])
    for key in ("close_below_ma", "close_above_ma"):
        if key in exits:
            needed.append(exits[key])

    for col in needed:
        if col not in df.columns:
            ma_type, ln = col.split("_")
            fn = ta.sma if ma_type == "sma" else ta.ema
            df[col] = fn(df["close"], length=int(ln))

    for side in sides:
        if side["type"] == "breakout":
            n = side["lookback"]
            # channel of the PRIOR n bars — shift(1) keeps the current bar out of its own level
            df[f"hh_{n}"] = df["high"].rolling(n).max().shift(1)
            df[f"ll_{n}"] = df["low"].rolling(n).min().shift(1)

    return df


# ── Core simulation ──────────────────────────────────────────────────────────

def _isnan(v) -> bool:
    try:
        return v is None or math.isnan(float(v))
    except (TypeError, ValueError):
        return True


def _filters_pass(filters: list[dict], row: pd.Series) -> bool:
    """All AND-filters must hold on the signal bar."""
    for f in filters:
        ftype = f["type"]
        if ftype in ("price_above_ma", "price_below_ma"):
            vals = (row.get("close"), row.get(f["ma"]))
            if any(_isnan(v) for v in vals):
                return False
            c, ma = map(float, vals)
            if ftype == "price_above_ma" and not c > ma:
                return False
            if ftype == "price_below_ma" and not c < ma:
                return False
        elif ftype in ("rsi_above", "rsi_below"):
            if _isnan(row.get("rsi")):
                return False
            r = float(row["rsi"])
            if ftype == "rsi_above" and not r > f["level"]:
                return False
            if ftype == "rsi_below" and not r < f["level"]:
                return False
        elif ftype == "volume_above_avg":
            vals = (row.get("volume"), row.get("vol_sma20"))
            if any(_isnan(v) for v in vals):
                return False
            if not float(row["volume"]) > float(row["vol_sma20"]):
                return False
    return True


def _entry_signal(entry: dict, row: pd.Series, prev: pd.Series) -> bool:
    etype = entry["type"]
    if etype == "breakout":
        col = f"hh_{entry['lookback']}" if entry["band"] == "high" else f"ll_{entry['lookback']}"
        vals = (row.get("close"), row.get(col), prev.get("close"), prev.get(col))
        if any(_isnan(v) for v in vals):
            return False
        c, lvl, pc, plvl = map(float, vals)
        if entry["band"] == "high":
            return c > lvl and pc <= plvl
        return c < lvl and pc >= plvl
    if etype == "ma_cross":
        vals = (row.get(entry["fast"]), row.get(entry["slow"]), prev.get(entry["fast"]), prev.get(entry["slow"]))
        if any(_isnan(v) for v in vals):
            return False
        f, s, pf, ps = map(float, vals)
        return (f < s and pf >= ps) if entry["is_short"] else (f > s and pf <= ps)
    if etype == "macd_cross":
        vals = (row.get("macd_line"), row.get("macd_signal"), prev.get("macd_line"), prev.get("macd_signal"))
        if any(_isnan(v) for v in vals):
            return False
        m, sig, pm, psig = map(float, vals)
        return (m < sig and pm >= psig) if entry["is_short"] else (m > sig and pm <= psig)
    if etype == "stoch_cross":
        vals = (row.get("stoch_k"), row.get("stoch_d"), prev.get("stoch_k"), prev.get("stoch_d"))
        if any(_isnan(v) for v in vals):
            return False
        k, d, pk, pd_ = map(float, vals)
        return (k < d and pk >= pd_) if entry["is_short"] else (k > d and pk <= pd_)
    if etype == "rsi_cross":
        vals = (row.get("rsi"), prev.get("rsi"))
        if any(_isnan(v) for v in vals):
            return False
        r, pr = map(float, vals)
        lvl = entry["level"]
        return (r < lvl and pr >= lvl) if entry["direction"] == "down" else (r > lvl and pr <= lvl)
    if etype == "bb_touch":
        band = "bb_low" if entry["band"] == "low" else "bb_upper"
        vals = (row.get("close"), row.get(band))
        if any(_isnan(v) for v in vals):
            return False
        c, b = map(float, vals)
        return c >= b if entry["band"] == "upper" else c <= b
    if etype == "price_vs_ma":
        vals = (row.get("close"), row.get(entry["ma"]), prev.get("close"), prev.get(entry["ma"]))
        if any(_isnan(v) for v in vals):
            return False
        c, ma, pc, pma = map(float, vals)
        return (c > ma and pc <= pma) if entry["side"] == "above" else (c < ma and pc >= pma)
    return False


def _rule_exit_signal(exits: dict, entry: dict, row: pd.Series, prev: pd.Series) -> Optional[str]:
    if "rsi_above" in exits and not _isnan(row.get("rsi")) and float(row["rsi"]) > exits["rsi_above"]:
        return f"rsi>{exits['rsi_above']:g}"
    if "rsi_below" in exits and not _isnan(row.get("rsi")) and float(row["rsi"]) < exits["rsi_below"]:
        return f"rsi<{exits['rsi_below']:g}"
    if "close_below_ma" in exits:
        ma = exits["close_below_ma"]
        if not _isnan(row.get(ma)) and float(row["close"]) < float(row[ma]):
            return f"close<{ma}"
    if "close_above_ma" in exits:
        ma = exits["close_above_ma"]
        if not _isnan(row.get(ma)) and float(row["close"]) > float(row[ma]):
            return f"close>{ma}"
    if exits.get("opposite_cross"):
        if entry.get("type") == "ma_cross":
            flipped = dict(entry, is_short=not entry["is_short"])
            if _entry_signal(flipped, row, prev):
                return "opposite_cross"
        elif entry.get("type") == "price_vs_ma":
            flipped = dict(entry, side=("below" if entry["side"] == "above" else "above"))
            if _entry_signal(flipped, row, prev):
                return "opposite_cross"
    return None


def _max_drawdown_pct(equity_points: list[float]) -> float:
    peak = -float("inf")
    max_dd = 0.0
    for eq in equity_points:
        peak = max(peak, eq)
        if peak > 0:
            max_dd = min(max_dd, (eq - peak) / peak * 100.0)
    return round(abs(max_dd), 2)


def _simulate(df: pd.DataFrame, entry: dict, exits: dict, stop: dict, target: dict,
              start_ts: pd.Timestamp) -> tuple[list[dict], list[list[float]]]:
    """
    Bar-by-bar simulation. Signals evaluate on bar close; fills at next bar open.
    Returns (trades, equity_curve). Trades carry internal fields (_gross_pct,
    _pos_frac, _f) used by the stats layer and stripped before persistence.
    """
    cost_pct = 2 * (FEE_BPS_PER_SIDE + SLIPPAGE_BPS_PER_SIDE) / 100.0  # round-trip, in %
    equity = START_EQUITY
    trades: list[dict] = []
    equity_curve: list[list[float]] = [[int(start_ts.timestamp() * 1000), START_EQUITY]]

    sides = entry["sides"] if entry.get("type") == "dual" else [entry]
    active = sides[0]                # rule side that opened the current position
    pending: Optional[dict] = None   # armed breakout awaiting its retest

    in_pos = False
    side = "LONG"
    entry_price = stop_price = target_price = sl_dist_pct = 0.0
    entry_ts_ms = 0
    entry_date_str = ""
    bars_held = 0

    start_idx = int(df["timestamp"].searchsorted(start_ts))
    n = len(df)

    for i in range(max(start_idx, 1), n):
        row, prev = df.iloc[i], df.iloc[i - 1]

        if in_pos:
            bars_held += 1
            o, h, l, c = (float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]))
            exit_price = None
            exit_reason = ""
            if side == "LONG":
                if o <= stop_price:
                    exit_price, exit_reason = o, "gap_stop"
                elif l <= stop_price:
                    exit_price, exit_reason = stop_price, "stop_loss"
                elif o >= target_price:
                    exit_price, exit_reason = o, "gap_target"
                elif h >= target_price:
                    exit_price, exit_reason = target_price, "take_profit"
            else:
                if o >= stop_price:
                    exit_price, exit_reason = o, "gap_stop"
                elif h >= stop_price:
                    exit_price, exit_reason = stop_price, "stop_loss"
                elif o <= target_price:
                    exit_price, exit_reason = o, "gap_target"
                elif l <= target_price:
                    exit_price, exit_reason = target_price, "take_profit"

            if exit_price is None:
                reason = _rule_exit_signal(exits, active, row, prev)
                if reason:
                    exit_price, exit_reason = c, reason
                elif exits.get("max_bars_held") and bars_held >= exits["max_bars_held"]:
                    exit_price, exit_reason = c, "timeout"
                elif i == n - 1:
                    exit_price, exit_reason = c, "end_of_data"

            if exit_price is None and stop.get("trailing"):
                # Ratchet the trail on the bar close — never loosens, only tightens
                atr_now = float(row["atr"]) if not _isnan(row.get("atr")) and float(row["atr"]) > 0 else None
                if atr_now:
                    if side == "LONG":
                        stop_price = max(stop_price, c - stop["trail_mult"] * atr_now)
                    else:
                        stop_price = min(stop_price, c + stop["trail_mult"] * atr_now)

            if exit_price is not None:
                if stop.get("trailing") and exit_reason in ("stop_loss", "gap_stop"):
                    exit_reason = "trail_stop"
                gross_pct = ((exit_price - entry_price) / entry_price * 100.0) * (1 if side == "LONG" else -1)
                net_pct = gross_pct - cost_pct
                # risk-based sizing: risk RISK_PCT of equity, position capped at equity (spot)
                pos_frac = min(RISK_PCT_PER_TRADE / sl_dist_pct, 1.0) if sl_dist_pct > 0 else 0.25
                equity_before = equity
                pnl = equity_before * pos_frac * net_pct / 100.0
                equity = max(equity + pnl, 0.01)
                r_actual = net_pct / sl_dist_pct if sl_dist_pct > 0 else 0.0
                f = pnl / equity_before if equity_before > 0 else 0.0

                ts_ms = int(row["timestamp"].timestamp() * 1000)
                equity_curve.append([ts_ms, round(equity, 2)])
                sign = "+" if r_actual >= 0 else "−"
                trades.append({
                    "date": row["timestamp"].strftime("%Y-%m-%d"),
                    "side": side,
                    "entry": round(entry_price, 6),
                    "exit": round(exit_price, 6),
                    "r": f"{sign}{abs(r_actual):.1f}R",
                    "pos": net_pct > 0,
                    "pnl_pct": round(net_pct, 2),
                    "exit_reason": exit_reason,
                    "entry_date": entry_date_str,
                    "entry_ts": entry_ts_ms,
                    "exit_ts": ts_ms,
                    "stop": round(stop_price, 6),
                    "target": round(target_price, 6),
                    "_gross_pct": gross_pct,
                    "_pos_frac": pos_frac,
                    "_f": f,
                })
                in_pos = False
            continue

        # Flat: look for an entry signal on this close, fill at next open
        if i >= n - 1:
            break

        signal_side: Optional[dict] = None
        retest_extreme: Optional[float] = None

        # Stage 2 of an armed breakout: price pulling back to touch the broken
        # level and closing back on the breakout side confirms the entry.
        if pending is not None:
            s = pending["side"]
            lvl = pending["level"]
            c_now = float(row["close"])
            if s["band"] == "high":
                touched = float(row["low"]) <= lvl * RETEST_TOUCH_TOL and c_now >= lvl
                invalidated = c_now < lvl - pending["atr"]
                extreme = float(row["low"])
            else:
                touched = float(row["high"]) >= lvl / RETEST_TOUCH_TOL and c_now <= lvl
                invalidated = c_now > lvl + pending["atr"]
                extreme = float(row["high"])
            if touched:
                signal_side = s
                retest_extreme = extreme
                pending = None
            elif invalidated or i >= pending["deadline"]:
                pending = None

        # Fresh signals — first side (LONG before SHORT for dual rules) wins the bar
        if signal_side is None:
            for s in sides:
                if not (_entry_signal(s, row, prev) and _filters_pass(s.get("filters", []), row)):
                    continue
                if s["type"] == "breakout" and s.get("retest"):
                    if pending is None:
                        col = f"hh_{s['lookback']}" if s["band"] == "high" else f"ll_{s['lookback']}"
                        lvl = float(row[col])
                        atr_now = float(row["atr"]) if not _isnan(row.get("atr")) and float(row["atr"]) > 0 else lvl * 0.02
                        pending = {"side": s, "level": lvl, "deadline": i + RETEST_WAIT_BARS, "atr": atr_now}
                    continue  # armed, not an entry yet
                signal_side = s
                break

        if signal_side is not None:
            s = signal_side
            pending = None
            nxt = df.iloc[i + 1]
            fill = float(nxt["open"])
            atr_val = float(row["atr"]) if not _isnan(row.get("atr")) and float(row["atr"]) > 0 else fill * 0.02

            if stop["mode"] == "retest_low" and retest_extreme is not None:
                sl_dist = (fill - retest_extreme) if not s["is_short"] else (retest_extreme - fill)
                if sl_dist <= 0:  # retest bar's extreme is on the wrong side of the fill — fall back
                    sl_dist = stop["mult"] * atr_val
            elif stop["mode"] == "atr" or stop["mode"] == "retest_low":
                sl_dist = stop["mult"] * atr_val
            else:
                sl_dist = fill * stop["pct"] / 100.0
            sl_dist_pct = sl_dist / fill * 100.0

            if target["mode"] == "r":
                tp_dist = target["mult"] * sl_dist
            else:
                tp_dist = fill * target["pct"] / 100.0

            active = s
            side = "SHORT" if s["is_short"] else "LONG"
            entry_price = fill
            entry_ts_ms = int(nxt["timestamp"].timestamp() * 1000)
            entry_date_str = nxt["timestamp"].strftime("%Y-%m-%d")
            if side == "LONG":
                stop_price, target_price = fill - sl_dist, fill + tp_dist
            else:
                stop_price, target_price = fill + sl_dist, fill - tp_dist
            in_pos = True
            bars_held = 0

    return trades, equity_curve


# ── Stats (every number computed, none invented) ─────────────────────────────

def _build_stats(trades: list[dict], equity_curve: list[list[float]],
                 start_dt: datetime, end_dt: datetime) -> dict:
    n = len(trades)
    wins = sum(1 for t in trades if t["pos"])
    final_equity = equity_curve[-1][1]
    total_return_pct = round(final_equity - START_EQUITY, 1)

    equity_values = [p[1] for p in equity_curve]
    max_dd = _max_drawdown_pct(equity_values)

    # Trade-based Sharpe, annualised by observed trade frequency
    fs = [t["_f"] for t in trades]
    years = max((end_dt - start_dt).days / 365.25, 1 / 365.25)
    sharpe = 0.0
    if n >= 2:
        mean_f = sum(fs) / n
        var = sum((x - mean_f) ** 2 for x in fs) / (n - 1)
        std = math.sqrt(var)
        if std > 0:
            sharpe = round(mean_f / std * math.sqrt(n / years), 2)

    pnl_dollars = [(equity_curve[i + 1][1] - equity_curve[i][1]) for i in range(len(equity_curve) - 1)]
    gross_profit = sum(p for p in pnl_dollars if p > 0)
    gross_loss = abs(sum(p for p in pnl_dollars if p < 0))
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else 9.99

    # Yearly breakdown from the actual equity path
    yearly = []
    by_year: dict[int, list[int]] = {}
    for idx, t in enumerate(trades):
        by_year.setdefault(int(t["date"][:4]), []).append(idx)
    running_equity = START_EQUITY
    for yr in sorted(by_year):
        idxs = by_year[yr]
        yr_start_eq = running_equity
        yr_points = [yr_start_eq] + [equity_curve[i + 1][1] for i in idxs]
        yr_wins = sum(1 for i in idxs if trades[i]["pos"])
        running_equity = yr_points[-1]
        yearly.append({
            "year": yr,
            "trades_count": len(idxs),
            "return_pct": round((yr_points[-1] / yr_start_eq - 1) * 100.0, 1),
            "drawdown_pct": _max_drawdown_pct(yr_points),
            "win_rate_pct": round(yr_wins / len(idxs) * 100.0, 1),
        })

    # Monte Carlo: reshuffle the observed trade returns, measure drawdown dispersion
    rng = random.Random(MONTE_CARLO_SEED)
    mc_dds = []
    if n >= 5:
        for _ in range(MONTE_CARLO_RUNS):
            seq = fs[:]
            rng.shuffle(seq)
            eq = START_EQUITY
            points = [eq]
            for f in seq:
                eq = max(eq * (1 + f), 0.01)
                points.append(eq)
            mc_dds.append(_max_drawdown_pct(points))
        mc_dds.sort()
    monte_carlo = {
        "runs": len(mc_dds) and MONTE_CARLO_RUNS,
        "median_max_dd_pct": mc_dds[len(mc_dds) // 2] if mc_dds else None,
        "p95_max_dd_pct": mc_dds[int(len(mc_dds) * 0.95)] if mc_dds else None,
        "note": "Trade order reshuffled; measures how much of the drawdown figure is sequence luck.",
    }

    # Cost stress: double fees + slippage, replay the same trades
    stressed_cost_pct = 2 * 2 * (FEE_BPS_PER_SIDE + SLIPPAGE_BPS_PER_SIDE) / 100.0
    eq = START_EQUITY
    for t in trades:
        net = t["_gross_pct"] - stressed_cost_pct
        eq = max(eq + eq * t["_pos_frac"] * net / 100.0, 0.01)
    stress = {
        "assumption": "fees + slippage doubled (0.60% round trip)",
        "stressed_return_pct": round(eq - START_EQUITY, 1),
    }

    trades_out = [{k: v for k, v in t.items() if not k.startswith("_")} for t in trades]
    trades_out.reverse()   # UI expects newest first
    yearly.reverse()

    return {
        "win_rate_pct": round(wins / n * 100.0, 1),
        "sharpe_ratio": sharpe,
        "max_drawdown_pct": max_dd,
        "profit_factor": profit_factor,
        "trade_count": n,
        "total_return_pct": total_return_pct,
        "equity_curve": equity_curve,
        "trades": trades_out,
        "yearly_breakdown": yearly,
        "monte_carlo": monte_carlo,
        "cost_stress": stress,
    }


# ── Background task ──────────────────────────────────────────────────────────

def _run_backtest_sync(spec: dict, source_prompt: str, start_date: str, end_date: str) -> dict:
    """Pure computation — raises BacktestError with a user-facing message on failure."""
    from app.v22.exchange import fetch_ohlcv_range

    # 1. Window
    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise BacktestError("Invalid date range — expected YYYY-MM-DD.")
    now = datetime.now(timezone.utc)
    start_dt = max(start_dt, EARLIEST_START)
    end_dt = min(end_dt, now)
    if start_dt >= end_dt:
        raise BacktestError("Start date must be before end date.")

    # 2. Timeframe — step down (1h → 4h → 1d) only if the window exceeds the bar budget
    timeframe = _normalize_timeframe(spec)
    range_minutes = (end_dt - start_dt).total_seconds() / 60
    for tf in (timeframe, "4h", "1d"):
        if TF_MINUTES[tf] >= TF_MINUTES[timeframe] and range_minutes / TF_MINUTES[tf] <= MAX_BARS:
            timeframe = tf
            break
    else:
        timeframe = "1d"
    tf_minutes = TF_MINUTES[timeframe]

    # 3. Rules
    entry = _parse_entry(str(spec.get("entry", "") or ""), timeframe)
    exits = _parse_exit(str(spec.get("exit", "") or ""), entry)
    stop = _parse_stop(str(spec.get("stop_loss", "") or ""),
                       " ".join(str(spec.get(k, "") or "") for k in ("entry", "exit", "target")),
                       entry)
    target = _parse_target(str(spec.get("target", "") or ""))

    # 4. Data (with indicator warm-up)
    asset = _normalize_asset(spec)
    since_ms = int((start_dt - timedelta(minutes=WARMUP_BARS * tf_minutes)).timestamp() * 1000)
    until_ms = int(end_dt.timestamp() * 1000)
    df = fetch_ohlcv_range(asset, timeframe, since_ms, until_ms)
    if df.empty or len(df) < 60:
        raise BacktestError(f"Not enough historical data for {asset} on {timeframe} in the requested window.")

    df = _compute_indicators(df, spec, source_prompt or "")
    df = _ensure_rule_columns(df, entry, exits)

    entry_label = entry["label"]
    if entry.get("filters"):
        entry_label += " — filters: " + ", ".join(f["label"] for f in entry["filters"])

    # 5. Simulate
    start_ts = pd.Timestamp(start_dt)
    trades, equity_curve = _simulate(df, entry, exits, stop, target, start_ts)
    if not trades:
        data_start = df["timestamp"].iloc[0].date()
        raise BacktestError(
            f"No trades triggered: '{entry_label}' never fired on {asset} ({timeframe}) "
            f"between {max(start_dt.date(), data_start)} and {end_dt.date()}. "
            "Loosen the entry rule or widen the date range."
        )

    # 6. Stats
    stats = _build_stats(trades, equity_curve, start_dt, end_dt)
    stats["engine"] = ENGINE_VERSION
    stats["params_used"] = {
        "asset": asset,
        "timeframe": timeframe,
        "entry": entry_label,
        "direction": "LONG + SHORT" if entry.get("type") == "dual" else ("SHORT" if entry["is_short"] else "LONG"),
        "stop_loss": stop["label"],
        "target": target["label"],
        "rule_exits": {k: v for k, v in exits.items()} or None,
        "sizing": f"{RISK_PCT_PER_TRADE:g}% equity risk per trade, position capped at 100% (spot, no leverage)",
        "costs": f"{FEE_BPS_PER_SIDE:g} bps fee + {SLIPPAGE_BPS_PER_SIDE:g} bps slippage per side",
        "fills": "signal on bar close, fill at next bar open; stop checked before target within a bar",
        "data_start": str(df["timestamp"].iloc[0].date()),
        "data_end": str(df["timestamp"].iloc[-1].date()),
        "bars": len(df),
    }
    return stats


CHART_PAD_BARS = 12       # context candles on each side of the trade
CHART_MAX_CANDLES = 400   # stride-sample beyond this to keep the payload light


def _trade_chart_payload(stats: dict, spec: dict, trade_index: int) -> dict:
    """Build the OHLC context payload for one backtested trade. Raises BacktestError."""
    from app.v22.exchange import fetch_ohlcv_range

    trades = stats.get("trades") or []
    if not (0 <= trade_index < len(trades)):
        raise BacktestError("Trade not found in this backtest run.")
    trade = trades[trade_index]

    params = stats.get("params_used") or {}
    asset = params.get("asset") or _normalize_asset(spec or {})
    timeframe = params.get("timeframe") or "1d"
    tf_minutes = TF_MINUTES.get(timeframe, 1440)
    tf_ms = tf_minutes * 60_000

    exit_ts = trade.get("exit_ts")
    if not exit_ts:
        exit_dt = datetime.strptime(trade["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        exit_ts = int(exit_dt.timestamp() * 1000)
    entry_ts = trade.get("entry_ts")
    approx_entry = not bool(entry_ts)
    if not entry_ts:
        # Legacy runs didn't record the entry bar — assume a ~20-bar hold for framing.
        entry_ts = exit_ts - 20 * tf_ms

    since_ms = entry_ts - CHART_PAD_BARS * tf_ms
    until_ms = min(exit_ts + CHART_PAD_BARS * tf_ms, int(datetime.now(timezone.utc).timestamp() * 1000))
    df = fetch_ohlcv_range(asset, timeframe, since_ms, until_ms)
    if df.empty:
        raise BacktestError(f"No historical candles available for {asset} around this trade.")

    ts_ms = (df["timestamp"].astype("int64") // 10**6).tolist()
    candles = [
        [ts_ms[i], float(df["open"].iloc[i]), float(df["high"].iloc[i]),
         float(df["low"].iloc[i]), float(df["close"].iloc[i])]
        for i in range(len(df))
    ]
    if len(candles) > CHART_MAX_CANDLES:
        stride = math.ceil(len(candles) / CHART_MAX_CANDLES)
        candles = candles[::stride]

    return {
        "asset": asset,
        "timeframe": timeframe,
        "candles": candles,
        "trade": {k: v for k, v in trade.items() if not k.startswith("_")},
        "entry_ts": entry_ts,
        "exit_ts": exit_ts,
        "approx_entry": approx_entry,
    }


async def _run_backtest(run_id: str, strategy_id: str, start_date: str, end_date: str, db: Client) -> None:
    try:
        db.table("backtest_runs").update({"status": "running"}).eq("id", run_id).execute()

        strat_res = db.table("strategies").select("spec, source_prompt").eq("id", strategy_id).single().execute()
        spec = (strat_res.data or {}).get("spec") or {}
        source_prompt = (strat_res.data or {}).get("source_prompt") or ""

        # ccxt + pandas work is blocking — keep the event loop free
        stats = await asyncio.to_thread(_run_backtest_sync, spec, source_prompt, start_date, end_date)

        db.table("backtest_runs").update({
            "status": "completed",
            "stats": stats,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", run_id).execute()

    except BacktestError as be:
        log.info(f"Backtest {run_id} failed honestly: {be}")
        db.table("backtest_runs").update({"status": "failed", "error": str(be)}).eq("id", run_id).execute()
    except Exception as exc:
        log.exception(f"Backtest {run_id} crashed")
        db.table("backtest_runs").update({
            "status": "failed",
            "error": "The backtest engine hit an unexpected error. Please try again or rephrase the strategy.",
        }).eq("id", run_id).execute()


# ── Endpoints (unchanged contracts) ──────────────────────────────────────────

@router.post("", status_code=status.HTTP_202_ACCEPTED, summary="Queue a backtest run")
async def queue_backtest(
    body: BacktestRequest,
    background_tasks: BackgroundTasks,
    user_id: CurrentUser,
    db: Annotated[Client, Depends(get_db)],
) -> dict:
    # 1. Fetch user's subscription tier
    prof_res = db.table("profiles").select("tier").eq("id", user_id).single().execute()
    tier = "free"
    if prof_res.data:
        tier = prof_res.data.get("tier") or "free"

    # 2. Count existing runs toward the quota — a run that failed (bad rule,
    #    no trades, engine error) never consumed a real backtest, so it
    #    shouldn't burn the user's allowance. Only queued/running/completed count.
    count_res = (
        db.table("backtest_runs")
        .select("id", count="exact")
        .eq("user_id", user_id)
        .neq("status", "failed")
        .execute()
    )
    count = count_res.count or 0

    # 3. Enforce strategy backtesting tier limits
    limits = {
        "free": 1,
        "trader": 5,
        "auto": 999999
    }
    user_limit = limits.get(tier.lower(), 1)

    if count >= user_limit:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "LIMIT_EXCEEDED",
                "tier": tier,
                "limit": user_limit,
                "current": count,
                "message": f"Backtest run limit of {user_limit} reached for your '{tier}' plan. Upgrade to unlock more runs."
            }
        )

    # 4. Verify strategy belongs to user
    strat = (
        db.table("strategies")
        .select("id")
        .eq("id", str(body.strategy_id))
        .eq("user_id", user_id)
        .single()
        .execute()
    )
    if not strat.data:
        raise HTTPException(status_code=404, detail="Strategy not found")

    # 5. Insert backtest run
    result = (
        db.table("backtest_runs")
        .insert({
            "strategy_id": str(body.strategy_id),
            "user_id": user_id,
            "start_date": body.start_date,
            "end_date": body.end_date,
            "status": "queued",
        })
        .execute()
    )
    run = result.data[0]

    # 6. Run the engine in the background
    background_tasks.add_task(_run_backtest, run["id"], str(body.strategy_id), body.start_date, body.end_date, db)
    return run


@router.get("/{run_id}", summary="Get backtest run status and results")
async def get_backtest(
    run_id: UUID,
    user_id: CurrentUser,
    db: Annotated[Client, Depends(get_db)],
) -> dict:
    result = (
        db.table("backtest_runs")
        .select("*")
        .eq("id", str(run_id))
        .eq("user_id", user_id)
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Backtest run not found")
    return result.data


@router.get("", summary="List user backtest runs")
async def list_backtests(
    user_id: CurrentUser,
    db: Annotated[Client, Depends(get_db)],
) -> list[dict]:
    result = (
        db.table("backtest_runs")
        .select("id, strategy_id, start_date, end_date, status, created_at, completed_at, stats")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .execute()
    )
    return result.data


@router.get("/{run_id}/trades/{trade_index}/chart", summary="OHLC context for one backtested trade")
async def get_trade_chart(
    run_id: UUID,
    trade_index: int,
    user_id: CurrentUser,
    db: Annotated[Client, Depends(get_db)],
) -> dict:
    """Real candles around a single trade so the UI can show where it bought and sold."""
    run_res = (
        db.table("backtest_runs")
        .select("stats, strategy_id")
        .eq("id", str(run_id))
        .eq("user_id", user_id)
        .single()
        .execute()
    )
    if not run_res.data or not run_res.data.get("stats"):
        raise HTTPException(status_code=404, detail="Backtest run not found")

    spec = {}
    strat_res = db.table("strategies").select("spec").eq("id", run_res.data["strategy_id"]).single().execute()
    if strat_res.data:
        spec = strat_res.data.get("spec") or {}

    try:
        return await asyncio.to_thread(_trade_chart_payload, run_res.data["stats"], spec, trade_index)
    except BacktestError as be:
        raise HTTPException(status_code=422, detail=str(be))


@router.post("/{run_id}/analyze", summary="AI Strategy Quant Coach Audit")
async def analyze_backtest(
    run_id: UUID,
    user_id: CurrentUser,
    db: Annotated[Client, Depends(get_db)],
) -> dict:
    """
    Analyzes backtest results, identifies trade structural flaws,
    and returns a quantitative audit report (Locked behind Trader/Auto upsell gates).
    """
    # 1. Fetch the run
    run_res = db.table("backtest_runs").select("*").eq("id", str(run_id)).eq("user_id", user_id).single().execute()
    if not run_res.data:
        raise HTTPException(status_code=404, detail="Backtest run not found")
    run = run_res.data

    # 2. Check user subscription tier in profiles
    prof_res = db.table("profiles").select("tier").eq("id", user_id).single().execute()
    tier = "free"
    if prof_res.data:
        tier = prof_res.data.get("tier") or "free"

    # 3. Guard: only available for trader or auto.
    if tier.lower() not in {"trader", "auto"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "UPSELL_REQUIRED",
                "message": "AI Quant Coach Audit and rules auto-tuning are premium features available on the Trader or Auto plans."
            }
        )

    # 4. Fetch the associated strategy spec
    strat_res = db.table("strategies").select("spec").eq("id", run["strategy_id"]).single().execute()
    spec = strat_res.data.get("spec") if strat_res.data else {}

    # 5. Call our AI Audit client
    from app.ai_client import call_ai_audit
    stats = run.get("stats") or {}
    res = await call_ai_audit(spec, stats)
    return res
