"""Base files endpoints — check status, download and upload base DB/files.

Files are extracted on upload into .base-files/{project}/files/ and shared
across previews via OverlayFS. The tar.gz is not kept on disk.
"""

import asyncio
import logging
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
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
    base_dir = get_base_files_dir(slug)
    if not base_dir.exists():
        raise HTTPException(status_code=404, detail="Base files not found")

    async def _stream():
        # Generate tar.gz on-the-fly from the extracted directory
        proc = await asyncio.create_subprocess_exec(
            "tar", "czf", "-",
            "--exclude=./css", "--exclude=./js", "--exclude=./php",
            "-C", str(base_dir), ".",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        while True:
            chunk = await proc.stdout.read(64 * 1024)
            if not chunk:
                break
            yield chunk
        await proc.wait()

    return StreamingResponse(
        _stream(),
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{slug}-files.tar.gz"',
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


async def _upload_db(slug: str, upload: UploadFile) -> dict:
    """Upload database dump (kept as .sql.gz)."""
    dest = _db_path(slug)
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=str(BACKUPS_DIR), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            while chunk := await upload.read(64 * 1024):
                f.write(chunk)
        shutil.move(tmp_path, str(dest))
        logger.info("Uploaded base DB %s (%d bytes)", dest, dest.stat().st_size)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    return {"success": True, "path": str(dest), "size_bytes": dest.stat().st_size}


async def _upload_and_extract_files(slug: str, upload: UploadFile) -> dict:
    """Upload files tar.gz, extract to .base-files/{project}/files/, delete tar."""
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Stream upload to temp file
    fd, tmp_path = tempfile.mkstemp(dir=str(BACKUPS_DIR), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            while chunk := await upload.read(64 * 1024):
                f.write(chunk)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    tar_size = os.path.getsize(tmp_path)
    logger.info("Received files tar.gz for %s (%d bytes)", slug, tar_size)

    try:
        # 2. Unmount all overlays for this project
        await umount_all_for_project(slug)

        # 3. Replace base-files directory
        base_dir = get_base_files_dir(slug)
        if base_dir.exists():
            shutil.rmtree(base_dir)
        base_dir.mkdir(parents=True)

        # 4. Extract tar.gz
        proc = await asyncio.create_subprocess_exec(
            "tar", "xzf", tmp_path, "-C", str(base_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            error = (stdout.decode() + stderr.decode()).strip()
            raise RuntimeError(f"Failed to extract files: {error}")

        # 5. Fix ownership (www-data:www-data, UID/GID 33)
        proc = await asyncio.create_subprocess_exec(
            "chown", "-R", "33:33", str(base_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

        logger.info("Extracted base files to %s", base_dir)

        # 6. Remount overlays for all active previews
        await remount_all_for_project(slug)

    finally:
        # 7. Always delete the temp tar.gz
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    # Also remove legacy tar.gz if it exists
    legacy_tar = BACKUPS_DIR / f"{slug}-files.tar.gz"
    if legacy_tar.exists():
        legacy_tar.unlink()
        logger.info("Removed legacy tar.gz: %s", legacy_tar)

    return {"success": True, "path": str(base_dir)}
