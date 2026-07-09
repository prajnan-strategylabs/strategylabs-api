"""
JWT validation for Supabase-issued access tokens.

The frontend still uses Supabase Auth (magic link / OAuth).
Every authenticated API request must include:
  Authorization: Bearer <supabase_access_token>

We verify the token by calling the official Supabase API client
to authenticate the user, eliminating the need for a static JWT secret.
"""
from typing import Annotated, Optional

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.db import get_db

bearer = HTTPBearer()
bearer_optional = HTTPBearer(auto_error=False)


def get_current_user_id(
    creds: Annotated[HTTPAuthorizationCredentials, Security(bearer)],
) -> str:
    token = creds.credentials
    db = get_db()

    try:
        # Call Supabase Auth API to retrieve user metadata for the token.
        # This validates the token's signature, expiry, and structure securely.
        response = db.auth.get_user(token)
        if not response or not response.user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token or user not found",
            )
        user_id: str = response.user.id
        return user_id
    except Exception as exc:
        # Catch any network or authentication errors from Supabase
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Authentication failed: {str(exc)}",
        )


def get_optional_user_id(
    creds: Annotated[Optional[HTTPAuthorizationCredentials], Security(bearer_optional)],
) -> Optional[str]:
    """Same idea as get_current_user_id but never raises — for endpoints that
    are publicly viewable (e.g. the marketing showcase) yet must still know
    who's asking so paid-tier content can be gated. Missing or invalid tokens
    simply resolve to None (anonymous / free)."""
    if creds is None:
        return None
    db = get_db()
    try:
        response = db.auth.get_user(creds.credentials)
        if not response or not response.user:
            return None
        return response.user.id
    except Exception:
        return None


def resolve_tier(user_id: Optional[str], db) -> str:
    """Free/trader/auto tier for a possibly-anonymous user. Never raises —
    callers on public endpoints should degrade to "free" rather than 500."""
    if not user_id:
        return "free"
    try:
        prof = db.table("profiles").select("tier").eq("id", user_id).single().execute()
        if prof.data:
            return (prof.data.get("tier") or "free").lower()
    except Exception:
        pass
    return "free"


CurrentUser = Annotated[str, Depends(get_current_user_id)]
OptionalUser = Annotated[Optional[str], Depends(get_optional_user_id)]
