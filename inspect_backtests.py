import asyncio
import os
from app.db import get_db
from app.config import get_settings

async def main():
    db = get_db()
    runs = db.table("backtest_runs").select("*").order("created_at", desc=True).limit(5).execute().data
    print("\n--- Backtest Runs ---")
    for r in runs:
        print(f"ID: {r['id']}, Status: {r['status']}, Error: {r.get('error')}, Created: {r.get('created_at')}")

if __name__ == "__main__":
    asyncio.run(main())
