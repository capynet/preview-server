"""Background task: auto-erase previews after prolonged inactivity."""

import asyncio
import logging
from datetime import datetime, timezone

from app import config_store
from app.database import get_all_previews

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 3600  # 1 hour


async def auto_erase_loop():
    """Run every CHECK_INTERVAL_SECONDS, deleting previews inactive for too long."""
    await asyncio.sleep(60)
    logger.info("Auto-erase background task started")

    while True:
        try:
            await _check_and_erase()
        except Exception as e:
            logger.error(f"Auto-erase loop error: {e}", exc_info=True)
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


async def _check_and_erase():
    """Check all previews and delete those that exceed the inactivity threshold."""
    global_enabled = await config_store.get_config("auto_erase_enabled")
    if global_enabled != "true":
        return

    global_days_str = await config_store.get_config("auto_erase_days")
    global_days = int(global_days_str) if global_days_str else 7

    previews = await get_all_previews()
    if not previews:
        return

    now = datetime.now(timezone.utc)
    erased_count = 0

    for p in previews:
        project = p["project"]
        preview_name = p["preview_name"]

        # Skip pinned previews
        if p.get("pinned"):
            continue

        # Use last_accessed_at, fallback to created_at for never-accessed previews
        ts = p.get("last_accessed_at") or p.get("created_at")
        if not ts:
            continue
        try:
            last_activity = datetime.fromisoformat(ts)
        except ValueError:
            continue

        idle_days = (now - last_activity).total_seconds() / 86400
        if idle_days < global_days:
            continue

        logger.info(
            f"Auto-erasing {project}/{preview_name}: "
            f"idle for {idle_days:.1f} days (threshold: {global_days} days)"
        )
        try:
            from app.routes.previews import delete_preview_internal
            await delete_preview_internal(project, preview_name)
            erased_count += 1
            logger.info(f"Auto-erased {project}/{preview_name}")
        except Exception as e:
            logger.error(f"Failed to auto-erase {project}/{preview_name}: {e}")

    if erased_count:
        logger.info(f"Auto-erase: deleted {erased_count} preview(s)")
