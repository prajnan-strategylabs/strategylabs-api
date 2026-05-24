from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from app.auth import CurrentUser
from app.db import get_db
from supabase import Client

router = APIRouter(prefix="/signals", tags=["signals"])


@router.get("", summary="List live signals for user's strategies")
async def list_signals(
    user_id: CurrentUser,
    db: Annotated[Client, Depends(get_db)],
    status: str | None = None,
    limit: int = 50,
) -> list[dict]:
    # Fetch user's strategy IDs first, then signals for those strategies
    strats = (
        db.table("strategies")
        .select("id")
        .eq("user_id", user_id)
        .execute()
    )
    strategy_ids = [s["id"] for s in strats.data]
    if not strategy_ids:
        return []

    query = (
        db.table("signals")
        .select("*")
        .in_("strategy_id", strategy_ids)
        .order("created_at", desc=True)
        .limit(limit)
    )
    if status:
        query = query.eq("status", status)

    result = query.execute()
    return result.data


@router.get("/{signal_id}", summary="Get signal detail")
async def get_signal(
    signal_id: UUID,
    user_id: CurrentUser,
    db: Annotated[Client, Depends(get_db)],
) -> dict:
    result = (
        db.table("signals")
        .select("*, strategies!inner(user_id)")
        .eq("id", str(signal_id))
        .eq("strategies.user_id", user_id)
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Signal not found")
    return result.data
