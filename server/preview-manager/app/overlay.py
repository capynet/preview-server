"""OverlayFS helpers for sharing base files across previews.

Base files are extracted once per project into .base-files/{project}/files/.
Each preview mounts an overlay with its own upper (writable) layer,
so changes are per-preview and the base files are shared read-only.
"""

import asyncio
import logging
from pathlib import Path

from config.settings import settings

logger = logging.getLogger(__name__)

BASE_FILES_ROOT = Path(settings.previews_base_path) / ".base-files"

# Defaults (can be overridden by preview.yml)
DEFAULT_DOCROOT = "web"
DEFAULT_PUBLIC_PATH = "sites/default/files"


def get_base_files_dir(project: str) -> Path:
    """Get the shared base files directory for a project."""
    return BASE_FILES_ROOT / project / "files"


def get_overlay_dir(preview_path: Path) -> Path:
    """Get the .overlay directory for a preview."""
    return preview_path / ".overlay"


def get_files_mount_point(
    preview_path: Path,
    docroot: str = DEFAULT_DOCROOT,
    public_path: str = DEFAULT_PUBLIC_PATH,
) -> Path:
    """Get the mount point for the overlay (where Drupal sees files)."""
    return preview_path / docroot / public_path


async def is_mounted(mount_point: Path) -> bool:
    """Check if a path is an active mount point."""
    proc = await asyncio.create_subprocess_exec(
        "mountpoint", "-q", str(mount_point),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    return proc.returncode == 0


async def mount_overlay(
    project: str,
    preview_path: Path,
    docroot: str = DEFAULT_DOCROOT,
    public_path: str = DEFAULT_PUBLIC_PATH,
) -> None:
    """Mount an overlay filesystem for a preview.

    Lower (read-only):  .base-files/{project}/files/
    Upper (read-write):  {preview}/.overlay/upper/
    Merged (visible):    {preview}/{docroot}/{public_path}/
    """
    base = get_base_files_dir(project)
    if not base.exists():
        raise RuntimeError(
            f"Base files not found for project '{project}'. "
            f"Upload base files first with: preview push files"
        )

    overlay = get_overlay_dir(preview_path)
    upper = overlay / "upper"
    work = overlay / "work"
    mount_point = get_files_mount_point(preview_path, docroot, public_path)

    upper.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    mount_point.mkdir(parents=True, exist_ok=True)

    if await is_mounted(mount_point):
        return  # Already mounted

    proc = await asyncio.create_subprocess_exec(
        "sudo", "mount", "-t", "overlay", "overlay",
        "-o", f"lowerdir={base},upperdir={upper},workdir={work}",
        str(mount_point),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        error = (stdout.decode() + stderr.decode()).strip()
        raise RuntimeError(f"Failed to mount overlay at {mount_point}: {error}")

    logger.info("Mounted overlay: %s (lower=%s)", mount_point, base)


async def umount_overlay(
    preview_path: Path,
    docroot: str = DEFAULT_DOCROOT,
    public_path: str = DEFAULT_PUBLIC_PATH,
) -> None:
    """Unmount the overlay filesystem for a preview."""
    mount_point = get_files_mount_point(preview_path, docroot, public_path)
    if not await is_mounted(mount_point):
        return

    proc = await asyncio.create_subprocess_exec(
        "sudo", "umount", str(mount_point),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        error = (stdout.decode() + stderr.decode()).strip()
        logger.warning("Failed to umount %s: %s", mount_point, error)
    else:
        logger.info("Unmounted overlay: %s", mount_point)


async def umount_all_for_project(project: str) -> None:
    """Unmount overlays for all previews of a project."""
    from app.database import get_all_previews

    previews = await get_all_previews()
    for p in previews:
        if p["project"] == project and p["status"] in ("active", "failed"):
            preview_path = Path(p["path"])
            try:
                await umount_overlay(preview_path)
            except Exception as e:
                logger.warning(
                    "Failed to umount %s/%s: %s",
                    project, p["preview_name"], e,
                )


async def remount_all_for_project(project: str) -> None:
    """Remount overlays for all previews of a project."""
    from app.database import get_all_previews

    base = get_base_files_dir(project)
    if not base.exists():
        return

    previews = await get_all_previews()
    for p in previews:
        if p["project"] == project and p["status"] in ("active", "failed"):
            preview_path = Path(p["path"])
            overlay_dir = get_overlay_dir(preview_path)
            if overlay_dir.exists():
                try:
                    await mount_overlay(project, preview_path)
                except Exception as e:
                    logger.warning(
                        "Failed to remount %s/%s: %s",
                        project, p["preview_name"], e,
                    )


async def remount_all() -> None:
    """Remount all overlays after server restart."""
    from app.database import get_all_previews

    if not BASE_FILES_ROOT.exists():
        return

    previews = await get_all_previews()
    mounted = 0
    for p in previews:
        if p["status"] not in ("active", "failed"):
            continue
        project = p["project"]
        preview_path = Path(p["path"])
        base = get_base_files_dir(project)
        overlay_dir = get_overlay_dir(preview_path)

        if base.exists() and overlay_dir.exists():
            try:
                await mount_overlay(project, preview_path)
                mounted += 1
            except Exception as e:
                logger.warning(
                    "Failed to remount %s/%s: %s",
                    project, p["preview_name"], e,
                )

    if mounted:
        logger.info("Remounted %d overlay(s) on startup", mounted)
