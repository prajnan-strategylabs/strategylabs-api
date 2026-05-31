import os
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.admin_auth import Admin
from app.config import get_settings, Settings
from app.db import get_db
from supabase import Client

log = logging.getLogger("app.admin.tracker")
router = APIRouter(tags=["admin"])

@router.get("/admin-check")
async def admin_check(admin: Admin) -> dict:
    """
    Lightweight check to verify if the current user is an admin.
    """
    return {"ok": True, "email": admin.email}

@router.get("/stats")
async def get_stats(
    admin: Admin,
    db: Annotated[Client, Depends(get_db)],
) -> dict:
    """
    Fetch overview dashboard metrics.
    """
    def get_count(table_name: str) -> int:
        try:
            res = db.table(table_name).select("id", count="exact").execute()
            if hasattr(res, "count") and res.count is not None:
                return res.count
            if hasattr(res, "data") and res.data:
                return len(res.data)
        except Exception as e:
            log.warning(f"Error getting count for {table_name}: {e}")
        return 0

    waitlist_count = get_count("waitlist")
    blog_count = get_count("blogs")
    strategy_count = get_count("strategies")
    signal_count = get_count("v22_signals")

    users_count = 0
    try:
        users_res = db.auth.admin.list_users()
        if hasattr(users_res, "users"):
            users_count = len(users_res.users)
        elif isinstance(users_res, dict) and "users" in users_res:
            users_count = len(users_res["users"])
    except Exception as e:
        log.warning(f"Error listing users for stats: {e}")

    return {
        "users": users_count,
        "waitlist": waitlist_count,
        "blogs": blog_count,
        "strategies": strategy_count,
        "signals": signal_count,
    }

@router.get("/config")
async def get_app_config(
    admin: Admin,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    """
    Get key application variables (read-only for security settings).
    """
    return {
        "is_launched": settings.is_launched,
        "waitlist_full": settings.waitlist_full,
        "admin_enabled": settings.admin_enabled,
        "v22_scanner_disabled": os.environ.get("V22_SCANNER_DISABLED", "").lower() in {"1", "true", "yes"},
    }

@router.get("/strategies", summary="Global strategy rule compiler tracker")
async def get_all_strategies(
    admin: Admin,
    db: Annotated[Client, Depends(get_db)],
) -> list:
    """
    Get all strategy rules compiled in the system across all users,
    mapping user emails alongside prompts and specs.
    """
    try:
        # Fetch strategies
        strats_res = db.table("strategies").select("*").order("created_at", desc=True).execute()
        strats = strats_res.data or []

        # Fetch profiles to map emails
        prof_res = db.table("profiles").select("id, tier").execute()
        profiles_map = {p["id"]: p for p in prof_res.data} if prof_res.data else {}

        # Fetch users
        users_res = db.auth.admin.list_users()
        users_list = []
        if hasattr(users_res, "users"):
            users_list = users_res.users
        elif isinstance(users_res, dict) and "users" in users_res:
            users_list = users_res["users"]

        users_map = {}
        for u in users_list:
            def get_attr(obj, attr):
                if isinstance(obj, dict):
                    return obj.get(attr)
                return getattr(obj, attr, None)
            uid = get_attr(u, "id")
            if uid:
                users_map[uid] = get_attr(u, "email")

        result = []
        for s in strats:
            uid = s.get("user_id")
            email = users_map.get(uid) or "Unknown User"
            profile = profiles_map.get(uid) or {}
            tier = profile.get("tier") or "free"

            result.append({
                "id": s.get("id"),
                "email": email,
                "tier": tier,
                "name": s.get("name"),
                "spec": s.get("spec"),
                "source_prompt": s.get("source_prompt"),
                "created_at": s.get("created_at")
            })

        return result
    except Exception as e:
        log.error(f"Error fetching global strategies: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Strategies tracking failed: {str(e)}"
        )

@router.get("/backtests", summary="Global backtesting runs log monitor")
async def get_all_backtests(
    admin: Admin,
    db: Annotated[Client, Depends(get_db)],
) -> list:
    """
    Get all backtesting executions run across the system,
    including strategy specifications, execution status, and results.
    """
    try:
        # Fetch backtest runs
        runs_res = db.table("backtest_runs").select("*").order("created_at", desc=True).execute()
        runs = runs_res.data or []

        # Fetch strategies to map name
        strats_res = db.table("strategies").select("id, name, spec").execute()
        strats_map = {s["id"]: s for s in strats_res.data} if strats_res.data else {}

        # Fetch profiles
        prof_res = db.table("profiles").select("id, tier").execute()
        profiles_map = {p["id"]: p for p in prof_res.data} if prof_res.data else {}

        # Fetch users
        users_res = db.auth.admin.list_users()
        users_list = []
        if hasattr(users_res, "users"):
            users_list = users_res.users
        elif isinstance(users_res, dict) and "users" in users_res:
            users_list = users_res["users"]

        users_map = {}
        for u in users_list:
            def get_attr(obj, attr):
                if isinstance(obj, dict):
                    return obj.get(attr)
                return getattr(obj, attr, None)
            uid = get_attr(u, "id")
            if uid:
                users_map[uid] = get_attr(u, "email")

        result = []
        for r in runs:
            uid = r.get("user_id")
            email = users_map.get(uid) or "Unknown User"
            profile = profiles_map.get(uid) or {}
            tier = profile.get("tier") or "free"

            sid = r.get("strategy_id")
            strat = strats_map.get(sid) or {}
            strat_name = strat.get("name") or "Deleted Strategy"
            spec = strat.get("spec") or {}

            result.append({
                "id": r.get("id"),
                "email": email,
                "tier": tier,
                "strategy_id": sid,
                "strategy_name": strat_name,
                "spec": spec,
                "start_date": r.get("start_date"),
                "end_date": r.get("end_date"),
                "status": r.get("status"),
                "created_at": r.get("created_at"),
                "completed_at": r.get("completed_at"),
                "stats": r.get("stats")
            })

        return result
    except Exception as e:
        log.error(f"Error fetching global backtests: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Backtests tracking failed: {str(e)}"
        )
