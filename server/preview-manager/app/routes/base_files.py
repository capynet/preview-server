"""Base files endpoints — upload/download base DB and files via S3.

Supports chunked uploads for large files (>50MB) via init/chunk/complete flow.
"""

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.auth.dependencies import require_role
from app.auth.models import Role, UserWithRole
from app.storage import storage_manager

logger = logging.getLogger(__name__)

router = APIRouter()

BACKUPS_DIR = Path("/backups")


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


@router.post("/api/projects/{slug}/base-files/db")
async def upload_base_db(
    slug: str,
    file: UploadFile,
    uncompressed_size: int = Form(0),
    user: UserWithRole = Depends(require_role(Role.manager)),
):
    return await _upload_db(slug, file, uncompressed_size)


@router.post("/api/projects/{slug}/base-files/files")
async def upload_base_files(
    slug: str,
    file: UploadFile,
    uncompressed_size: int = Form(0),
    user: UserWithRole = Depends(require_role(Role.manager)),
):
    return await _upload_files(slug, file, uncompressed_size)


async def _save_upload_to_temp(upload: UploadFile) -> str:
    """Stream an UploadFile to a temp file. Returns temp path."""
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(BACKUPS_DIR), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            while chunk := await upload.read(64 * 1024):
                f.write(chunk)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    return tmp_path


async def _process_db(slug: str, file_path: Path, uncompressed_size: int = 0) -> dict:
    """Upload DB to S3 and invalidate cache."""
    await storage_manager.upload_base_db(slug, file_path, uncompressed_size)

    # Invalidate DB cache in S3
    await storage_manager.delete_db_cache(slug)

    size = file_path.stat().st_size
    logger.info("Uploaded base DB for %s (%d bytes) to S3", slug, size)

    # Clean up temp file
    file_path.unlink(missing_ok=True)

    return {"success": True, "size_bytes": size}


async def _process_files(slug: str, file_path: Path, uncompressed_size: int = 0) -> dict:
    """Upload files to S3."""
    size = file_path.stat().st_size
    await storage_manager.upload_base_files(slug, file_path, uncompressed_size)

    logger.info("Uploaded base files for %s (%d bytes) to S3", slug, size)

    # Clean up temp file
    file_path.unlink(missing_ok=True)

    return {"success": True, "size_bytes": size}


async def _upload_db(slug: str, upload: UploadFile, uncompressed_size: int = 0) -> dict:
    tmp_path = await _save_upload_to_temp(upload)
    return await _process_db(slug, Path(tmp_path), uncompressed_size)


async def _upload_files(slug: str, upload: UploadFile, uncompressed_size: int = 0) -> dict:
    tmp_path = await _save_upload_to_temp(upload)
    return await _process_files(slug, Path(tmp_path), uncompressed_size)


# ---------------------------------------------------------------------------
# Chunked upload endpoints
# ---------------------------------------------------------------------------

UPLOAD_TMP = Path("/backups/.uploads")
CHUNK_EXPIRY_SECONDS = 2 * 3600  # 2 hours


class ChunkedInitRequest(BaseModel):
    total_chunks: int
    total_size: int


@router.post("/api/projects/{slug}/base-files/{kind}/upload/init")
async def chunked_upload_init(
    slug: str,
    kind: str,
    body: ChunkedInitRequest,
    user: UserWithRole = Depends(require_role(Role.manager)),
):
    if kind not in ("db", "files"):
        raise HTTPException(status_code=400, detail="kind must be 'db' or 'files'")
    if body.total_chunks < 1:
        raise HTTPException(status_code=400, detail="total_chunks must be >= 1")

    upload_id = str(uuid.uuid4())
    upload_dir = UPLOAD_TMP / upload_id
    upload_dir.mkdir(parents=True)

    meta = {
        "slug": slug,
        "kind": kind,
        "total_chunks": body.total_chunks,
        "total_size": body.total_size,
        "created_at": time.time(),
        "received_chunks": [],
    }
    (upload_dir / "meta.json").write_text(json.dumps(meta))

    logger.info("Chunked upload init: %s, %d chunks, %d bytes", upload_id, body.total_chunks, body.total_size)
    return {"upload_id": upload_id}


