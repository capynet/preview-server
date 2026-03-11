"""Monitor cloud VM status for real-time updates.

Called as an arq cron job (every 15 minutes).
"""

import logging

from app.database import get_previews_with_active_vms, update_preview_vm

logger = logging.getLogger(__name__)


async def check_vm_status():
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

                # Publish update event
                from app.valkey import publish_event
                await publish_event(f"previews:global", {
                    "action": "vm_cleaned",
                    "preview_name": preview_name,
                    "project_slug": project_slug,
                })
        except Exception as e:
            logger.debug(f"Error checking VM {vm_id}: {e}")
