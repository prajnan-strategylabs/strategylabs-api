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
TIER_ORDER = ["free", "explorer", "trader", "pro", "auto"]


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

def _fmt_price(p: float | None) -> str:
    if p is None:
        return "—"
    if p >= 1000:
        return f"${p:,.2f}"
    if p >= 1:
        return f"${p:.3f}"
    return f"${p:.6f}".rstrip("0").rstrip(".")


def format_signal_message(signal: dict[str, Any]) -> str:
    """Plain-text message body (Telegram default; no markdown to avoid
    escaping headaches on funky tickers)."""
    asset = signal.get("asset") or signal.get("symbol", "").split("/")[0]
    direction = (signal.get("direction") or "").upper()
    arrow = "🟢" if direction == "LONG" else "🔴" if direction == "SHORT" else "⚪"
    rr = signal.get("rr")
    strategy = signal.get("strategy", "?")
    entry = signal.get("entry")
    sl = signal.get("stop_loss")
    tp1 = signal.get("tp1")
    tp2 = signal.get("tp2")

    lines = [
        f"{arrow} V22 · {asset}/USDT · {direction}",
        f"────────────────────────",
        f"Entry      {_fmt_price(entry)}",
        f"Stop loss  {_fmt_price(sl)}",
        f"TP1        {_fmt_price(tp1)}",
    ]
    if tp2:
        lines.append(f"TP2        {_fmt_price(tp2)}")
    lines.append(f"R:R        {rr}:1   ({strategy})")
    lines.append("────────────────────────")
    lines.append("This is not financial advice. Manage your own risk.")
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
    settings = get_settings()
    token = settings.telegram_bot_token
    if not token:
        # Notifier disabled by config — silent no-op
        return 0

    # Eligibility query is sync (Supabase client) — wrap in to_thread
    eligible = await asyncio.to_thread(_list_eligible_chat_ids)
    if not eligible:
        return 0

    text = format_signal_message(signal)
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
    log.info(f"[notify] dispatched signal to {sent}/{len(eligible)} subscribers")
    return sent
