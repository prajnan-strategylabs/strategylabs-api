"""
Telegram linking + webhook endpoints.

Flow
----
1. User clicks "Connect Telegram" in the app
2. Frontend: POST /api/v1/telegram/link  -> { url, token, expires_at }
3. App opens t.me/<bot>?start=<token> in a new tab
4. User taps /start in Telegram → Telegram POSTs to our webhook
5. Webhook resolves token → user_id, captures chat_id+@handle, marks verified
6. User sees "Linked ✓" in the app on next refresh
"""

from __future__ import annotations

import secrets
import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel

from app.auth import CurrentUser
from app.config import get_settings
from app.db import get_db

log = logging.getLogger(__name__)
router = APIRouter(prefix="/telegram", tags=["telegram"])

# How long the deep-link token is valid before it must be regenerated
LINK_TOKEN_TTL_MIN = 15


# ── Response models ────────────────────────────────────────────────────────

class LinkResponse(BaseModel):
    url: str
    token: str
    expires_at: str


class StatusResponse(BaseModel):
    is_linked: bool
    enabled: bool
    telegram_handle: str | None
    verified_at: str | None
    last_sent_at: str | None
    bot_username: str | None
    # The configured tier floor for sending alerts. UI uses this to render
    # "available on Explorer plan and above" copy.
    signal_min_tier: str


class PauseRequest(BaseModel):
    enabled: bool


# ── Helpers ─────────────────────────────────────────────────────────────────

def _new_token() -> str:
    # 22-char URL-safe token; Telegram's /start parameter caps at 64 chars
    return secrets.token_urlsafe(16)


# ── Routes ──────────────────────────────────────────────────────────────────

@router.post("/link", response_model=LinkResponse, summary="Generate a fresh Telegram deep-link")
async def create_link(user_id: CurrentUser) -> LinkResponse:
    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_bot_username:
        raise HTTPException(
            status_code=503,
            detail="Telegram alerts are not configured on the server.",
        )

    db = get_db()
    token = _new_token()
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=LINK_TOKEN_TTL_MIN)).isoformat()

    # Upsert by user_id: re-clicking "Connect" rotates the token; doesn't wipe
    # the chat_id/verified_at fields if they were already set.
    existing = (
        db.table("telegram_subscriptions").select("id, verified_at, chat_id, telegram_handle")
        .eq("user_id", user_id).maybe_single().execute()
    )

    if existing and existing.data:
        row_id = existing.data["id"]
        db.table("telegram_subscriptions").update(
            {"link_token": token, "link_token_expires_at": expires_at}
        ).eq("id", row_id).execute()
    else:
        db.table("telegram_subscriptions").insert(
            {
                "user_id": user_id,
                "link_token": token,
                "link_token_expires_at": expires_at,
                "enabled": True,
            }
        ).execute()

    url = f"https://t.me/{settings.telegram_bot_username}?start={token}"
    return LinkResponse(url=url, token=token, expires_at=expires_at)


@router.get("/status", response_model=StatusResponse, summary="Check current Telegram link state")
async def get_status(user_id: CurrentUser) -> StatusResponse:
    settings = get_settings()
    db = get_db()
    row = (
        db.table("telegram_subscriptions")
        .select("chat_id, enabled, telegram_handle, verified_at, last_sent_at")
        .eq("user_id", user_id).maybe_single().execute()
    )
    data = (row.data if row else None) or {}
    return StatusResponse(
        is_linked=bool(data.get("verified_at")) and bool(data.get("chat_id")),
        enabled=bool(data.get("enabled", True)),
        telegram_handle=data.get("telegram_handle"),
        verified_at=data.get("verified_at"),
        last_sent_at=data.get("last_sent_at"),
        bot_username=settings.telegram_bot_username or None,
        signal_min_tier=settings.telegram_signal_min_tier,
    )


@router.patch("/pause", summary="Pause or resume signal delivery")
async def set_paused(body: PauseRequest, user_id: CurrentUser) -> dict[str, bool]:
    get_db().table("telegram_subscriptions").update(
        {"enabled": body.enabled}
    ).eq("user_id", user_id).execute()
    return {"enabled": body.enabled}


@router.delete("/unlink", status_code=status.HTTP_204_NO_CONTENT, summary="Disconnect Telegram")
async def unlink(user_id: CurrentUser) -> None:
    get_db().table("telegram_subscriptions").delete().eq("user_id", user_id).execute()


# ── Webhook (called by Telegram, not by browser) ────────────────────────────

@router.post("/webhook", summary="Telegram webhook receiver (internal)")
async def telegram_webhook(
    update: dict[str, Any],
    x_telegram_bot_api_secret_token: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    """Receives the user's /start <token> message and verifies the link."""
    settings = get_settings()
    # Telegram echoes the secret we set via setWebhook back as this header.
    # If we configured one, require it on every incoming hook.
    if settings.telegram_webhook_secret and (
        x_telegram_bot_api_secret_token != settings.telegram_webhook_secret
    ):
        # Don't reveal anything to a hostile caller
        raise HTTPException(status_code=401, detail="invalid secret")

    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return {"ok": "ignored"}

    text = (msg.get("text") or "").strip()
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    handle = chat.get("username")  # may be None if user has no @username set
    if not chat_id:
        return {"ok": "ignored"}

    # Only act on "/start <token>"
    if not text.startswith("/start"):
        # Optional: respond to /help, /stop, etc. Skip for v1.
        return {"ok": "noop"}

    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        # /start with no token — generic welcome
        await _send_text(settings.telegram_bot_token, chat_id,
                         "Hi! Open Strategy Labs and tap 'Connect Telegram' "
                         "to link this account.")
        return {"ok": "no_token"}

    token = parts[1].strip()
    db = get_db()
    row = (
        db.table("telegram_subscriptions")
        .select("id, user_id, link_token_expires_at, verified_at")
        .eq("link_token", token).maybe_single().execute()
    )
    if not row or not row.data:
        await _send_text(settings.telegram_bot_token, chat_id,
                         "That link is invalid or already used. Open the app "
                         "and click 'Connect Telegram' again to get a fresh one.")
        return {"ok": "unknown_token"}

    # Check expiry
    try:
        expires_at = datetime.fromisoformat(row.data["link_token_expires_at"].replace("Z", "+00:00"))
    except Exception:
        expires_at = datetime.now(timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        await _send_text(settings.telegram_bot_token, chat_id,
                         "That link expired. Open the app and click 'Connect "
                         "Telegram' again to get a fresh one.")
        return {"ok": "expired"}

    # Link the chat_id + handle, mark verified
    db.table("telegram_subscriptions").update(
        {
            "chat_id": int(chat_id),
            "telegram_handle": handle,
            "verified_at": datetime.now(timezone.utc).isoformat(),
            # invalidate the token — single-use
            "link_token_expires_at": datetime.now(timezone.utc).isoformat(),
        }
    ).eq("id", row.data["id"]).execute()

    await _send_text(settings.telegram_bot_token, chat_id,
                     "✓ Linked. You'll get V22 signal alerts here. "
                     "Send /stop in the app to pause anytime.")
    return {"ok": "linked"}


# ── Outbound helper used by the webhook ─────────────────────────────────────

async def _send_text(token: str, chat_id: int, text: str) -> None:
    if not token:
        return
    import httpx
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=10.0,
            )
    except Exception as e:
        log.warning(f"[telegram] _send_text failed: {e}")
