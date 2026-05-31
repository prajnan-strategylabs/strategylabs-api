import csv
import io
import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, status, Query
from fastapi.responses import StreamingResponse

from app.admin_auth import Admin
from app.db import get_db
from supabase import Client

log = logging.getLogger("app.admin.waitlist")
router = APIRouter(prefix="/waitlist", tags=["admin"])

@router.get("")
async def get_waitlist(
    admin: Admin,
    db: Annotated[Client, Depends(get_db)],
    search: Optional[str] = Query(None),
) -> list:
    """
    Get waitlist submissions with optional search.
    """
    try:
        res = db.table("waitlist").select("*").order("created_at", desc=True).execute()
        data = res.data or []

        if search:
            search_clean = search.lower().strip()
            data = [item for item in data if search_clean in item.get("email", "").lower()]
        
        return data
    except Exception as e:
        log.error(f"Error fetching waitlist: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database query failed: {str(e)}"
        )

@router.get("/export")
async def export_waitlist(
    admin: Admin,
    db: Annotated[Client, Depends(get_db)],
):
    """
    Export waitlist database as a CSV stream.
    """
    try:
        res = db.table("waitlist").select("*").order("created_at", desc=True).execute()
        data = res.data or []

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["id", "email", "source", "referrer", "utm_source", "utm_medium", "utm_campaign", "created_at"])
        
        for row in data:
            writer.writerow([
                row.get("id"),
                row.get("email"),
                row.get("source"),
                row.get("referrer"),
                row.get("utm_source"),
                row.get("utm_medium"),
                row.get("utm_campaign"),
                row.get("created_at")
            ])

        output.seek(0)
        return StreamingResponse(
            io.BytesIO(output.getvalue().encode("utf-8")),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=waitlist_export.csv"}
        )
    except Exception as e:
        log.error(f"Error exporting waitlist: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Export failed: {str(e)}"
        )

@router.delete("/{email}")
async def delete_waitlist_entry(
    email: str,
    admin: Admin,
    db: Annotated[Client, Depends(get_db)],
) -> dict:
    """
    Delete a waitlist submission.
    """
    try:
        res = db.table("waitlist").delete().eq("email", email).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="Waitlist entry not found")
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Error deleting waitlist {email}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete waitlist entry: {str(e)}"
        )
