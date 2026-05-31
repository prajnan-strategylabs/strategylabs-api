from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.auth import CurrentUser
from app.db import get_db
from supabase import Client

router = APIRouter(prefix="/strategies", tags=["strategies"])

VALID_STATUSES = {"draft", "backtesting", "ready", "live", "paused", "archived"}


class StrategyCreate(BaseModel):
    name: str
    spec: dict[str, Any]
    source_prompt: str | None = None


class StrategyUpdate(BaseModel):
    name: str | None = None
    spec: dict[str, Any] | None = None
    status: str | None = None
    source_prompt: str | None = None


@router.get("", summary="List user strategies")
async def list_strategies(
    user_id: CurrentUser,
    db: Annotated[Client, Depends(get_db)],
) -> list[dict]:
    result = (
        db.table("strategies")
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .execute()
    )
    return result.data


@router.post("", status_code=status.HTTP_201_CREATED, summary="Create strategy")
async def create_strategy(
    body: StrategyCreate,
    user_id: CurrentUser,
    db: Annotated[Client, Depends(get_db)],
) -> dict:
    result = (
        db.table("strategies")
        .insert({"user_id": user_id, "name": body.name, "spec": body.spec, "source_prompt": body.source_prompt})
        .execute()
    )
    return result.data[0]


@router.get("/{strategy_id}", summary="Get strategy")
async def get_strategy(
    strategy_id: UUID,
    user_id: CurrentUser,
    db: Annotated[Client, Depends(get_db)],
) -> dict:
    result = (
        db.table("strategies")
        .select("*")
        .eq("id", str(strategy_id))
        .eq("user_id", user_id)
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Strategy not found")
    return result.data


@router.patch("/{strategy_id}", summary="Update strategy")
async def update_strategy(
    strategy_id: UUID,
    body: StrategyUpdate,
    user_id: CurrentUser,
    db: Annotated[Client, Depends(get_db)],
) -> dict:
    if body.status and body.status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {VALID_STATUSES}")

    patch = body.model_dump(exclude_none=True)
    if not patch:
        raise HTTPException(status_code=400, detail="No fields to update")

    result = (
        db.table("strategies")
        .update(patch)
        .eq("id", str(strategy_id))
        .eq("user_id", user_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Strategy not found")
    return result.data[0]


@router.delete("/{strategy_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete strategy")
async def delete_strategy(
    strategy_id: UUID,
    user_id: CurrentUser,
    db: Annotated[Client, Depends(get_db)],
) -> None:
    db.table("strategies").delete().eq("id", str(strategy_id)).eq("user_id", user_id).execute()


class ChatMessage(BaseModel):
    role: str
    content: str


class StrategyChatRequest(BaseModel):
    prompt: str
    messages: list[ChatMessage] = []


@router.post("/chat", summary="Interactive strategy builder and doubt resolver")
async def strategy_chat(
    body: StrategyChatRequest,
    user_id: CurrentUser,
) -> dict:
    """
    Conversational Quant Coach that resolves strategy ambiguities,
    asks clarifying questions, and compiles natural language into structured rules.
    """
    msgs_raw = [{"role": m.role, "content": m.content} for m in body.messages]
    from app.ai_client import call_ai_chat
    res = await call_ai_chat(body.prompt, msgs_raw)
    return res
