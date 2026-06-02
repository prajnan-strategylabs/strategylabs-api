import asyncio
from app.db import get_db
from app.config import get_settings

async def main():
    db = get_db()
    settings = get_settings()
    
    print("TELEGRAM_SIGNAL_MIN_TIER:", settings.telegram_signal_min_tier)
    
    subs = db.table("telegram_subscriptions").select("*").execute().data
    print("\n--- Subscriptions ---")
    for s in subs:
        print(s)
        
    profs = db.table("profiles").select("*").execute().data
    print("\n--- Profiles ---")
    for p in profs:
        print(p)

if __name__ == "__main__":
    asyncio.run(main())