@router.post("/api/projects/{slug}/base-files/{kind}/upload/chunk")
async def chunked_upload_chunk(
    slug: str,
    kind: str,
    upload_id: str = Form(...),
    chunk_index: int = Form(...),
    file: UploadFile = ...,
    user: UserWithRole = Depends(require_role(Role.manager)),
):
    upload_dir = UPLOAD_TMP / upload_id
    meta_path = upload_dir / "meta.json"

    if not meta_path.exists():
        raise HTTPException(status_code=404, detail="Upload not found")

    meta = json.loads(meta_path.read_text())
    if meta["slug"] != slug or meta["kind"] != kind:
        raise HTTPException(status_code=400, detail="slug/kind mismatch")
    if chunk_index < 0 or chunk_index >= meta["total_chunks"]:
        raise HTTPException(status_code=400, detail=f"chunk_index out of range (0..{meta['total_chunks']-1})")

    chunk_path = upload_dir / f"{chunk_index}.part"
    fd, tmp_path = tempfile.mkstemp(dir=str(upload_dir), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            while data := await file.read(64 * 1024):
                f.write(data)
        shutil.move(tmp_path, str(chunk_path))
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    if chunk_index not in meta["received_chunks"]:
        meta["received_chunks"].append(chunk_index)
        meta_path.write_text(json.dumps(meta))

    logger.info("Chunk %d/%d received for upload %s (%d bytes)",
                chunk_index + 1, meta["total_chunks"], upload_id, chunk_path.stat().st_size)
    return {"received": chunk_index}


@router.post("/api/projects/{slug}/base-files/{kind}/upload/complete")
async def chunked_upload_complete(
    slug: str,
    kind: str,
    body: dict,
    user: UserWithRole = Depends(require_role(Role.manager)),
):
    upload_id = body.get("upload_id")
    if not upload_id:
        raise HTTPException(status_code=400, detail="upload_id required")

    uncompressed_size = int(body.get("uncompressed_size", 0))

    upload_dir = UPLOAD_TMP / upload_id
    meta_path = upload_dir / "meta.json"

    if not meta_path.exists():
        raise HTTPException(status_code=404, detail="Upload not found")

    meta = json.loads(meta_path.read_text())
    if meta["slug"] != slug or meta["kind"] != kind:
        raise HTTPException(status_code=400, detail="slug/kind mismatch")

    expected = set(range(meta["total_chunks"]))
    received = set(meta["received_chunks"])
    missing = expected - received
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing chunks: {sorted(missing)}")

    # Reassemble
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    fd, final_path = tempfile.mkstemp(dir=str(BACKUPS_DIR), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as out:
            for i in range(meta["total_chunks"]):
                chunk_path = upload_dir / f"{i}.part"
                with open(chunk_path, "rb") as chunk_f:
                    shutil.copyfileobj(chunk_f, out)

        final_size = os.path.getsize(final_path)
        logger.info("Reassembled %d chunks into %s (%d bytes)", meta["total_chunks"], final_path, final_size)

        if kind == "db":
            result = await _process_db(slug, Path(final_path), uncompressed_size)
        else:
            result = await _process_files(slug, Path(final_path), uncompressed_size)

    except Exception:
        if os.path.exists(final_path):
            os.unlink(final_path)
        raise
    finally:
        shutil.rmtree(upload_dir, ignore_errors=True)

    return result


async def cleanup_stale_uploads_loop():
    """Background task that removes stale chunked upload directories."""
    logger.info("Starting stale uploads cleanup loop")
    while True:
        try:
            await asyncio.sleep(30 * 60)
            if not UPLOAD_TMP.exists():
                continue
            now = time.time()
            for entry in UPLOAD_TMP.iterdir():
                if not entry.is_dir():
                    continue
                meta_path = entry / "meta.json"
                if meta_path.exists():
                    try:
                        meta = json.loads(meta_path.read_text())
                        created = meta.get("created_at", 0)
                    except Exception:
                        created = 0
                else:
                    created = entry.stat().st_mtime
                if now - created > CHUNK_EXPIRY_SECONDS:
                    logger.info("Cleaning up stale upload: %s", entry.name)
                    shutil.rmtree(entry, ignore_errors=True)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Error in stale uploads cleanup: %s", e, exc_info=True)
