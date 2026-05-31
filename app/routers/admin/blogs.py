import re
import uuid
import os
import logging
from datetime import datetime
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, status
from pydantic import BaseModel

from app.admin_auth import Admin
from app.config import get_settings, Settings
from app.db import get_db
from supabase import Client

log = logging.getLogger("app.admin.blogs")
router = APIRouter(prefix="/blogs", tags=["admin"])

# ── Pydantic Models ───────────────────────────────────────────────────────────

class AdminBlogCreate(BaseModel):
    title: str
    slug: Optional[str] = None
    excerpt: str
    content: str
    cover_image: Optional[str] = None
    cover_gradient: Optional[str] = "from-[#22d3aa]/30 via-[#3b6af1]/25 to-bg-card"
    read_time: Optional[str] = "5 min read"
    tags: List[str] = []
    status: Optional[str] = "published"  # "draft" or "published"

class AdminBlogUpdate(BaseModel):
    title: Optional[str] = None
    slug: Optional[str] = None
    excerpt: Optional[str] = None
    content: Optional[str] = None
    cover_image: Optional[str] = None
    cover_gradient: Optional[str] = None
    read_time: Optional[str] = None
    tags: Optional[List[str]] = None
    status: Optional[str] = None

# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("")
async def get_all_blogs(
    admin: Admin,
    db: Annotated[Client, Depends(get_db)],
) -> list:
    """
    Get all blog posts (published & drafts).
    """
    try:
        res = db.table("blogs").select("*").order("created_at", desc=True).execute()
        return res.data or []
    except Exception as e:
        log.error(f"Error fetching blogs: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database query failed: {str(e)}"
        )

@router.post("", status_code=status.HTTP_201_CREATED)
async def create_blog(
    body: AdminBlogCreate,
    admin: Admin,
    db: Annotated[Client, Depends(get_db)],
) -> dict:
    """
    Create a new blog post.
    """
    slug = body.slug
    if not slug:
        slug = body.title.lower().strip()
        slug = re.sub(r"[^a-z0-9\s-]", "", slug)
        slug = re.sub(r"[\s-]+", "-", slug)

    # Ensure slug is unique
    try:
        existing = db.table("blogs").select("slug").eq("slug", slug).execute()
        if existing.data:
            # Append random 4-char suffix to slug to make unique
            slug = f"{slug}-{uuid.uuid4().hex[:4]}"
    except Exception:
        pass

    blog_data = {
        "title": body.title,
        "slug": slug,
        "excerpt": body.excerpt,
        "content": body.content,
        "cover_image": body.cover_image,
        "cover_gradient": body.cover_gradient,
        "read_time": body.read_time,
        "tags": body.tags,
        "status": body.status or "published",
        "created_at": datetime.utcnow().isoformat(),
        "updated_at": datetime.utcnow().isoformat(),
    }

    try:
        db.table("blogs").insert(blog_data).execute()
        return {"ok": True, "slug": slug}
    except Exception as e:
        log.error(f"Error creating blog: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to insert blog post: {str(e)}"
        )

@router.patch("/{slug}")
async def update_blog(
    slug: str,
    body: AdminBlogUpdate,
    admin: Admin,
    db: Annotated[Client, Depends(get_db)],
) -> dict:
    """
    Update an existing blog post.
    """
    update_data = {}
    for field, value in body.model_dump(exclude_unset=True).items():
        if value is not None:
            update_data[field] = value

    if not update_data:
        return {"ok": True, "message": "No changes requested."}

    update_data["updated_at"] = datetime.utcnow().isoformat()

    try:
        res = db.table("blogs").update(update_data).eq("slug", slug).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="Blog post not found")
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Error updating blog {slug}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update blog post: {str(e)}"
        )

@router.delete("/{slug}")
async def delete_blog(
    slug: str,
    admin: Admin,
    db: Annotated[Client, Depends(get_db)],
) -> dict:
    """
    Delete a blog post.
    """
    try:
        res = db.table("blogs").delete().eq("slug", slug).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="Blog post not found")
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Error deleting blog {slug}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete blog post: {str(e)}"
        )

@router.post("/upload-image")
async def upload_image(
    admin: Admin,
    db: Annotated[Client, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    file: UploadFile = File(...),
) -> dict:
    """
    Upload a cover image. Prefers Cloudflare R2 (S3-compatible) if credentials are set,
    otherwise falls back to Supabase Storage.
    """
    try:
        file_bytes = await file.read()
        ext = os.path.splitext(file.filename or "")[1] or ".jpg"
        file_path = f"{uuid.uuid4()}{ext}"
        content_type = file.content_type or "image/jpeg"

        # Check if Cloudflare R2 credentials are configured
        if settings.r2_account_id and settings.r2_access_key_id and settings.r2_secret_access_key:
            try:
                import boto3
                from botocore.config import Config

                endpoint_url = f"https://{settings.r2_account_id}.r2.cloudflarestorage.com"
                s3_client = boto3.client(
                    "s3",
                    endpoint_url=endpoint_url,
                    aws_access_key_id=settings.r2_access_key_id,
                    aws_secret_access_key=settings.r2_secret_access_key,
                    config=Config(signature_version="s3v4"),
                )

                s3_client.put_object(
                    Bucket=settings.r2_bucket_name,
                    Key=file_path,
                    Body=file_bytes,
                    ContentType=content_type,
                )

                # Construct R2 Public URL
                public_base = settings.r2_public_url.rstrip("/")
                public_url = f"{public_base}/{file_path}"
                log.info(f"Successfully uploaded cover image to Cloudflare R2: {public_url}")
                return {"url": public_url}

            except Exception as r2_err:
                log.error(f"Cloudflare R2 upload failed, falling back to Supabase Storage: {r2_err}")
                # Fall through to Supabase Storage

        # Fallback: Supabase Storage
        db.storage.from_("blog-covers").upload(
            path=file_path,
            file=file_bytes,
            file_options={"content-type": content_type}
        )

        # Get public url. Check if settings has url.
        try:
            public_url = db.storage.from_("blog-covers").get_public_url(file_path)
            # In some client versions get_public_url returns a string directly,
            # in others it returns a dict or URL object.
            if hasattr(public_url, "publicUrl"):
                public_url = public_url.publicUrl
            elif isinstance(public_url, dict) and "publicUrl" in public_url:
                public_url = public_url["publicUrl"]
        except Exception:
            # Fallback to manual URL construction
            base_url = settings.supabase_url.rstrip("/")
            public_url = f"{base_url}/storage/v1/object/public/blog-covers/{file_path}"

        log.info(f"Successfully uploaded cover image to Supabase Storage: {public_url}")
        return {"url": public_url}
    except Exception as e:
        log.error(f"Error uploading image: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Image upload failed: {str(e)}"
        )
