"""Base files endpoints — upload/download base DB and files.

Uploads use presigned URLs for S3 backends, or proxy upload for others.
"""

import logging
import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.auth.dependencies import require_org_role, require_project_role
from app.auth.models import OrgRole, UserWithContext
from app.database import get_project_by_slug
from app.storage import storage_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/orgs/{org}/projects/{slug}/base-files", tags=["base-files"])


class BaseFileInfo(BaseModel):
    exists: bool
    size_bytes: int
    modified_at: str
    uncompressed_size: int = 0


class BaseFilesStatus(BaseModel):
    db: BaseFileInfo | None = None
    files: BaseFileInfo | None = None


async def _resolve_project(user: UserWithContext, slug: str) -> dict:
    project = await get_project_by_slug(user.org.id, slug)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{slug}' not found")
    return project


@router.get("")
async def get_base_files_status(
    slug: str,
    user: UserWithContext = Depends(require_project_role(OrgRole.viewer)),
):
    project = await _resolve_project(user, slug)
    status = await storage_manager.get_base_files_status(project["slug"])
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


@router.get("/db")
async def download_base_db(
    slug: str,
    user: UserWithContext = Depends(require_project_role(OrgRole.member)),
):
    from fastapi.responses import StreamingResponse

    project = await _resolve_project(user, slug)
    project_slug = project["slug"]
    status = await storage_manager.get_base_files_status(project_slug)
    if not status.get("db"):
        raise HTTPException(status_code=404, detail="Base database not found")

    return StreamingResponse(
        storage_manager.stream_base_db(project_slug),
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{project_slug}-base.sql.gz"',
            "Content-Length": str(status["db"]["size_bytes"]),
        },
    )


@router.get("/files")
async def download_base_files(
    slug: str,
    user: UserWithContext = Depends(require_project_role(OrgRole.member)),
):
    from fastapi.responses import StreamingResponse

    project = await _resolve_project(user, slug)
    project_slug = project["slug"]
    status = await storage_manager.get_base_files_status(project_slug)
    if not status.get("files"):
        raise HTTPException(status_code=404, detail="Base files not found")

    return StreamingResponse(
        storage_manager.stream_base_files(project_slug),
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{project_slug}-files.tar.gz"',
            "Content-Length": str(status["files"]["size_bytes"]),
        },
    )


# ---------------------------------------------------------------------------
# Direct S3 upload (presigned URL flow)
# ---------------------------------------------------------------------------


@router.post("/{kind}/upload/presign")
async def presign_upload(
    slug: str,
    kind: str,
    body: dict,
    user: UserWithContext = Depends(require_project_role(OrgRole.member)),
):
    """Generate presigned URL(s) for direct upload to S3.

    For backends that don't support presigned URLs (e.g. Storage Box),
    returns mode="proxy" so the client uploads via the proxy endpoint.
    """
    if kind not in ("db", "files"):
        raise HTTPException(status_code=400, detail="kind must be 'db' or 'files'")

    project = await _resolve_project(user, slug)
    project_slug = project["slug"]

    total_size = int(body.get("total_size", 0))
    if total_size <= 0:
        raise HTTPException(status_code=400, detail="total_size required and must be > 0")

    # Backend doesn't support presigned URLs — use proxy upload
    if not storage_manager.supports_presigned_urls:
        return {"mode": "proxy"}

    max_single = 5 * 1024 * 1024 * 1024  # 5 GB

    if total_size <= max_single:
        url = await storage_manager.generate_presigned_upload_url(project_slug, kind)
        return {"mode": "single", "presigned_url": url}
    else:
        # Multipart: parts of ~500 MB
        part_size = 500 * 1024 * 1024
        num_parts = (total_size + part_size - 1) // part_size
        upload_id = await storage_manager.create_multipart_upload(project_slug, kind)
        urls = await storage_manager.generate_presigned_part_urls(
            project_slug, kind, upload_id, num_parts,
        )
        return {
            "mode": "multipart",
            "upload_id": upload_id,
            "part_size": part_size,
            "part_urls": urls,
        }


@router.post("/{kind}/upload/complete")
async def complete_upload(
    slug: str,
    kind: str,
    body: dict,
    user: UserWithContext = Depends(require_project_role(OrgRole.member)),
):
    """Complete an upload — finalize multipart if needed, set metadata, invalidate caches."""
    if kind not in ("db", "files"):
        raise HTTPException(status_code=400, detail="kind must be 'db' or 'files'")

    project = await _resolve_project(user, slug)
    project_slug = project["slug"]

    uncompressed_size = int(body.get("uncompressed_size", 0))
    upload_id = body.get("upload_id")
    parts = body.get("parts")  # [{"ETag": "...", "PartNumber": N}, ...]

    # Complete multipart upload if applicable
    if upload_id and parts:
        await storage_manager.complete_multipart_upload(project_slug, kind, upload_id, parts)

    # Verify the object exists in storage
    status = await storage_manager.get_base_files_status(project_slug)
    obj = status.get(kind)
    if not obj:
        raise HTTPException(status_code=404, detail=f"Object not found in storage for {project_slug}/{kind}")

    # Set metadata (uncompressed size)
    if uncompressed_size:
        await storage_manager.set_object_metadata(project_slug, kind, uncompressed_size)

    # Invalidate DB cache when base DB changes
    if kind == "db":
        await storage_manager.delete_db_cache(project_slug)

    size = obj["size_bytes"]
    logger.info("Confirmed upload for %s/%s (%d bytes, uncompressed=%d)", project_slug, kind, size, uncompressed_size)
    return {"success": True, "size_bytes": size}


# ---------------------------------------------------------------------------
# Proxy upload (for backends without presigned URL support)
# ---------------------------------------------------------------------------


@router.put("/{kind}/upload/proxy")
async def proxy_upload(
    request: Request,
    slug: str,
    kind: str,
    user: UserWithContext = Depends(require_project_role(OrgRole.member)),
):
    """Accept file upload and forward to storage backend.

    Used when the backend doesn't support presigned URLs (e.g. Storage Box).
    The request body is the raw file content (application/octet-stream).
    """
    if kind not in ("db", "files"):
        raise HTTPException(status_code=400, detail="kind must be 'db' or 'files'")

    project = await _resolve_project(user, slug)
    project_slug = project["slug"]

    # Stream request body to temp file
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".gz")
    try:
        with os.fdopen(tmp_fd, "wb") as tmp_file:
            async for chunk in request.stream():
                tmp_file.write(chunk)

        # Upload to storage
        file_path = Path(tmp_path)
        if kind == "db":
            await storage_manager.upload_base_db(project_slug, file_path)
        else:
            await storage_manager.upload_base_files(project_slug, file_path)

        # Get size for response
        size = file_path.stat().st_size
        logger.info("Proxy uploaded %s/%s (%d bytes)", project_slug, kind, size)
        return {"success": True, "size_bytes": size}
    finally:
        os.unlink(tmp_path)
