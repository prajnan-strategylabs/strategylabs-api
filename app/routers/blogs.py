import re
from typing import Annotated, List, Optional
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Header, status
from pydantic import BaseModel

from app.db import get_db
from app.config import get_settings, Settings
from supabase import Client

router = APIRouter(prefix="/blogs", tags=["blogs"])

class BlogPostCreate(BaseModel):
    title: str
    slug: Optional[str] = None
    excerpt: str
    content: str
    cover_gradient: Optional[str] = "from-[#22d3aa]/30 via-[#3b6af1]/25 to-bg-card"
    read_time: Optional[str] = "5 min read"
    tags: List[str] = []

class BlogPostResponse(BaseModel):
    title: str
    slug: str
    excerpt: str
    content: str
    cover_image: Optional[str] = None
    cover_gradient: str
    read_time: str
    tags: List[str]
    created_at: str

# Seeded fallback posts to guarantee the UI is never broken if database is not fully set up
FALLBACK_BLOGS = [
    {
        "title": "Inside V22: Compounding +784% Return with Dual-Regime Ensembles",
        "slug": "inside-v22-dual-regime-backtest",
        "excerpt": "An in-depth mathematical audit of our flagship institutional strategy. Explore the regime classifier consensus on ADX and Choppiness.",
        "cover_gradient": "from-[#22d3aa]/30 via-[#3b6af1]/25 to-bg-card",
        "read_time": "6 min read",
        "tags": ["Strategy Release", "Backtesting", "Regime Classifier"],
        "created_at": "May 20, 2026",
        "content": """# Inside V22: Compounding +784% Return with Dual-Regime Ensembles

Our flagship institutional model, **V22**, represents a significant leap forward in systematic crypto trend-following. In this article, we lay bare the exact mechanics, math, and walk-forward verification that powered its 8-year audited return.

---

### The Regime Problem
Most trading algorithms fail because they are designed for a single market state. A pure trend-follower rips profits in a bull run but gets shredded in a choppy consolidation. Conversely, mean-reversion grids capture ranging profits but blow up during a strong breakout.

The **V22 Strategy** solves this with a **Consensus Regime Classifier**:
* **ADX (Average Directional Index):** Evaluates trend strength.
* **CI (Choppiness Index):** Measures whether price is compressed or expanding.
* **EMA21 Slope:** Determines directional momentum.

By demanding a **2-of-3 consensus**, V22 dynamically switches between two active states:
1. **Trending Regime:** Runs Pullbacks (S3 Strategy) alongside continuation Bollinger Breakouts (S5 Strategy).
2. **Ranging Regime:** Disables pullback entries completely, running only Band-Squeezes to avoid catching falling knives.

---

### Dynamic Sizing & S3 Pullbacks
The S3 component targets daily trend alignments on the 4H/1H charts. By entering pullbacks to the EMA21 only when the **BTC regime gate** is bullish, it acts as a high-fidelity filter. Sizing is continuously adjusted via our proprietary **Drawdown Brake (DD Brake)**—risk is cut in half during equity curves drawdown, preventing compounding drawdowns.

### Verifiable Performance
* **8-Year Return:** +784% ($5k initial capital compounding to $44,196).
* **Max Drawdown:** 8.57% (well within typical prop firm constraints).
* **Sharpe Ratio:** 1.86 portfolio-wide.

The data proves that **portfolio diversification** is the only free lunch in quantitative finance. Interleaving trades across 42 pairs naturally smooths out drawdowns, giving V22 its institutional-grade stability."""
    },
    {
        "title": "The Volatility Squeeze: How Bollinger Bands Identify S5 Continuation Breakouts",
        "slug": "bollinger-squeeze-continuation-s5",
        "excerpt": "Explore the textbook S5 Break-Retest-Go setup. Learn how we filter high-probability breakouts and avoid false fakeouts.",
        "cover_gradient": "from-[#f59e0b]/20 via-[#3b6af1]/25 to-bg-card",
        "read_time": "5 min read",
        "tags": ["Technical Analysis", "S5 Setup", "Breakout Trading"],
        "created_at": "May 12, 2026",
        "content": """# The Volatility Squeeze: Bollinger Bands & S5 Setup

The **S5 Break-Retest-Go** pattern is the absolute workhorse of our algorithmic ensemble. It accounts for **90% of the total profit** compiled over our 8-year historical audit. Here is the exact blueprint for identifying and automating this setup.

---

### Step 1: The Squeeze & Breakout
Markets alternate between compression and expansion. The S5 setup looks for Bollinger Bands contracting to historical lows, indicating an imminent breakout.
* **The Trigger:** A 4H candle closes fully outside the **Bollinger Band (20, 2)**. This confirms momentum expansion.

### Step 2: The Retest
90% of retail traders chase breakouts immediately—and get wiped out by false fakeouts. V22 waits.
* **The Retest Filter:** The price must retest the broken band within **6 candles**. This confirms that the broken barrier has flipped into active support or resistance.

### Step 3: Reclaim & Sizing
* **The Go Signal:** A confirmation candle closes back in the breakout direction with a body size $\\ge 50\\%$.
* **Risk Parameters:** Stop loss is placed at the swing extreme $\\pm 0.5\\times \\text{ATR}$.
* **Take Profits:** TP1 is placed at $2.0\\times$ the stop distance (RR 2.0). TP2 is placed at $4.0\\times$ the stop distance.

---

### Filtering the Noise
To maintain a high profit factor, V22 applies strict filters:
1. **ATR Chaos Check:** Skip the trade if ATR is $>5\\%$, representing extreme market chaos where technical indicators degrade.
2. **BTC Gate:** Longs are completely blocked if Bitcoin is trading below its daily EMA21. We never swim against the macro tide."""
    },
    {
        "title": "Regime Classification: The Secret to Surviving the 2026 Crypto Grind",
        "slug": "regime-classification-crypto-2026",
        "excerpt": "Why retail algos fail in choppy sideways grind, and how systematic regime switches protect your portfolio capital.",
        "cover_gradient": "from-[#ef4444]/20 via-[#3b6af1]/25 to-bg-card",
        "read_time": "4 min read",
        "tags": ["Market Regimes", "Capital Protection", "Risk Management"],
        "created_at": "May 05, 2026",
        "content": """# Regime Classification: Surviving the 2026 Grind

As the crypto market matures, cycles are compressing. Choppy, transitional months are becoming the default state. In this environment, retail algorithms that perform spectacularly in backtests get quickly wiped out in live trading.

---

### The Reality of Choppiness
Ranging markets are characterized by **high fakeout rates**. Breakouts are quickly faded, and swing levels are repeatedly swept. If you run a standard trend-following system in this regime:
* You will buy the highs, get stopped out on the pullbacks, and short the lows.
* This is known as **whipsawing**, and it is the #1 killer of systematic accounts.

### How V22 Adapts
V22 continuously scans the daily ADX and Choppiness index:
* If the daily **Choppiness Index is above 60**, the market is declared **RANGING**.
* Pullback entries (S3) are immediately turned off.
* Bollinger breakout targets are tightened, and stop losses are trailed aggressively using a **1.5x ATR trailing mechanism** that instantly shifts to **1.0x ATR** once a trade hits $+3R$ profit.

### Capital Preservation is the Moat
Professional trading is not about making massive gains in bull markets—it's about **losing as little as possible** during the grinds. By utilizing the V22 consensus switches, your capital remains protected, sitting safely in cash or low-exposure setups, ready to deploy at full capacity when true macro volatility returns."""
    }
]

