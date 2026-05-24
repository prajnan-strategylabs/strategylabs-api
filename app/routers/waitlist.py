import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, field_validator

from app.auth import CurrentUser
from app.config import get_settings
from app.db import get_db
from supabase import Client

router = APIRouter(prefix="/waitlist", tags=["waitlist"])

UTM_KEYS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"}


class JoinRequest(BaseModel):
    email: EmailStr
    source: str = "hero"

    @field_validator("source")
    @classmethod
    def sanitize_source(cls, v: str) -> str:
        return re.sub(r"[^a-z0-9_\-]", "", v.lower())[:64]


class JoinResponse(BaseModel):
    ok: bool
    already_member: bool = False


@router.post("", response_model=JoinResponse, status_code=status.HTTP_200_OK)
async def join_waitlist(
    body: JoinRequest,
    request: Request,
    db: Annotated[Client, Depends(get_db)],
) -> JoinResponse:
    # Hard block: if the waitlist is marked full, refuse new signups even if
    # someone bypasses the UI by hitting the endpoint directly.
    if get_settings().waitlist_full:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The waitlist is currently full. Follow @strategylabs for the next batch.",
        )

    email = body.email.lower().strip()

    referrer = request.headers.get("referer") or None
    utm: dict | None = None
    if referer_url := request.headers.get("x-page-url"):
        from urllib.parse import urlparse, parse_qs
        params = parse_qs(urlparse(referer_url).query)
        utm_data = {k: params[k][0] for k in UTM_KEYS if k in params}
        utm = utm_data or None

    result = (
        db.table("waitlist")
        .insert({"email": email, "source": body.source, "referrer": referrer, "utm": utm})
        .execute()
    )

    # supabase-py raises on DB errors; duplicate key = PostgrestAPIError with code 23505
    return JoinResponse(ok=True, already_member=False)


@router.post("/check-duplicate", include_in_schema=False)
async def _handle_duplicate_gracefully():
    # Supabase raises PostgrestAPIError for unique violations.
    # The exception handler in main.py maps code 23505 → already_member=True.
    pass


class MeResponse(BaseModel):
    on_waitlist: bool
    position: int | None = None  # 1-based position by created_at asc; null if not on list
    source: str | None = None


@router.get("/me", response_model=MeResponse, summary="Check if current user is on the waitlist")
async def waitlist_me(
    user_id: CurrentUser,
    db: Annotated[Client, Depends(get_db)],
) -> MeResponse:
    """
    Returns waitlist status for the authenticated user (looked up by email).
    Used by the frontend to gate dashboard access during pre-launch.
    """
    # Look up the user's email from auth.users via service-role client
    user_lookup = db.auth.admin.get_user_by_id(user_id)
    if not user_lookup or not user_lookup.user or not user_lookup.user.email:
        raise HTTPException(status_code=404, detail="User not found")
    email = user_lookup.user.email.lower().strip()

    # Find their waitlist row (if any)
    row = (
        db.table("waitlist")
        .select("id, source, created_at")
        .eq("email", email)
        .limit(1)
        .execute()
    )

    if not row.data:
        return MeResponse(on_waitlist=False)

    entry = row.data[0]

    # Compute 1-based position by counting earlier (or equal-created_at) rows
    count = (
        db.table("waitlist")
        .select("id", count="exact")
        .lte("created_at", entry["created_at"])
        .execute()
    )
    position = count.count if count.count is not None else None

    return MeResponse(on_waitlist=True, position=position, source=entry.get("source"))
