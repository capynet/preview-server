"""Database volume cache — skip SQL import by reusing a pre-populated volume.

On the first preview for a given (project, db_image, dump_file) combination,
the DB volume is exported to a tar.gz after import.  Subsequent previews
restore the volume from that cache instead of re-importing the SQL dump,
which is dramatically faster (seconds vs minutes).

Cache key = md5(dump_file)[:16] + sanitized db_spec.
Invalidated automatically when a new base DB is uploaded via `preview push db`.
"""

import asyncio
import hashlib
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

DB_CACHE_DIR = Path("/var/www/preview-manager/db-cache")


def _hash_file(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _sanitize(s: str) -> str:
    return s.replace(":", "-").replace("/", "-")


def compute_cache_key(project: str, db_spec: str, dump_path: Path) -> str:
    dump_hash = _hash_file(dump_path)
    return f"{_sanitize(db_spec)}-{dump_hash[:16]}"


def get_cache_path(project: str, cache_key: str) -> Path:
    return DB_CACHE_DIR / project / f"{cache_key}.tar.gz"


def cache_exists(project: str, cache_key: str) -> bool:
    return get_cache_path(project, cache_key).exists()


async def export_volume(volume_name: str, project: str, cache_key: str) -> Path:
    """Export a Docker volume to a gzipped tar cache file."""
    cache_path = get_cache_path(project, cache_key)
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    tmp_name = f"{cache_key}.tmp.tar.gz"
    tmp_path = cache_path.parent / tmp_name

    proc = await asyncio.create_subprocess_exec(
        "docker", "run", "--rm",
        "-v", f"{volume_name}:/data:ro",
        "-v", f"{str(cache_path.parent)}:/cache",
        "alpine",
        "sh", "-c", f"tar czf /cache/{tmp_name} -C /data .",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(f"Failed to export DB volume: {stderr.decode()}")

    tmp_path.rename(cache_path)
    size_mb = cache_path.stat().st_size / (1024 * 1024)
    logger.info("Exported DB cache: %s (%.1f MB)", cache_path.name, size_mb)
    return cache_path


async def import_volume(volume_name: str, project: str, cache_key: str) -> None:
    """Create and populate a Docker volume from a cached tar."""
    cache_path = get_cache_path(project, cache_key)

    # Create the volume (no-op if exists)
    proc = await asyncio.create_subprocess_exec(
        "docker", "volume", "create", volume_name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()

    # Populate from cache
    proc = await asyncio.create_subprocess_exec(
        "docker", "run", "--rm",
        "-v", f"{volume_name}:/data",
        "-v", f"{str(cache_path.parent)}:/cache:ro",
        "alpine",
        "sh", "-c", f"tar xzf /cache/{cache_path.name} -C /data",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(f"Failed to import DB volume from cache: {stderr.decode()}")

    logger.info("Restored DB volume from cache: %s <- %s", volume_name, cache_path.name)


def invalidate_cache(project: str) -> int:
    """Remove all cached DB volumes for a project. Returns count removed."""
    cache_dir = DB_CACHE_DIR / project
    if not cache_dir.exists():
        return 0
    count = sum(1 for f in cache_dir.iterdir() if f.is_file())
    shutil.rmtree(cache_dir)
    logger.info("Invalidated %d DB cache(s) for project '%s'", count, project)
    return count
