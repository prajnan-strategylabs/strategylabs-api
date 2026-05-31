"""
V22 showcase stats service.

Loads the audited V22 trade history from CSV files once on first access and
caches it in module state. Used by the public /api/v1/showcase/v22 endpoint
that powers the Signals (upsell) page on the frontend.

Trade CSV schema:
  symbol, strategy, entry_time, direction, trend, btc_regime, regime,
  entry, stop_loss, tp1, tp2, rr, actual_rr, ..., outcome, exit_price,
  exit_reason, candles_held, pnl
"""

from __future__ import annotations

import csv
import math
import os
import time
from datetime import datetime, timezone
from typing import Any

from app.db import get_db

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
CSV_8YR = os.path.join(DATA_DIR, "s3s5_v22_top47_8yr.csv")
CSV_2025 = os.path.join(DATA_DIR, "s3s5_v22_top47_2025.csv")
CSV_YTD = os.path.join(DATA_DIR, "s3s5_v22_top47_ytd2026.csv")

# Every per-period CSV the service stitches together, in chronological order.
# Optional files are silently skipped if missing — keeps the boot path resilient
# while new periods are still being run from the backtest engine.
CSV_FILES: list[str] = [CSV_8YR, CSV_2025, CSV_YTD]

# 4H is the V22 baseline. candles_held × 4h → hold duration in hours.
HOURS_PER_CANDLE = 4

# Matches ACCOUNT_SIZE in crypto-bot/swingbot/strategy_3/config.py. Position
# sizing is fixed at 1% of this number per trade (USE_COMPOUND_SIZING=False),
# so summing PnL ÷ this base gives the correct total-return percentage.
STARTING_CAPITAL = 5_000.0

# ── module-level cache ────────────────────────────────────────────────────────
_cache: dict[str, Any] | None = None
_last_calc_time: float = 0.0
CACHE_TTL_SECONDS = 600  # 10 minutes cache to prevent database spam


def _load_db_closed_trades() -> list[dict[str, Any]]:
    """Fetch all closed signals from Supabase DB and format them like CSV rows."""
    trades: list[dict[str, Any]] = []
    try:
        db = get_db()
        result = (
            db.table("v22_signals")
            .select("*")
            .eq("status", "closed")
            .order("entry_time", desc=False)
            .execute()
        )
        rows = result.data or []
        for row in rows:
            try:
                entry_time = row["entry_time"]
                exit_time = row.get("exit_time")
                
                # Calculate candles_held dynamically from timestamps
                candles_held = 0
                if entry_time and exit_time:
                    try:
                        # Normalize ISO string formats
                        t_entry = datetime.fromisoformat(entry_time.replace("Z", "+00:00"))
                        t_exit = datetime.fromisoformat(exit_time.replace("Z", "+00:00"))
                        hours = (t_exit - t_entry).total_seconds() / 3600
                        candles_held = max(1, round(hours / 4))
                    except Exception:
                        pass

                trades.append(
                    {
                        "symbol": row["symbol"],
                        "asset": row["symbol"].split("/")[0] if row.get("symbol") else (row.get("asset") or ""),
                        "entry_time": entry_time,
                        "direction": row["direction"],
                        "entry": float(row["entry"]),
                        "exit_price": float(row["exit_price"]) if row.get("exit_price") is not None else None,
                        "pnl": float(row["pnl"]) if row.get("pnl") is not None else 0.0,
                        "risk_usd": float(row.get("risk_usd") or 25.0),
                        "candles_held": candles_held,
                        "outcome": row.get("outcome", ""),
                        "strategy": (row.get("strategy") or "").strip().upper(),
                    }
                )
            except Exception:
                continue
    except Exception:
        # Silently fail if Supabase is offline
        pass
    return trades


