"""
Telegram notifier for V22 signals.

Fires when the scanner inserts a new row into v22_signals. Loads the set of
verified+enabled subscribers whose tier meets the configured floor and
sends each one a formatted message via Telegram's Bot API.

Graceful no-op when TELEGRAM_BOT_TOKEN is unset — useful for local dev.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import get_settings
from app.db import get_db

log = logging.getLogger("v22.notify")

# Ordered tier ladder: anyone at index ≥ min_tier_idx is eligible.
TIER_ORDER = ["free", "trader", "auto"]


def _tier_index(tier: str) -> int:
    try:
        return TIER_ORDER.index(tier)
    except ValueError:
        return 0  # unknown tier → treat as free


# ── Eligibility ─────────────────────────────────────────────────────────────

def _list_eligible_chat_ids() -> list[tuple[int, str]]:
    """Return (chat_id, user_id) for every subscriber who should receive alerts.

    Joins telegram_subscriptions × profiles and filters by tier in Python.
    Could be a single SQL with a join, but keeping it as 2 hops since both
    tables are tiny.
    """
    settings = get_settings()
    floor_idx = _tier_index(settings.telegram_signal_min_tier)
    db = get_db()

    try:
        subs = (
            db.table("telegram_subscriptions")
            .select("user_id, chat_id, enabled, verified_at")
            .execute()
        )
    except Exception as e:
        log.warning(f"[notify] could not list subscriptions: {e}")
        return []

    candidates: list[dict[str, Any]] = [
        s for s in (subs.data or [])
        if s.get("verified_at") and s.get("enabled") and s.get("chat_id")
    ]
    if not candidates:
        return []

    # Fetch tiers in one query
    user_ids = [s["user_id"] for s in candidates]
    try:
        profs = (
            db.table("profiles")
            .select("id, tier")
            .in_("id", user_ids)
            .execute()
        )
    except Exception as e:
        log.warning(f"[notify] could not fetch profiles for tier check: {e}")
        return []

    tier_by_user: dict[str, str] = {p["id"]: (p.get("tier") or "free") for p in (profs.data or [])}
    eligible: list[tuple[int, str]] = []
    for s in candidates:
        tier = tier_by_user.get(s["user_id"], "free")
        if _tier_index(tier) >= floor_idx:
            eligible.append((int(s["chat_id"]), s["user_id"]))
    return eligible


# ── Message formatter ───────────────────────────────────────────────────────

def html_escape(val: Any) -> str:
    if val is None:
        return ""
    s = str(val)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt_price(p: float | None) -> str:
    if p is None:
        return "—"
    if p >= 1000:
        return f"${p:,.2f}"
    if p >= 1:
        return f"${p:.3f}"
    return f"${p:.6f}".rstrip("0").rstrip(".")


def _fmt_raw_price(p: float | None) -> str:
    if p is None:
        return "—"
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.3f}"
    return f"{p:.6f}".rstrip("0").rstrip(".")


def format_signal_message(signal: dict[str, Any]) -> str:
    """HTML message body formatted with premium emojis and tap-to-copy code blocks."""
    asset = html_escape(signal.get("asset") or signal.get("symbol", "").split("/")[0])
    direction = html_escape((signal.get("direction") or "").upper())
    arrow = "🟢" if direction == "LONG" else "🔴" if direction == "SHORT" else "⚪"
    rr = signal.get("rr")
    strategy = html_escape(signal.get("strategy", "?"))
    
    entry = _fmt_raw_price(signal.get("entry"))
    sl = _fmt_raw_price(signal.get("stop_loss"))
    tp1 = _fmt_raw_price(signal.get("tp1"))
    tp2 = _fmt_raw_price(signal.get("tp2"))

    lines = [
        f"🛰 <b>V22 SIGNAL RADAR</b>",
        f"────────────────────────",
        f"Asset: <b>{asset}/USDT</b>",
        f"Direction: {arrow} <b>{direction}</b>",
        f"Strategy: <i>{strategy}</i>",
        f"",
        f"🔹 <b>Entry:</b> $<code>{entry}</code>",
        f"🔸 <b>Stop Loss:</b> $<code>{sl}</code>",
        f"🎯 <b>Target 1:</b> $<code>{tp1}</code>",
    ]
    if signal.get("tp2") is not None:
        lines.append(f"🎯 <b>Target 2:</b> $<code>{tp2}</code>")
        
    lines.extend([
        f"",
        f"⚖️ <b>Risk/Reward:</b> <code>{rr}:1</code>",
        f"────────────────────────",
        f"⚠️ <i>Manage your own risk. Not financial advice.</i>"
    ])
    return "\n".join(lines)


def format_close_message(signal: dict[str, Any]) -> str:
    """HTML message body for position close events, custom styled for profit vs loss."""
    asset = html_escape(signal.get("asset") or signal.get("symbol", "").split("/")[0])
    direction = html_escape((signal.get("direction") or "").upper())
    strategy = html_escape(signal.get("strategy", "?"))
    reason = html_escape((signal.get("exit_reason") or "closed").replace("_", " ").upper())
    pnl = signal.get("pnl")
    ret_pct = signal.get("ret_pct")

    # Determine profit vs loss
    is_profit = False
    if pnl is not None:
        is_profit = float(pnl) >= 0
    elif ret_pct is not None:
        is_profit = float(ret_pct) >= 0

    if pnl is None:
        pnl_label = "—"
    else:
        pnl_val = float(pnl)
        pnl_label = f"{'+' if pnl_val >= 0 else '-'}${abs(pnl_val):.2f}"

    if ret_pct is None:
        ret_label = "—"
    else:
        ret_val = float(ret_pct)
        ret_label = f"{'+' if ret_val >= 0 else ''}{ret_val:.2f}%"

    entry = _fmt_raw_price(signal.get("entry"))
    exit_price = _fmt_raw_price(signal.get("exit_price"))

    if is_profit:
        header = "🎉 <b>V22 POSITION CLOSED (PROFIT)</b> 🎉"
        pnl_status = f"🟢 <b>{pnl_label} ({ret_label})</b>"
        footer = "📊 <i>Audit log updated. Keep compounding!</i>"
    else:
        header = "❌ <b>V22 POSITION CLOSED (LOSS)</b> ❌"
        pnl_status = f"🔴 <b>{pnl_label} ({ret_label})</b>"
        footer = "📉 <i>Risk managed. Onto the next trade.</i>"

    lines = [
        header,
        "────────────────────────",
        f"Asset: <b>{asset}/USDT</b>",
        f"Direction: <b>{direction}</b>",
        f"Strategy: <i>{strategy}</i>",
        f"",
        f"🏁 <b>Exit Reason:</b> <b>{reason}</b>",
        f"",
        f"🔹 <b>Entry Price:</b> $<code>{entry}</code>",
        f"🏁 <b>Exit Price:</b> $<code>{exit_price}</code>",
        f"",
        f"💰 <b>P&L:</b> {pnl_status}",
        "────────────────────────",
        footer,
    ]
    return "\n".join(lines)


# ── Sender ──────────────────────────────────────────────────────────────────

async def _send_one(client: httpx.AsyncClient, token: str, chat_id: int, text: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = await client.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10.0,
        )
        if resp.status_code == 200:
            return True
        log.warning(f"[notify] sendMessage chat_id={chat_id} -> {resp.status_code} {resp.text[:200]}")
        return False
    except Exception as e:
        log.warning(f"[notify] sendMessage chat_id={chat_id} errored: {e}")
        return False


async def notify_new_signal(signal: dict[str, Any]) -> int:
    """Dispatch a fresh V22 signal to every eligible Telegram subscriber.
    Returns the count of successful sends."""
    return await _dispatch_telegram_text(format_signal_message(signal), "signal")


async def notify_closed_signal(signal: dict[str, Any]) -> int:
    """Dispatch a trade-close update to every eligible Telegram subscriber."""
    return await _dispatch_telegram_text(format_close_message(signal), "close")


async def _dispatch_telegram_text(text: str, label: str) -> int:
    settings = get_settings()
    token = settings.telegram_bot_token
    if not token:
        return 0

    eligible = await asyncio.to_thread(_list_eligible_chat_ids)
    if not eligible:
        return 0

    sent = 0
    # Bump last_sent_at for each subscriber we successfully notified
    now_iso = datetime.now(timezone.utc).isoformat()
    db = get_db()
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *[_send_one(client, token, chat_id, text) for chat_id, _ in eligible],
            return_exceptions=True,
        )
    for (chat_id, user_id), ok in zip(eligible, results):
        if ok is True:
            sent += 1
            try:
                db.table("telegram_subscriptions").update(
                    {"last_sent_at": now_iso}
                ).eq("chat_id", chat_id).execute()
            except Exception:
                pass
    log.info(f"[notify] dispatched {label} to {sent}/{len(eligible)} subscribers")
    return sent
