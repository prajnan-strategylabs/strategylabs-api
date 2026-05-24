"""
JWT validation for Supabase-issued access tokens.

The frontend still uses Supabase Auth (magic link / OAuth).
Every authenticated API request must include:
  Authorization: Bearer <supabase_access_token>

We verify the token by calling the official Supabase API client
to authenticate the user, eliminating the need for a static JWT secret.
"""
from typing import Annotated

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.db import get_db

bearer = HTTPBearer()


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


CurrentUser = Annotated[str, Depends(get_current_user_id)]