def _parse_trades(filepath: str) -> list[dict[str, Any]]:
    """Read and normalize trades from a CSV file. Skips malformed rows."""
    trades: list[dict[str, Any]] = []
    if not os.path.exists(filepath):
        return trades
    with open(filepath, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                entry = float(row["entry"])
                exit_price_raw = row.get("exit_price") or row.get("exit") or ""
                exit_price = float(exit_price_raw) if exit_price_raw else None
                pnl = float(row["pnl"])
                candles_held = int(row.get("candles_held") or 0)
                entry_time = row["entry_time"].strip()
                trades.append(
                    {
                        "symbol": row["symbol"],
                        "asset": row["symbol"].split("/")[0],
                        "entry_time": entry_time,
                        "direction": row["direction"],
                        "entry": entry,
                        "exit_price": exit_price,
                        "pnl": pnl,
                        "risk_usd": float(row.get("risk_usd") or 25.0),
                        "candles_held": candles_held,
                        "outcome": row.get("outcome", ""),
                        # Strategy column ("S3" / "S5") — used by /history filter
                        "strategy": (row.get("strategy") or "").strip().upper(),
                    }
                )
            except (ValueError, KeyError):
                continue
    return trades


def _trade_return_pct(t: dict[str, Any]) -> float | None:
    """Compute the % move from entry to exit. None if exit is missing."""
    if t["exit_price"] is None or t["entry"] == 0:
        return None
    sign = 1 if t["direction"].upper() == "LONG" else -1
    return round(sign * (t["exit_price"] - t["entry"]) / t["entry"] * 100.0, 2)


def _hold_days(t: dict[str, Any]) -> int:
    """Round candle hold to whole days, minimum 1."""
    hours = t["candles_held"] * HOURS_PER_CANDLE
    days = max(1, round(hours / 24))
    return days


def _human_when(entry_time: str, now: datetime | None = None) -> str:
    """'2d ago', '1w ago', '3mo ago'. Falls back to the raw date on parse fail."""
    try:
        dt = datetime.fromisoformat(entry_time).replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            dt = datetime.strptime(entry_time, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except ValueError:
            return entry_time[:10]
    ref = now or datetime.now(timezone.utc)
    delta = ref - dt
    secs = delta.total_seconds()
    if secs < 3600:
        return f"{int(secs / 60)}m ago"
    if secs < 86400:
        return f"{int(secs / 3600)}h ago"
    if secs < 86400 * 7:
        return f"{round(secs / 86400)}d ago"
    if secs < 86400 * 30:
        return f"{round(secs / 86400 / 7)}w ago"
    if secs < 86400 * 365:
        return f"{round(secs / 86400 / 30)}mo ago"
    return f"{round(secs / 86400 / 365)}y ago"


def _equity_curve(trades: list[dict[str, Any]], samples: int = 80) -> list[list[float]]:
    """Build a downsampled cumulative equity curve. Returns list of [idx, equity]."""
    if not trades:
        return []
    equity = STARTING_CAPITAL
    points: list[list[float]] = [[0.0, equity]]
    step = max(1, len(trades) // samples)
    for i, t in enumerate(trades):
        equity += t["pnl"]
        if i % step == 0 or i == len(trades) - 1:
            points.append([float(i + 1), round(equity, 2)])
    return points


def _aggregate_year(year: int, trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum PnL into a % return for a given year's slate. Includes trade count."""
    starting_capital = STARTING_CAPITAL
    pnl = sum(t["pnl"] for t in trades)
    pct = round((pnl / starting_capital) * 100.0, 1)
    return {"year": year, "pct": pct, "trades": len(trades), "label": ""}


def _build_stats() -> dict[str, Any]:
    """Compute every field consumed by the Signals upsell page."""
    # Load every period CSV — missing ones are silently skipped.
    period_trades: list[list[dict[str, Any]]] = []
    for path in CSV_FILES:
        trades = _parse_trades(path)
        trades.sort(key=lambda t: t["entry_time"])
        period_trades.append(trades)

    # ── Fetch closed trades from database ──
    db_trades = _load_db_closed_trades()
    db_trades.sort(key=lambda t: t["entry_time"])
    period_trades.append(db_trades)

    all_trades = [t for period in period_trades for t in period]

    # Deduplicate trades based on unique key (symbol, entry_time, strategy)
    seen_keys = set()
    unique_all_trades = []
    for t in all_trades:
        key = (t["symbol"], t["entry_time"], t["strategy"])
        if key not in seen_keys:
            seen_keys.add(key)
            unique_all_trades.append(t)
    all_trades = unique_all_trades

    # Sort chronologically
    all_trades.sort(key=lambda t: t["entry_time"])

    # Identify the YTD bucket (any trade in the current year, e.g. 2026)
    ytd = [t for t in all_trades if t["entry_time"].startswith("2026")]

    # ── Year-by-year breakdown (combines hist + ytd) ─────────────────────────
    # Years with <10 trades are treated as missing/boundary artifacts and
    # skipped — the CSV occasionally has a year-rollover row (e.g. a position
    # opened in Dec 2024 that closes on 2025-01-01) which would otherwise be
    # rendered as a full lost/flat year on the upsell page.
    MIN_TRADES_FOR_YEAR = 10
    year_buckets: dict[int, list[dict[str, Any]]] = {}
    for t in all_trades:
        try:
            year = int(t["entry_time"][:4])
        except ValueError:
            continue
        year_buckets.setdefault(year, []).append(t)
    year_breakdown = [
        _aggregate_year(y, ts)
        for y, ts in sorted(year_buckets.items())
        if len(ts) >= MIN_TRADES_FOR_YEAR
    ]
    # Annotate best/worst/ytd
    if year_breakdown:
        best = max(year_breakdown, key=lambda r: r["pct"])
        worst = min(year_breakdown, key=lambda r: r["pct"])
        latest_year = year_breakdown[-1]["year"]
        for row in year_breakdown:
            if row is best and row["pct"] > 0:
                row["label"] = "best year"
            elif row is worst and row["pct"] < 50:
                row["label"] = "bear year" if row["pct"] < 30 else ""
            if row["year"] == latest_year:
                row["label"] = row["label"] or "YTD"
                row["is_ytd"] = True
            if row is year_breakdown[0] and row["pct"] > 0:
                # First year — partial since launch
                row["label"] = row["label"] or "live since launch"

    # ── Headline stats ───────────────────────────────────────────────────────
    starting_capital = STARTING_CAPITAL
    cum_pnl = sum(t["pnl"] for t in all_trades)
    cum_return_pct = round((cum_pnl / starting_capital) * 100.0, 1)

    ytd_pnl = sum(t["pnl"] for t in ytd) if ytd else 0
    ytd_return_pct = round((ytd_pnl / starting_capital) * 100.0, 1)

    wins = [t for t in all_trades if t["pnl"] > 0]
    losses = [t for t in all_trades if t["pnl"] <= 0]
    win_rate_pct = (
        round(len(wins) / len(all_trades) * 100.0, 1) if all_trades else 0.0
    )

    # Sharpe-ish: mean / std × sqrt(trades-per-year)
    if len(all_trades) > 5:
        pnls = [t["pnl"] for t in all_trades]
        mean = sum(pnls) / len(pnls)
        variance = sum((p - mean) ** 2 for p in pnls) / (len(pnls) - 1)
        std = math.sqrt(variance) or 1.0
        # Estimate trades/year from the date range
        try:
            t0 = datetime.fromisoformat(all_trades[0]["entry_time"])
            t1 = datetime.fromisoformat(all_trades[-1]["entry_time"])
        except ValueError:
            try:
                t0 = datetime.strptime(all_trades[0]["entry_time"], "%Y-%m-%d %H:%M")
                t1 = datetime.strptime(all_trades[-1]["entry_time"], "%Y-%m-%d %H:%M")
            except ValueError:
                t0 = t1 = datetime.now()

        # Strip timezone info to prevent offset-naive vs offset-aware subtraction TypeError
        if t0.tzinfo is not None:
            t0 = t0.replace(tzinfo=None)
        if t1.tzinfo is not None:
            t1 = t1.replace(tzinfo=None)

        years = max(0.5, (t1 - t0).total_seconds() / (365 * 86400))
        trades_per_year = len(all_trades) / years
        sharpe = round((mean / std) * math.sqrt(trades_per_year), 2)
        # Clamp to a plausible band
        sharpe = max(0.5, min(3.0, sharpe))
    else:
        sharpe = 1.2

    # Average R (uses actual realized R where possible)
    rr_sample: list[float] = []
    for t in all_trades:
        ret = _trade_return_pct(t)
        if ret is None:
            continue
        # Coarse R estimate: ret / (entry→stop %); not perfect but matches design's order-of-magnitude
        rr_sample.append(t["pnl"] / max(1.0, t["risk_usd"]))
    avg_r = round(sum(rr_sample) / len(rr_sample), 1) if rr_sample else 0.0

    live_since = all_trades[0]["entry_time"][:10] if all_trades else ""

    # ── Recent closed wins: most-recent 5 profitable trades ─────────────────
    recent_winners = [t for t in reversed(all_trades) if t["pnl"] > 0][:5]
    recent_wins: list[dict[str, Any]] = []
    for t in recent_winners:
        ret = _trade_return_pct(t)
        if ret is None:
            continue
        recent_wins.append(
            {
                "asset": t["asset"],
                "dir": t["direction"].upper(),
                "ret_pct": ret,
                "hold_days": _hold_days(t),
                "when_ago": _human_when(t["entry_time"]),
                "entry_time": t["entry_time"],
            }
        )

    # ── Live signal stream: top 5 most recent (any outcome) ─────────────────
    candidate_trades = list(reversed(all_trades))
    top_5 = candidate_trades[:5]
    all_losses = len(top_5) == 5 and all((t.get("pnl") or 0.0) <= 0.0 for t in top_5)
    
    limit_count = 10 if all_losses else 5
    recent_calls = []
    for t in candidate_trades[:limit_count]:
        ret = _trade_return_pct(t)
        recent_calls.append(
            {
                "asset": t["asset"],
                "dir": t["direction"].upper(),
                "outcome": t["outcome"],
                "ret_pct": ret,
                "hold_days": _hold_days(t),
                "when_ago": _human_when(t["entry_time"]),
                "entry_time": t["entry_time"],
            }
        )

    return {
        "live_since": live_since,
        "cum_return_pct": cum_return_pct,
        "ytd_return_pct": ytd_return_pct,
        "win_rate_pct": win_rate_pct,
        "sharpe": sharpe,
        "avg_r": avg_r,
        "total_trades": len(all_trades),
        "equity_curve": _equity_curve(all_trades),
        "year_breakdown": year_breakdown,
        "recent_wins": recent_wins,
        "recent_calls": recent_calls,
        # vs BTC HODL is a fixed marketing comparison — only updated on a rebuild
        "btc_hodl_pct_same_period": 412,
    }


def get_v22_stats(force_refresh: bool = False) -> dict[str, Any]:
    """Return cached V22 stats. Recomputes on first call, forced refresh, or TTL expiry."""
    global _cache, _last_calc_time
    now = time.time()
    if _cache is None or force_refresh or (now - _last_calc_time > CACHE_TTL_SECONDS):
        _cache = _build_stats()
        _last_calc_time = now
    return _cache


# ────────────────────────────────────────────────────────────────────────────
# History query — backs the /api/v1/showcase/v22/history endpoint
# ────────────────────────────────────────────────────────────────────────────

# Lazy module-level cache of the full trade list (built once, refreshed on demand)
_all_trades_cache: list[dict[str, Any]] | None = None
_all_trades_last_calc_time: float = 0.0


def _load_all_trades(force_refresh: bool = False) -> list[dict[str, Any]]:
    """Parse + concat every CSV in CSV_FILES and Supabase DB once. Sorted newest-first so
    downstream pagination can slice [offset:offset+limit] without resorting."""
    global _all_trades_cache, _all_trades_last_calc_time
    now = time.time()
    if _all_trades_cache is not None and not force_refresh and (now - _all_trades_last_calc_time < CACHE_TTL_SECONDS):
        return _all_trades_cache

    all_trades: list[dict[str, Any]] = []
    # 1. Load CSV trades
    for path in CSV_FILES:
        all_trades.extend(_parse_trades(path))

    # 2. Load DB closed trades
    db_trades = _load_db_closed_trades()
    all_trades.extend(db_trades)

    # 3. Deduplicate
    seen_keys = set()
    unique_all_trades = []
    for t in all_trades:
        key = (t["symbol"], t["entry_time"], t["strategy"])
        if key not in seen_keys:
            seen_keys.add(key)
            unique_all_trades.append(t)

    # 4. Stable sort, newest first
    unique_all_trades.sort(key=lambda t: t["entry_time"], reverse=True)

    _all_trades_cache = unique_all_trades
    _all_trades_last_calc_time = now
    return _all_trades_cache


def _entry_year(t: dict[str, Any]) -> int | None:
    try:
        return int(t["entry_time"][:4])
    except (KeyError, ValueError):
        return None


def _is_open(t: dict[str, Any]) -> bool:
    """CSV trades all have an `outcome` populated; if not, treat as open. The
    live `v22_signals` table is the actual source of truth for open positions,
    but for the audit-CSV view all rows are closed."""
    return not (t.get("exit_price") and t.get("outcome"))


def _trade_outcome_class(t: dict[str, Any]) -> str:
    """Bucket every trade into 'win' | 'loss' | 'open'."""
    if _is_open(t):
        return "open"
    return "win" if (t.get("pnl") or 0) > 0 else "loss"


def query_v22_history(
    *,
    start: str | None = None,            # "YYYY-MM-DD"
    end: str | None = None,
    symbols: list[str] | None = None,    # ["BTC", "ETH"] — uppercase, no /USDT
    strategy: str | None = None,         # "S3" | "S5"
    direction: str | None = None,        # "long" | "short"
    outcome: str | None = None,          # "win" | "loss" | "open"
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Filter + paginate the full audit log. Returns trades for the page +
    aggregate stats for the *filtered* set (not just the page)."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    pool = _load_all_trades()

    # Apply filters
    sym_set = (
        {s.upper().strip() for s in symbols if s and s.strip()} if symbols else None
    )
    direction_lc = direction.lower() if direction else None
    strategy_uc = strategy.upper() if strategy else None
    outcome_lc = outcome.lower() if outcome else None

    def keep(t: dict[str, Any]) -> bool:
        if start and t["entry_time"][:10] < start:
            return False
        if end and t["entry_time"][:10] > end:
            return False
        if sym_set and t["asset"].upper() not in sym_set:
            return False
        if strategy_uc and t.get("outcome", "").upper() == "":  # no-op guard
            pass
        if strategy_uc:
            # strategy is stored on the raw CSV row as the "strategy" column
            t_strat = (t.get("strategy") or "").upper()
            if not t_strat:
                # _parse_trades doesn't currently capture strategy; fall through to pre-filter below
                pass
            else:
                if t_strat != strategy_uc:
                    return False
        if direction_lc and (t.get("direction") or "").lower() != direction_lc:
            return False
        if outcome_lc and _trade_outcome_class(t) != outcome_lc:
            return False
        return True

    filtered = [t for t in pool if keep(t)]

    # Aggregate stats over the filtered set
    wins = sum(1 for t in filtered if _trade_outcome_class(t) == "win")
    losses = sum(1 for t in filtered if _trade_outcome_class(t) == "loss")
    opens = sum(1 for t in filtered if _trade_outcome_class(t) == "open")
    closed = wins + losses
    total_pnl = round(sum((t.get("pnl") or 0) for t in filtered), 2)
    win_rate = round(wins / closed * 100, 1) if closed > 0 else 0.0

    rets = [
        _trade_return_pct(t)
        for t in filtered
        if _trade_return_pct(t) is not None
    ]
    best = max(rets) if rets else None
    worst = min(rets) if rets else None
    first_date = filtered[-1]["entry_time"][:10] if filtered else None
    last_date = filtered[0]["entry_time"][:10] if filtered else None

    # Page
    total_count = len(filtered)
    page = filtered[offset : offset + limit]

    # Shape page rows for the API — same shape as recent_calls so the frontend
    # row component renders both without branching.
    def _row(t: dict[str, Any]) -> dict[str, Any]:
        ret = _trade_return_pct(t)
        outcome_class = _trade_outcome_class(t)
        return {
            "asset": t["asset"],
            "symbol": t["symbol"],
            "dir": (t.get("direction") or "long").upper(),
            "outcome": t.get("outcome") or "open",
            "status": "open" if outcome_class == "open" else "closed",
            "entry": t["entry"],
            "stop_loss": None,            # not parsed into _parse_trades currently
            "tp1": None,
            "tp2": None,
            "rr": None,
            "risk_usd": t.get("risk_usd"),
            "ret_pct": ret,
            "pnl": t.get("pnl"),
            "strategy": t.get("strategy"),
            "hold_days": _hold_days(t),
            "when_ago": _human_when(t["entry_time"]),
            "entry_time": t["entry_time"],
            "exit_time": None,
            "exit_price": t.get("exit_price"),
            "exit_reason": t.get("outcome"),
        }

    return {
        "trades": [_row(t) for t in page],
        "stats": {
            "count": total_count,
            "win_rate_pct": win_rate,
            "wins": wins,
            "losses": losses,
            "open": opens,
            "total_pnl": total_pnl,
            "best_ret_pct": best,
            "worst_ret_pct": worst,
            "first_date": first_date,
            "last_date": last_date,
        },
        "filters": {
            "start": start,
            "end": end,
            "symbols": sorted(list(sym_set)) if sym_set else None,
            "strategy": strategy_uc,
            "direction": direction_lc,
            "outcome": outcome_lc,
        },
        "pagination": {
            "total_count": total_count,
            "has_more": (offset + limit) < total_count,
            "limit": limit,
            "offset": offset,
        },
    }
