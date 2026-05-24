import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, field_validator

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
