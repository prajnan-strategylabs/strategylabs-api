"""
JWT validation for Supabase-issued access tokens.

The frontend still uses Supabase Auth (magic link / OAuth).
Every authenticated API request must include:
  Authorization: Bearer <supabase_access_token>

We verify the JWT locally using the Supabase JWT secret (HS256)
so there's no extra round-trip to Supabase on every request.
"""
import os
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings

bearer = HTTPBearer()


def get_current_user_id(
    creds: Annotated[HTTPAuthorizationCredentials, Security(bearer)],
) -> str:
    token = creds.credentials
    s = get_settings()

    # Supabase signs JWTs with the project's JWT secret.
    # Set SUPABASE_JWT_SECRET in .env (Settings → API → JWT secret).
    jwt_secret = os.environ.get("SUPABASE_JWT_SECRET", "")
    if not jwt_secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="JWT secret not configured",
        )

    try:
        payload = jwt.decode(
            token,
            jwt_secret,
            algorithms=["HS256"],
            audience="authenticated",
        )
        user_id: str = payload["sub"]
        return user_id
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc))


CurrentUser = Annotated[str, Depends(get_current_user_id)]
