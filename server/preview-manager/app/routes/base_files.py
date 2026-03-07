"""Base files endpoints — upload/download base DB and files via S3.

Uploads use presigned URLs for direct CLI-to-S3 transfer.
Legacy single-file upload via POST is kept for small files / backward compat.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.dependencies import require_role
from app.auth.models import Role, UserWithRole
from app.storage import storage_manager

logger = logging.getLogger(__name__)

router = APIRouter()


class BaseFileInfo(BaseModel):
    exists: bool
    size_bytes: int
    modified_at: str
    uncompressed_size: int = 0


class BaseFilesStatus(BaseModel):
    db: BaseFileInfo | None = None
    files: BaseFileInfo | None = None


@router.get("/api/projects/{slug}/base-files")
async def get_base_files_status(
    slug: str,
    user: UserWithRole = Depends(require_role(Role.viewer)),
):
    status = await storage_manager.get_base_files_status(slug)
    result = BaseFilesStatus()
    if status.get("db"):
        d = status["db"]
        result.db = BaseFileInfo(
            exists=True,
            size_bytes=d["size_bytes"],
            modified_at=d["modified_at"],
            uncompressed_size=d.get("uncompressed_size", 0),
        )
    if status.get("files"):
        f = status["files"]
        result.files = BaseFileInfo(
            exists=True,
            size_bytes=f["size_bytes"],
            modified_at=f["modified_at"],
            uncompressed_size=f.get("uncompressed_size", 0),
        )
    return result


@router.get("/api/projects/{slug}/base-files/db")
async def download_base_db(
    slug: str,
    user: UserWithRole = Depends(require_role(Role.manager)),
):
    from fastapi.responses import StreamingResponse

    status = await storage_manager.get_base_files_status(slug)
    if not status.get("db"):
        raise HTTPException(status_code=404, detail="Base database not found")

    return StreamingResponse(
        storage_manager.stream_base_db(slug),
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{slug}-base.sql.gz"',
            "Content-Length": str(status["db"]["size_bytes"]),
        },
    )


@router.get("/api/projects/{slug}/base-files/files")
async def download_base_files(
    slug: str,
    user: UserWithRole = Depends(require_role(Role.manager)),
):
    from fastapi.responses import StreamingResponse

    status = await storage_manager.get_base_files_status(slug)
    if not status.get("files"):
        raise HTTPException(status_code=404, detail="Base files not found")

    return StreamingResponse(
        storage_manager.stream_base_files(slug),
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{slug}-files.tar.gz"',
            "Content-Length": str(status["files"]["size_bytes"]),
        },
    )


# ---------------------------------------------------------------------------
# Direct S3 upload (presigned URL flow)
# ---------------------------------------------------------------------------


@router.post("/api/projects/{slug}/base-files/{kind}/upload/presign")
async def presign_upload(
    slug: str,
    kind: str,
    body: dict,
    user: UserWithRole = Depends(require_role(Role.manager)),
):
    """Generate presigned URL(s) for direct upload to S3.

    For files <= 5 GB: returns a single presigned PUT URL.
    For files > 5 GB: initiates a multipart upload and returns presigned URLs per part.
    """
    if kind not in ("db", "files"):
        raise HTTPException(status_code=400, detail="kind must be 'db' or 'files'")

    total_size = int(body.get("total_size", 0))
    if total_size <= 0:
        raise HTTPException(status_code=400, detail="total_size required and must be > 0")

    max_single = 5 * 1024 * 1024 * 1024  # 5 GB

    if total_size <= max_single:
        url = await storage_manager.generate_presigned_upload_url(slug, kind)
        return {"mode": "single", "presigned_url": url}
    else:
        # Multipart: parts of ~500 MB
        part_size = 500 * 1024 * 1024
        num_parts = (total_size + part_size - 1) // part_size
        upload_id = await storage_manager.create_multipart_upload(slug, kind)
        urls = await storage_manager.generate_presigned_part_urls(
            slug, kind, upload_id, num_parts,
        )
        return {
            "mode": "multipart",
            "upload_id": upload_id,
            "part_size": part_size,
            "part_urls": urls,
        }


@router.post("/api/projects/{slug}/base-files/{kind}/upload/complete")
async def complete_upload(
    slug: str,
    kind: str,
    body: dict,
    user: UserWithRole = Depends(require_role(Role.manager)),
):
    """Complete an upload — finalize multipart if needed, set metadata, invalidate caches."""
    if kind not in ("db", "files"):
        raise HTTPException(status_code=400, detail="kind must be 'db' or 'files'")

    uncompressed_size = int(body.get("uncompressed_size", 0))
    upload_id = body.get("upload_id")
    parts = body.get("parts")  # [{"ETag": "...", "PartNumber": N}, ...]

    # Complete multipart upload if applicable
    if upload_id and parts:
        await storage_manager.complete_multipart_upload(slug, kind, upload_id, parts)

    # Verify the object exists in S3
    status = await storage_manager.get_base_files_status(slug)
    obj = status.get(kind)
    if not obj:
        raise HTTPException(status_code=404, detail=f"Object not found in S3 for {slug}/{kind}")

    # Set metadata (uncompressed size)
    if uncompressed_size:
        await storage_manager.set_object_metadata(slug, kind, uncompressed_size)

    # Invalidate DB cache when base DB changes
    if kind == "db":
        await storage_manager.delete_db_cache(slug)

    size = obj["size_bytes"]
    logger.info("Confirmed upload for %s/%s (%d bytes, uncompressed=%d)", slug, kind, size, uncompressed_size)
    return {"success": True, "size_bytes": size}
