"""
Admin-only authentication guard.

All admin endpoints require:
1. ADMIN_ENABLED=true in the environment
2. A valid Supabase JWT token  
3. The authenticated user's email must be in ADMIN_EMAILS
"""
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings
from app.db import get_db

bearer = HTTPBearer()

@dataclass
class AdminUser:
    id: str
    email: str

def require_admin(
    creds: Annotated[HTTPAuthorizationCredentials, Security(bearer)],
) -> AdminUser:
    settings = get_settings()
    if not settings.admin_enabled:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin panel is disabled")
    
    token = creds.credentials
    db = get_db()
    
    try:
        response = db.auth.get_user(token)
        if not response or not response.user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Authentication failed: {str(exc)}")
    
    user = response.user
    email = (user.email or "").lower()
    
    admin_emails_str = settings.admin_emails or ""
    allowed = [e.strip().lower() for e in admin_emails_str.split(",") if e.strip()]
    
    if email not in allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not an admin")
    
    return AdminUser(id=user.id, email=email)

Admin = Annotated[AdminUser, Depends(require_admin)]
