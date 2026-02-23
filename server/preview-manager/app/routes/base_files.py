"""Base files endpoints — check status, download and upload base DB/files.

Files are extracted on upload into .base-files/{project}/files/ and shared
across previews via OverlayFS. The tar.gz is not kept on disk.

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
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.auth.dependencies import require_role
from app.auth.models import Role, UserWithRole
from app.overlay import (
    get_base_files_dir,
    umount_all_for_project,
    remount_all_for_project,
)

logger = logging.getLogger(__name__)

router = APIRouter()

BACKUPS_DIR = Path("/backups")


class BaseFileInfo(BaseModel):
    exists: bool
    size_bytes: int
    modified_at: str


class BaseFilesStatus(BaseModel):
    db: BaseFileInfo | None = None
    files: BaseFileInfo | None = None


def _file_info(path: Path) -> BaseFileInfo | None:
    if not path.exists():
        return None
    stat = path.stat()
    mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    return BaseFileInfo(exists=True, size_bytes=stat.st_size, modified_at=mtime)


def _dir_info(path: Path) -> BaseFileInfo | None:
    """Get info about an extracted files directory."""
    if not path.exists():
        return None
    # Use the directory's own mtime as modified_at
    stat = path.stat()
    mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    # Calculate total size via du (async would be better but this is a status check)
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return BaseFileInfo(exists=True, size_bytes=total, modified_at=mtime)


def _db_path(slug: str) -> Path:
    return BACKUPS_DIR / f"{slug}-base.sql.gz"


@router.get("/api/projects/{slug}/base-files")
async def get_base_files_status(
    slug: str,
    user: UserWithRole = Depends(require_role(Role.viewer)),
):
    base_dir = get_base_files_dir(slug)
    return BaseFilesStatus(
        db=_file_info(_db_path(slug)),
        files=_dir_info(base_dir),
    )


@router.get("/api/projects/{slug}/base-files/db")
async def download_base_db(
    slug: str,
    user: UserWithRole = Depends(require_role(Role.manager)),
):
    path = _db_path(slug)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Base database not found")

    async def _stream():
        with open(path, "rb") as f:
            while chunk := f.read(64 * 1024):
                yield chunk

    return StreamingResponse(
        _stream(),
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{path.name}"',
            "Content-Length": str(path.stat().st_size),
        },
    )


@router.get("/api/projects/{slug}/base-files/files")
async def download_base_files(
    slug: str,
    user: UserWithRole = Depends(require_role(Role.manager)),
):
    # Serve the saved tar.gz (kept from the original upload)
    tar_path = get_base_files_dir(slug).parent / "files.tar.gz"
    if not tar_path.exists():
        raise HTTPException(status_code=404, detail="Base files not found")

    async def _stream():
        with open(tar_path, "rb") as f:
            while chunk := f.read(64 * 1024):
                yield chunk

    return StreamingResponse(
        _stream(),
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{slug}-files.tar.gz"',
            "Content-Length": str(tar_path.stat().st_size),
        },
    )


@router.post("/api/projects/{slug}/base-files/db")
async def upload_base_db(
    slug: str,
    file: UploadFile,
    user: UserWithRole = Depends(require_role(Role.manager)),
):
    return await _upload_db(slug, file)


@router.post("/api/projects/{slug}/base-files/files")
async def upload_base_files(
    slug: str,
    file: UploadFile,
    user: UserWithRole = Depends(require_role(Role.manager)),
):
    return await _upload_and_extract_files(slug, file)


async def _save_upload_to_temp(upload: UploadFile) -> str:
    """Stream an UploadFile to a temp file in BACKUPS_DIR. Returns temp path."""
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


async def _process_db(slug: str, file_path: Path) -> dict:
    """Process a database dump file: move to final destination."""
    dest = _db_path(slug)
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    shutil.move(str(file_path), str(dest))
    logger.info("Uploaded base DB %s (%d bytes)", dest, dest.stat().st_size)
    return {"success": True, "path": str(dest), "size_bytes": dest.stat().st_size}


async def _process_files(slug: str, file_path: Path) -> dict:
    """Process a files tar.gz: extract, chown, remount overlays."""
    tar_size = file_path.stat().st_size
    logger.info("Processing files tar.gz for %s (%d bytes)", slug, tar_size)

    try:
        # 1. Unmount all overlays for this project
        await umount_all_for_project(slug)

        # 2. Replace base-files directory
        base_dir = get_base_files_dir(slug)
        if base_dir.exists():
            shutil.rmtree(base_dir)
        base_dir.mkdir(parents=True)

        # 3. Extract tar.gz
        proc = await asyncio.create_subprocess_exec(
            "tar", "xzf", str(file_path), "-C", str(base_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            error = (stdout.decode() + stderr.decode()).strip()
            raise RuntimeError(f"Failed to extract files: {error}")

        # 4. Fix ownership (www-data:www-data, UID/GID 33)
        proc = await asyncio.create_subprocess_exec(
            "chown", "-R", "33:33", str(base_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

        # 5. Touch directory so mtime reflects upload time (tar preserves original dates)
        os.utime(base_dir)

        logger.info("Extracted base files to %s", base_dir)

        # 6. Remount overlays for all active previews
        await remount_all_for_project(slug)

    finally:
        # 7. Keep the tar.gz alongside extracted files for fast downloads
        tar_dest = get_base_files_dir(slug).parent / "files.tar.gz"
        if file_path.exists():
            shutil.move(str(file_path), str(tar_dest))
            logger.info("Saved tar.gz at %s", tar_dest)

    # Remove legacy tar.gz from /backups/ if it exists
    legacy_tar = BACKUPS_DIR / f"{slug}-files.tar.gz"
    if legacy_tar.exists():
        legacy_tar.unlink()
        logger.info("Removed legacy tar.gz: %s", legacy_tar)

    return {"success": True, "path": str(base_dir)}


async def _upload_db(slug: str, upload: UploadFile) -> dict:
    """Upload database dump (kept as .sql.gz)."""
    tmp_path = await _save_upload_to_temp(upload)
    return await _process_db(slug, Path(tmp_path))


async def _upload_and_extract_files(slug: str, upload: UploadFile) -> dict:
    """Upload files tar.gz, extract to .base-files/{project}/files/."""
    tmp_path = await _save_upload_to_temp(upload)
    return await _process_files(slug, Path(tmp_path))


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

    # Write chunk to disk
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

    # Track received chunks
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

    upload_dir = UPLOAD_TMP / upload_id
    meta_path = upload_dir / "meta.json"

    if not meta_path.exists():
        raise HTTPException(status_code=404, detail="Upload not found")

    meta = json.loads(meta_path.read_text())
    if meta["slug"] != slug or meta["kind"] != kind:
        raise HTTPException(status_code=400, detail="slug/kind mismatch")

    # Verify all chunks received
    expected = set(range(meta["total_chunks"]))
    received = set(meta["received_chunks"])
    missing = expected - received
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing chunks: {sorted(missing)}")

    # Reassemble chunks into final file
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

        # Process the reassembled file
        if kind == "db":
            result = await _process_db(slug, Path(final_path))
        else:
            result = await _process_files(slug, Path(final_path))

    except Exception:
        if os.path.exists(final_path):
            os.unlink(final_path)
        raise
    finally:
        # Clean up chunks directory
        shutil.rmtree(upload_dir, ignore_errors=True)

    return result


async def cleanup_stale_uploads_loop():
    """Background task that removes stale chunked upload directories."""
    logger.info("Starting stale uploads cleanup loop")
    while True:
        try:
            await asyncio.sleep(30 * 60)  # every 30 minutes
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