@router.get("", response_model=List[BlogPostResponse])
async def get_blogs(
    db: Annotated[Client, Depends(get_db)],
) -> List[BlogPostResponse]:
    """
    Get all quantitative blog posts.
    Queries Supabase database table `blogs`, ordered by `created_at desc`.
    Gracefully falls back to pre-seeded high-fidelity articles if the database is unconfigured.
    """
    try:
        result = (
            db.table("blogs")
            .select("*")
            .eq("status", "published")
            .order("created_at", desc=True)
            .execute()
        )
        if result.data and len(result.data) > 0:
            posts = []
            for item in result.data:
                # Format ISO timestamp to a readable date
                created_at_val = item.get("created_at", "")
                try:
                    dt = datetime.fromisoformat(created_at_val.replace("Z", "+00:00"))
                    formatted_date = dt.strftime("%b %d, %Y")
                except Exception:
                    formatted_date = created_at_val[:10] if created_at_val else "May 24, 2026"

                posts.append(BlogPostResponse(
                    title=item["title"],
                    slug=item["slug"],
                    excerpt=item["excerpt"],
                    content=item["content"],
                    cover_image=item.get("cover_image"),
                    cover_gradient=item.get("cover_gradient") or "from-[#22d3aa]/30 via-[#3b6af1]/25 to-bg-card",
                    read_time=item.get("read_time") or "5 min read",
                    tags=item.get("tags") or [],
                    created_at=formatted_date
                ))
            return posts
    except Exception as e:
        # Table not found or connection error -> graceful fallback
        pass
    
    # Return pre-seeded fallbacks
    return [BlogPostResponse(**p) for p in FALLBACK_BLOGS]

@router.post("", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_blog(
    body: BlogPostCreate,
    db: Annotated[Client, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    x_blog_pipeline_secret: Optional[str] = Header(None, alias="X-Blog-Pipeline-Secret"),
) -> dict:
    """
    Secure endpoint for the AI blogging pipeline to submit new automated posts.
    Protected by a secret configured via environment variable/settings.
    """
    # Verify the incoming header secret
    if not x_blog_pipeline_secret or x_blog_pipeline_secret != settings.blog_pipeline_secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Invalid or missing pipeline key."
        )

    # Generate slug if not provided
    slug = body.slug
    if not slug:
        slug = body.title.lower().strip()
        slug = re.sub(r"[^a-z0-9\s-]", "", slug)
        slug = re.sub(r"[\s-]+", "-", slug)
    
    blog_data = {
        "title": body.title,
        "slug": slug,
        "excerpt": body.excerpt,
        "content": body.content,
        "cover_gradient": body.cover_gradient,
        "read_time": body.read_time,
        "tags": body.tags
    }

    try:
        result = db.table("blogs").insert(blog_data).execute()
        return {"ok": True, "message": "Blog post successfully published.", "slug": slug}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database Insertion Failed: {str(e)}"
        )
