from typing import Annotated
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from supabase import Client

from app.config import Settings, get_settings
from app.db import get_db

router = APIRouter(prefix="/analytics", tags=["analytics"])

UTM_KEYS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"}


class PageViewRequest(BaseModel):
    visitor_id: str = Field(min_length=8, max_length=128)
    session_id: str = Field(min_length=8, max_length=128)
    path: str = Field(min_length=1, max_length=512)
    title: str | None = Field(default=None, max_length=256)
    referrer: str | None = Field(default=None, max_length=512)
    utm: dict[str, str] | None = None

    @field_validator("visitor_id", "session_id")
    @classmethod
    def sanitize_identifier(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Identifier cannot be blank")
        return cleaned

    @field_validator("path")
    @classmethod
    def normalize_path(cls, value: str) -> str:
        path = value.strip()
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Path must be a relative URL path")
        return path

    @field_validator("referrer")
    @classmethod
    def reduce_referrer_to_origin(cls, value: str | None) -> str | None:
        if not value:
            return None
        parsed = urlparse(value.strip())
        if not parsed.scheme or not parsed.netloc:
            return None
        return f"{parsed.scheme}://{parsed.netloc}"

    @field_validator("utm")
    @classmethod
    def keep_known_utm_fields(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if not value:
            return None
        cleaned = {
            key: field_value.strip()[:128]
            for key, field_value in value.items()
            if key in UTM_KEYS and field_value.strip()
        }
        return cleaned or None


@router.post("/page-view", status_code=status.HTTP_201_CREATED)
async def record_page_view(
    body: PageViewRequest,
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    db: Annotated[Client, Depends(get_db)],
) -> dict:
    """Store a privacy-conscious, pseudonymous page view."""
    origin = request.headers.get("origin")
    if origin and origin not in settings.allowed_origins:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Origin is not allowed")

    db.table("page_views").insert(
        {
            "visitor_id": body.visitor_id,
            "session_id": body.session_id,
            "path": body.path,
            "title": body.title.strip() if body.title else None,
            "referrer": body.referrer,
            "utm": body.utm,
        }
    ).execute()

    return {"ok": True}
