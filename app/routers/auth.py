from typing import Annotated
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, field_validator
from supabase import Client

from app.config import Settings, get_settings
from app.db import get_db

router = APIRouter(prefix="/auth", tags=["auth"])


class OtpRequest(BaseModel):
    email: EmailStr
    redirect_to: str | None = None

    @field_validator("redirect_to")
    @classmethod
    def normalize_redirect_to(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class OtpResponse(BaseModel):
    ok: bool


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _prelaunch_allowed_emails(settings: Settings) -> set[str]:
    return {
        _normalize_email(email)
        for email in settings.prelaunch_auth_emails.split(",")
        if email.strip()
    }


def _allowed_redirect_to(redirect_to: str | None, settings: Settings) -> str | None:
    if not redirect_to:
        return None

    parsed = urlparse(redirect_to)
    if not parsed.scheme or not parsed.netloc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid auth redirect URL.",
        )

    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin not in settings.allowed_origins:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Auth redirect origin is not allowed.",
        )

    return origin


@router.post("/otp", response_model=OtpResponse, summary="Request a sign-in OTP")
async def request_otp(
    body: OtpRequest,
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[Client, Depends(get_db)],
) -> OtpResponse:
    email = _normalize_email(body.email)

    if not settings.is_launched and email not in _prelaunch_allowed_emails(settings):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Strategy Labs is invite-only until launch. Join the waitlist and we'll email you when access opens.",
        )

    redirect_to = _allowed_redirect_to(body.redirect_to, settings)

    try:
        db.auth.sign_in_with_otp(
            {
                "email": email,
                "options": {
                    "email_redirect_to": redirect_to,
                },
            }
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not send sign-in code: {str(exc)}",
        ) from exc

    return OtpResponse(ok=True)
