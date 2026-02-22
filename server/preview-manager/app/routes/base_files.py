"""Base files endpoints — check status, download and upload base DB/files."""

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


def _db_path(slug: str) -> Path:
    return BACKUPS_DIR / f"{slug}-base.sql.gz"


def _files_path(slug: str) -> Path:
    return BACKUPS_DIR / f"{slug}-files.tar.gz"


@router.get("/api/projects/{slug}/base-files")
async def get_base_files_status(
    slug: str,
    user: UserWithRole = Depends(require_role(Role.viewer)),
):
    return BaseFilesStatus(
        db=_file_info(_db_path(slug)),
        files=_file_info(_files_path(slug)),
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
    path = _files_path(slug)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Base files archive not found")

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


@router.post("/api/projects/{slug}/base-files/db")
async def upload_base_db(
    slug: str,
    file: UploadFile,
    user: UserWithRole = Depends(require_role(Role.manager)),
):
    return await _upload_base_file(slug, file, _db_path(slug))


@router.post("/api/projects/{slug}/base-files/files")
async def upload_base_files(
    slug: str,
    file: UploadFile,
    user: UserWithRole = Depends(require_role(Role.manager)),
):
    return await _upload_base_file(slug, file, _files_path(slug))


async def _upload_base_file(slug: str, upload: UploadFile, dest: Path) -> dict:
    """Write upload to a temp file in the same dir, then atomic rename."""
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=str(BACKUPS_DIR), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            while chunk := await upload.read(64 * 1024):
                f.write(chunk)
        shutil.move(tmp_path, str(dest))
        logger.info("Uploaded base file %s (%d bytes)", dest, dest.stat().st_size)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    return {"success": True, "path": str(dest), "size_bytes": dest.stat().st_size}
