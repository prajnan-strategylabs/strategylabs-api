import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.routers import analytics, auth, waitlist, strategies, backtests, signals, showcase, blogs, telegram, push, admin, webhooks


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("app.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Validate config on startup so fly.io deploy fails fast if env vars are missing
    get_settings()

    # ── V22 live scanner ─────────────────────────────────────────────────────
    # Set V22_SCANNER_DISABLED=1 in dev/CI if you don't want the scanner
    # banging Binance / Supabase every minute.
    scanner_disabled = os.environ.get("V22_SCANNER_DISABLED", "").lower() in {"1", "true", "yes"}
    if scanner_disabled:
        log.info("V22 scanner disabled via V22_SCANNER_DISABLED env var")
    else:
        try:
            from app.v22 import scanner as v22_scanner
            await v22_scanner.start()
        except Exception as e:
            log.exception(f"failed to start V22 scanner: {e}")

    yield

    # Graceful shutdown
    try:
        from app.v22 import scanner as v22_scanner
        await v22_scanner.stop()
    except Exception:
        pass


app = FastAPI(
    title="Strategy Labs API",
    version="0.1.0",
    docs_url="/docs",
    redoc_url=None,
    lifespan=lifespan,
)

# ── CORS ─────────────────────────────────────────────────────────────────────
s = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=s.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Exception handlers ────────────────────────────────────────────────────────
@app.exception_handler(Exception)
async def _supabase_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # Map Supabase unique-violation (23505) to a friendly waitlist response
    msg = str(exc)
    if "23505" in msg or "duplicate" in msg.lower():
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"ok": True, "already_member": True},
        )
    raise exc


# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(waitlist.router, prefix="/api/v1")
app.include_router(auth.router, prefix="/api/v1")
app.include_router(analytics.router, prefix="/api/v1")
app.include_router(strategies.router, prefix="/api/v1")
app.include_router(backtests.router, prefix="/api/v1")
app.include_router(signals.router, prefix="/api/v1")
app.include_router(showcase.router, prefix="/api/v1")
app.include_router(blogs.router, prefix="/api/v1")
app.include_router(telegram.router, prefix="/api/v1")
app.include_router(push.router, prefix="/api/v1")
app.include_router(admin.router, prefix="/api/v1")
app.include_router(webhooks.router, prefix="/api/v1")


@app.get("/health", tags=["meta"])
async def health() -> dict:
    return {"status": "ok"}


@app.get("/api/v1/config", tags=["meta"])
async def get_app_config() -> dict:
    settings = get_settings()
    return {
        "is_launched": settings.is_launched,
        "waitlist_full": settings.waitlist_full,
    }
