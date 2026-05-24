from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, status
from pydantic import BaseModel

from app.auth import CurrentUser
from app.db import get_db
from supabase import Client

router = APIRouter(prefix="/backtests", tags=["backtests"])


class BacktestRequest(BaseModel):
    strategy_id: UUID
    start_date: str  # "YYYY-MM-DD"
    end_date: str    # "YYYY-MM-DD"


async def _run_backtest(run_id: str, strategy_id: str, start_date: str, end_date: str, db: Client) -> None:
    """
    Background task: marks run as running, delegates to crypto-bot backtest engine,
    then writes results back. The actual engine call will be wired in once
    the Python backtest module is importable here.
    """
    try:
        db.table("backtest_runs").update({"status": "running"}).eq("id", run_id).execute()

        # TODO: import and call the strategy engine once mounted
        # from crypto_bot.swingbot.strategy_3 import backtest
        # stats, trades, robustness = backtest.run(strategy_id, start_date, end_date)

        # Placeholder until engine is wired in
        db.table("backtest_runs").update({
            "status": "completed",
            "stats": {"message": "Engine not yet wired — placeholder"},
        }).eq("id", run_id).execute()

    except Exception as exc:
        db.table("backtest_runs").update({"status": "failed", "error": str(exc)}).eq("id", run_id).execute()


@router.post("", status_code=status.HTTP_202_ACCEPTED, summary="Queue a backtest run")
async def queue_backtest(
    body: BacktestRequest,
    background_tasks: BackgroundTasks,
    user_id: CurrentUser,
    db: Annotated[Client, Depends(get_db)],
) -> dict:
    # Verify strategy belongs to user
    strat = (
        db.table("strategies")
        .select("id")
        .eq("id", str(body.strategy_id))
        .eq("user_id", user_id)
        .single()
        .execute()
    )
    if not strat.data:
        raise HTTPException(status_code=404, detail="Strategy not found")

    result = (
        db.table("backtest_runs")
        .insert({
            "strategy_id": str(body.strategy_id),
            "user_id": user_id,
            "start_date": body.start_date,
            "end_date": body.end_date,
            "status": "queued",
        })
        .execute()
    )
    run = result.data[0]
    background_tasks.add_task(_run_backtest, run["id"], str(body.strategy_id), body.start_date, body.end_date, db)
    return run


@router.get("/{run_id}", summary="Get backtest run status and results")
async def get_backtest(
    run_id: UUID,
    user_id: CurrentUser,
    db: Annotated[Client, Depends(get_db)],
) -> dict:
    result = (
        db.table("backtest_runs")
        .select("*")
        .eq("id", str(run_id))
        .eq("user_id", user_id)
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Backtest run not found")
    return result.data


@router.get("", summary="List user backtest runs")
async def list_backtests(
    user_id: CurrentUser,
    db: Annotated[Client, Depends(get_db)],
) -> list[dict]:
    result = (
        db.table("backtest_runs")
        .select("id, strategy_id, start_date, end_date, status, created_at, completed_at, stats")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .execute()
    )
    return result.data
