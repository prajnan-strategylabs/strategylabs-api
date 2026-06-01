import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.admin_auth import Admin
from app.db import get_db
from supabase import Client

log = logging.getLogger("app.admin.users")
router = APIRouter(prefix="/users", tags=["admin"])

class TierUpdate(BaseModel):
    tier: str

@router.get("")
async def get_all_users(
    admin: Admin,
    db: Annotated[Client, Depends(get_db)],
) -> list:
    """
    Get all Supabase Auth users along with their tier from profiles.
    """
    try:
        users_res = db.auth.admin.list_users()
        
        # Determine the user list structure from supabase auth
        users_list = []
        if hasattr(users_res, "users"):
            users_list = users_res.users
        elif isinstance(users_res, dict) and "users" in users_res:
            users_list = users_res["users"]

        # Fetch all profiles
        profiles_res = db.table("profiles").select("*").execute()
        profiles_map = {}
        if profiles_res.data:
            for p in profiles_res.data:
                profiles_map[p["id"]] = p

        result = []
        for u in users_list:
            # Safely fetch properties
            def get_attr(obj, attr, default=None):
                if isinstance(obj, dict):
                    return obj.get(attr, default)
                return getattr(obj, attr, default)

            uid = get_attr(u, "id")
            email = get_attr(u, "email")
            created_at = get_attr(u, "created_at")
            last_sign_in_at = get_attr(u, "last_sign_in_at")

            if not uid:
                continue

            profile = profiles_map.get(uid) or {}
            tier = profile.get("tier") or "free"

            result.append({
                "id": uid,
                "email": email,
                "created_at": created_at,
                "last_sign_in_at": last_sign_in_at,
                "tier": tier
            })

        return result
    except Exception as e:
        log.error(f"Error listing users: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch users: {str(e)}"
        )

@router.patch("/{user_id}/tier")
async def update_user_tier(
    user_id: str,
    body: TierUpdate,
    admin: Admin,
    db: Annotated[Client, Depends(get_db)],
) -> dict:
    """
    Update a user's subscription tier in the profiles table.
    """
    allowed_tiers = {"free", "trader", "auto"}
    if body.tier not in allowed_tiers:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid tier. Must be one of: {', '.join(allowed_tiers)}"
        )

    try:
        res = db.table("profiles").upsert({"id": user_id, "tier": body.tier}).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="User profile not found")
        return {"ok": True, "tier": body.tier}
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Error updating tier for {user_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update tier: {str(e)}"
        )
