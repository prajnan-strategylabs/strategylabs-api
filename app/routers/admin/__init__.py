from fastapi import APIRouter
from .blogs import router as blogs_router
from .waitlist import router as waitlist_router
from .users import router as users_router
from .tracker import router as tracker_router

router = APIRouter(prefix="/admin", tags=["admin"])

# Fast API merges sub-routers prefix automatically
router.include_router(blogs_router)
router.include_router(waitlist_router)
router.include_router(users_router)
router.include_router(tracker_router)
