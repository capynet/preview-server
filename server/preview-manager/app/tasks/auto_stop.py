"""Background task: auto-stop previews after inactivity.

For cloud previews: destroy the VM but keep the volume.
"""

import asyncio
import logging
from datetime import datetime, timezone

from app import config_store
from app.database import (
    get_all_previews, get_project, has_running_deployment,
    update_preview_vm,
)
from app.cloud import cloud_manager
from app.caddy_api import caddy_manager

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 60  # 1 minute


async def auto_stop_loop():
    """Run every CHECK_INTERVAL_SECONDS, stopping idle previews."""
    await asyncio.sleep(30)
    logger.info("Auto-stop background task started")

    while True:
        try:
            await _check_and_stop()
        except Exception as e:
            logger.error(f"Auto-stop loop error: {e}", exc_info=True)
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


async def _check_and_stop():
    """Check all previews and stop those that exceed their inactivity threshold."""
    global_enabled = await config_store.get_config("auto_stop_enabled")
    if global_enabled != "true":
        return

    global_minutes_str = await config_store.get_config("auto_stop_minutes")
    global_minutes = int(global_minutes_str) if global_minutes_str else 60

    previews = await get_all_previews()
    if not previews:
        return

    now = datetime.now(timezone.utc)
    stopped_count = 0

    for p in previews:
        project = p["project"]
        preview_name = p["preview_name"]

        # Only stop previews that have an active VM
        if not p.get("vm_id"):
            continue

        # Check per-project override
        proj = await get_project(project)
        if proj and proj["auto_stop_enabled"] is not None:
            if not proj["auto_stop_enabled"]:
                continue
            threshold_minutes = proj["auto_stop_minutes"] if proj["auto_stop_minutes"] else global_minutes
        else:
            threshold_minutes = global_minutes

        # Determine last activity
        last_accessed = p.get("last_accessed_at")
        last_deployed = p.get("last_deployed_at")

        last_activity = None
        for ts in (last_accessed, last_deployed):
            if ts:
                try:
                    dt = datetime.fromisoformat(ts)
                    if last_activity is None or dt > last_activity:
                        last_activity = dt
                except ValueError:
                    pass

        if not last_activity:
            continue

        idle_seconds = (now - last_activity).total_seconds()
        if idle_seconds < threshold_minutes * 60:
            continue

        # Skip if there's an active deployment
        if await has_running_deployment(p["id"]):
            continue

        # Destroy VM (keep volume)
        logger.info(
            f"Auto-stopping {project}/{preview_name}: "
            f"idle for {int(idle_seconds / 60)} min (threshold: {threshold_minutes} min)"
        )
        try:
            await cloud_manager.destroy_vm(p["vm_id"])
            await caddy_manager.remove_preview_routes(preview_name, project)
            await update_preview_vm(project, preview_name, None, None)
            stopped_count += 1
            logger.info(f"Auto-stopped {project}/{preview_name} (VM destroyed, volume kept)")
        except Exception as e:
            logger.error(f"Failed to auto-stop {project}/{preview_name}: {e}")

    if stopped_count:
        logger.info(f"Auto-stop: stopped {stopped_count} preview(s)")
