"""
Native push notification device-token registration.

Flow
----
1. User grants notification permission in the app (Capacitor PushNotifications)
2. App receives an FCM registration token from the OS
3. Frontend: POST /api/v1/push/register { token } -> upserts push_tokens row
4. Scanner fires notify_new_signal_push / notify_closed_signal_push, which
   looks up all enabled tokens for eligible-tier users and sends via FCM.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.auth import CurrentUser
from app.config import get_settings
from app.db import get_db

log = logging.getLogger(__name__)
router = APIRouter(prefix="/push", tags=["push"])


class RegisterRequest(BaseModel):
    token: str
    platform: str = "android"


class PauseRequest(BaseModel):
    enabled: bool


class StatusResponse(BaseModel):
    is_registered: bool
    enabled: bool
    signal_min_tier: str


@router.post("/register", status_code=status.HTTP_204_NO_CONTENT, summary="Register (or refresh) a device's FCM token")
async def register_token(body: RegisterRequest, user_id: CurrentUser) -> None:
    if not body.token.strip():
        raise HTTPException(status_code=400, detail="token is required")

    db = get_db()
    # Upsert by token (unique): if this exact token was previously registered
    # to a different user (e.g. account switch on the same device), reassign
    # it rather than erroring — the newest registration wins.
    existing = (
        db.table("push_tokens").select("id")
        .eq("token", body.token).maybe_single().execute()
    )
    if existing and existing.data:
        db.table("push_tokens").update(
            {"user_id": user_id, "platform": body.platform, "enabled": True}
        ).eq("id", existing.data["id"]).execute()
    else:
        db.table("push_tokens").insert(
            {"user_id": user_id, "token": body.token, "platform": body.platform, "enabled": True}
        ).execute()


@router.get("/status", response_model=StatusResponse, summary="Check current push registration state")
async def get_status(user_id: CurrentUser) -> StatusResponse:
    settings = get_settings()
    db = get_db()
    rows = (
        db.table("push_tokens")
        .select("enabled")
        .eq("user_id", user_id)
        .execute()
    )
    data = rows.data or []
    return StatusResponse(
        is_registered=len(data) > 0,
        enabled=any(r.get("enabled") for r in data) if data else False,
        signal_min_tier=settings.push_signal_min_tier,
    )


@router.patch("/pause", summary="Pause or resume push delivery for all of this user's devices")
async def set_paused(body: PauseRequest, user_id: CurrentUser) -> dict[str, bool]:
    get_db().table("push_tokens").update(
        {"enabled": body.enabled}
    ).eq("user_id", user_id).execute()
    return {"enabled": body.enabled}


@router.delete("/unregister", status_code=status.HTTP_204_NO_CONTENT, summary="Remove a specific device token")
async def unregister_token(token: str, user_id: CurrentUser) -> None:
    get_db().table("push_tokens").delete().eq("user_id", user_id).eq("token", token).execute()
