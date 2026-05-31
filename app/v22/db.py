"""
V22 Supabase helpers.

Thin wrapper over the service-role Supabase client. The scanner uses these
for the open→closed lifecycle and the showcase endpoint reads the latest N
calls for the "Live" section.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.db import get_db

log = logging.getLogger(__name__)


def _asset_from_symbol(symbol: str) -> str:
    return symbol.split("/")[0]


# ── Inserts / updates ────────────────────────────────────────────────────────

def insert_open_signal(signal: dict[str, Any]) -> dict | None:
    """Insert a freshly-fired signal. Idempotent via the (symbol, entry_time,
    strategy) unique index — duplicate scans are silently no-op'd."""
    db = get_db()
    payload = {
        "entry_time": signal["entry_time"],
        "symbol": signal["symbol"],
        "asset": _asset_from_symbol(signal["symbol"]),
        "strategy": signal["strategy"],
        "direction": signal["direction"],
        "entry": signal["entry"],
        "stop_loss": signal["stop_loss"],
        "tp1": signal["tp1"],
        "tp2": signal.get("tp2"),
        "rr": signal["rr"],
        "risk_usd": signal.get("risk_usd"),
        "position_size": signal.get("position_size"),
        "status": "open",
    }
    try:
        result = (
            db.table("v22_signals")
            .upsert(payload, on_conflict="symbol,entry_time,strategy", ignore_duplicates=True)
            .execute()
        )
        data = result.data or []
        if data:
            log.info(f"[v22] new signal: {payload['symbol']} {payload['direction']} {payload['strategy']}")
            return data[0]
        return None
    except Exception as e:
        log.warning(f"[v22] insert_open_signal failed: {e}")
        return None


def close_signal(
    signal_id: int,
    *,
    exit_price: float,
    exit_time: datetime,
    exit_reason: str,
    pnl: float,
    ret_pct: float,
    outcome: str,
) -> None:
    db = get_db()
    payload = {
        "status": "closed",
        "exit_price": exit_price,
        "exit_time": exit_time.isoformat(),
        "exit_reason": exit_reason,
        "pnl": pnl,
        "ret_pct": ret_pct,
        "outcome": outcome,
        "last_checked_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        db.table("v22_signals").update(payload).eq("id", signal_id).execute()
        log.info(f"[v22] closed #{signal_id} via {exit_reason} → ${pnl:.2f}")
    except Exception as e:
        log.warning(f"[v22] close_signal failed: {e}")


def touch_signal(signal_id: int) -> None:
    """Bump last_checked_at without changing lifecycle."""
    try:
        get_db().table("v22_signals").update(
            {"last_checked_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", signal_id).execute()
    except Exception:
        pass


# ── Reads ────────────────────────────────────────────────────────────────────

def list_open_signals() -> list[dict]:
    try:
        result = (
            get_db()
            .table("v22_signals")
            .select("*")
            .eq("status", "open")
            .order("entry_time", desc=False)
            .execute()
        )
        return list(result.data or [])
    except Exception as e:
        log.warning(f"[v22] list_open_signals failed: {e}")
        return []


def list_recent_signals(limit: int = 5) -> list[dict]:
    """Latest N signals, putting open ones first and filling the rest with recent closed ones."""
    try:
        # 1. Fetch all open signals (newest first)
        open_res = (
            get_db()
            .table("v22_signals")
            .select("*")
            .eq("status", "open")
            .order("entry_time", desc=True)
            .execute()
        )
        open_signals = list(open_res.data or [])
        
        # If we already have enough open signals to satisfy the limit, return them
        if len(open_signals) >= limit:
            return open_signals[:limit]
            
        # 2. Fetch recent closed signals to fill the rest of the limit
        closed_limit = limit - len(open_signals)
        closed_res = (
            get_db()
            .table("v22_signals")
            .select("*")
            .eq("status", "closed")
            .order("entry_time", desc=True)
            .limit(closed_limit)
            .execute()
        )
        closed_signals = list(closed_res.data or [])
        
        return open_signals + closed_signals
    except Exception as e:
        log.warning(f"[v22] list_recent_signals failed: {e}")
        return []


# ── Scanner state ────────────────────────────────────────────────────────────

def write_scanner_state(
    *,
    last_scan_at: datetime | None = None,
    last_exit_check: datetime | None = None,
    last_signal_at: datetime | None = None,
    open_count: int | None = None,
    closed_count: int | None = None,
) -> None:
    payload: dict[str, Any] = {"updated_at": datetime.now(timezone.utc).isoformat()}
    if last_scan_at is not None:
        payload["last_scan_at"] = last_scan_at.isoformat()
    if last_exit_check is not None:
        payload["last_exit_check"] = last_exit_check.isoformat()
    if last_signal_at is not None:
        payload["last_signal_at"] = last_signal_at.isoformat()
    if open_count is not None:
        payload["open_count"] = open_count
    if closed_count is not None:
        payload["closed_count"] = closed_count
    try:
        get_db().table("v22_scanner_state").update(payload).eq("id", 1).execute()
    except Exception as e:
        log.warning(f"[v22] write_scanner_state failed: {e}")


def read_scanner_state() -> dict | None:
    try:
        result = get_db().table("v22_scanner_state").select("*").eq("id", 1).single().execute()
        return result.data
    except Exception:
        return None
