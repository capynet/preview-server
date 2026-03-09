"""Background task: monitor cloud VM status for real-time updates."""

import asyncio
import logging

from app.database import get_previews_with_active_vms, update_preview_vm

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 30


async def docker_events_loop():
    """Periodically check cloud VM status and clean up stale entries."""
    await asyncio.sleep(10)
    logger.info("Cloud VM status monitor started")

    while True:
        try:
            await _check_vm_status()
        except asyncio.CancelledError:
            logger.info("Cloud VM status monitor cancelled")
            raise
        except Exception as e:
            logger.error(f"Cloud VM status monitor error: {e}", exc_info=True)
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


async def _check_vm_status():
    """Check all VMs with active entries and clean up disappeared ones."""
    from app.cloud import cloud_manager

    previews = await get_previews_with_active_vms()
    if not previews:
        return

    for p in previews:
        vm_id = p["vm_id"]
        project_slug = p.get("project_slug", "")
        preview_name = p["preview_name"]

        try:
            server = await cloud_manager.get_vm(vm_id)
            if server is None:
                # VM disappeared — clean up DB
                logger.warning(
                    f"VM {vm_id} for {project_slug}/{preview_name} not found, "
                    "cleaning up DB entry"
                )
                await update_preview_vm(p["id"], None, None)

                # Broadcast update
                from app.websockets import preview_list_manager
                await preview_list_manager.force_broadcast()
        except Exception as e:
            logger.debug(f"Error checking VM {vm_id}: {e}")
